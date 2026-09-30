from __future__ import annotations

"""代理文件解析与轮换模块

支持从 txt 文件加载代理列表（一行一个），自动识别各种格式：
  - ip:port
  - http://ip:port
  - socks5://ip:port
  - user:pass@ip:port
  - http://user:pass@ip:port
  - socks5://user:pass@ip:port

支持轮询（round-robin），状态持久化，重启后续接。
"""

import os
import json
import time as _time
import threading
import hashlib
import requests as _requests
from typing import Optional, List, Tuple
from urllib.parse import urlparse, unquote

from logger import logger

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE = os.path.join(_BASE_DIR, ".proxy_state.json")


def parse_proxy_line(line: str) -> Optional[dict]:
    """将一行文本解析为 Playwright 代理配置。

    支持的格式：
        ip:port
        http://ip:port
        https://ip:port
        socks5://ip:port
        socks4://ip:port
        user:pass@ip:port
        http://user:pass@ip:port
        socks5://user:pass@ip:port

    Returns:
        Playwright 代理 dict: {"server": "...", "username": "...", "password": "..."}
        解析失败返回 None
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # 情况 1：带协议头（http://, socks5:// 等）
    if "://" in line:
        parsed = urlparse(line)
        if not parsed.hostname or not parsed.port:
            return None
        result = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        }
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result

    # 情况 2：user:pass@ip:port
    if "@" in line:
        cred_part, host_part = line.rsplit("@", 1)
        if ":" not in cred_part or ":" not in host_part:
            return None
        user, pwd = cred_part.split(":", 1)
        # host_part 可能是 ip:port
        parts = host_part.split(":")
        if len(parts) != 2:
            return None
        ip, port = parts
        return {
            "server": f"http://{ip}:{port}",
            "username": user,
            "password": pwd,
        }

    # 情况 3：ip:port 或 ip:port:user:pass（裸格式，默认 HTTP）
    if ":" in line:
        parts = line.split(":")
        if len(parts) == 2 and parts[1].isdigit():
            return {"server": f"http://{parts[0]}:{parts[1]}"}
        # Webshare 等：ip:port:user:pass
        if len(parts) == 4 and parts[1].isdigit():
            return {
                "server": f"http://{parts[0]}:{parts[1]}",
                "username": parts[2],
                "password": parts[3],
            }

    return None


def proxy_to_url(proxy_config: dict) -> str:
    """将 Playwright 代理配置转为 URL 字符串（用于 Geo 查询）。

    例如 {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"}
    → "http://u:p@1.2.3.4:8080"
    """
    server = proxy_config.get("server", "")
    user = proxy_config.get("username")
    pwd = proxy_config.get("password")

    if user and pwd:
        # 在 server 中插入认证信息
        scheme, rest = server.split("://", 1)
        return f"{scheme}://{user}:{pwd}@{rest}"
    return server


def load_proxy_file(filepath: str, auth: dict = None) -> List[dict]:
    """从文件加载代理列表。

    文件格式：一行一个代理，# 开头为注释，空行跳过。
    支持格式：
      - ip:port
      - user:pass@ip:port
      - http://ip:port

    Args:
        filepath: 代理文件路径
        auth: 可选，统一认证 {"username": "...", "password": "..."}
              如果代理没有自带认证，会自动加上

    Returns:
        解析后的 Playwright 代理配置列表
    """
    abs_path = filepath if os.path.isabs(filepath) else os.path.join(_BASE_DIR, filepath)

    if not os.path.exists(abs_path):
        logger.warning(f"[ProxyFile] 代理文件不存在: {abs_path}")
        return []

    proxies: List[dict] = []
    with open(abs_path, "r", encoding="utf-8-sig") as f:  # utf-8-sig 自动去 BOM
        for line_num, line in enumerate(f, 1):
            parsed = parse_proxy_line(line)
            if parsed:
                # 如果代理没有认证信息，但 config 提供了统一认证，自动加上
                if auth and "username" not in parsed:
                    parsed["username"] = auth.get("username", "")
                    parsed["password"] = auth.get("password", "")
                proxies.append(parsed)
            elif line.strip() and not line.strip().startswith("#"):
                logger.warning(f"[ProxyFile] 第 {line_num} 行无法解析: {line.strip()}")

    logger.info(f"[ProxyFile] 从 {filepath} 加载了 {len(proxies)} 个代理")
    return proxies


class ProxyRotator:
    """代理轮换器。

    线程安全，支持 round-robin 轮换，状态持久化到 .proxy_state.json。
    重启后从上次断开的代理继续。
    """

    _lock = threading.Lock()

    def __init__(self, proxies: List[dict]):
        self._proxies = proxies
        self._count = len(proxies)

    def pick(self) -> Optional[dict]:
        """取下一个代理（round-robin），推进索引并持久化。"""
        if not self._proxies:
            return None

        with self._lock:
            index = self._load_index()
            if index is None or index >= self._count:
                index = 0

            # 保存下一个索引
            self._save_index(index + 1)

            proxy = self._proxies[index]
            server = proxy.get("server", "?")
            logger.info(f"[Proxy] 使用代理 [{index + 1}/{self._count}] {server}")
            return proxy

    def __len__(self) -> int:
        return self._count

    @staticmethod
    def _load_index() -> Optional[int]:
        try:
            with open(_STATE_FILE) as f:
                return json.load(f).get("next_proxy_index")
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return None

    @staticmethod
    def _save_index(index: int) -> None:
        try:
            # 读取现有状态，合并写入（不覆盖端口模式的状态）
            state = {}
            try:
                with open(_STATE_FILE) as f:
                    state = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            state["next_proxy_index"] = index
            with open(_STATE_FILE, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.warning(f"[ProxyFile] 保存代理索引失败: {e}")


def fetch_proxies_from_api(api_config: dict, auth: dict = None) -> List[dict]:
    """从站大爷 API 实时提取代理。

    Args:
        api_config: config.json 中的 proxy_api 配置
        auth: proxy_auth 配置 {"username": "...", "password": "..."}

    Returns:
        Playwright 代理配置列表
    """
    url = api_config.get("url", "")
    api = api_config.get("api") or api_config.get("inst_id", "")
    akey = api_config.get("akey", "")

    # 如果没填 akey，从密码自动计算（中间16位MD5）
    if not akey and auth and auth.get("password"):
        md5_full = hashlib.md5(auth["password"].encode()).hexdigest()
        akey = md5_full[8:24]
        logger.info(f"[ProxyAPI] 自动计算 akey: {akey}")

    # API 调用间隔至少 12 秒
    elapsed = _time.time() - getattr(fetch_proxies_from_api, "_last_call", 0)
    if elapsed < 12:
        wait = 12 - elapsed
        logger.info(f"[ProxyAPI] 冷却中，等待 {wait:.1f}s...")
        _time.sleep(wait)
    fetch_proxies_from_api._last_call = _time.time()

    params = {
        "api": api,
        "akey": akey,
        "count": api_config.get("count", 10),
        "timespan": api_config.get("timespan", 3),
        "type": api_config.get("type", 3),
    }

    try:
        resp = _requests.get(url, params=params, timeout=10)
        data = resp.json()

        if data.get("code") != "10001":
            logger.error(f"[ProxyAPI] 提取失败: {data.get('msg', 'unknown')}")
            return []

        proxy_list = data.get("data", {}).get("proxy_list", [])
        proxies = []
        for item in proxy_list:
            ip = item.get("ip")
            port = item.get("port")
            expired = item.get("timeout", item.get("expired_seconds", 999))
            if ip and port:
                proto = (auth or {}).get("proxy_type", "http")
                proxy = {"server": f"{proto}://{ip}:{port}"}
                if auth:
                    proxy["username"] = auth.get("username", "")
                    proxy["password"] = auth.get("password", "")
                proxy["expired_seconds"] = expired
                proxies.append(proxy)

        logger.info(f"[ProxyAPI] 提取 {len(proxies)} 个代理")
        # 记录提取时间，用于计算实际剩余寿命
        extracted_at = _time.time()
        for p in proxies:
            p["_extracted_at"] = extracted_at
        return proxies

    except Exception as e:
        logger.error(f"[ProxyAPI] API 调用失败: {e}")
        return []


def fetch_free_proxies(app_id: str, password: str, count: int = 10,
                       dalu: int = 0, protocol_type: int = 3,
                       level_type: int = 1, sleep_type: int = 2,
                       alive_type: int = 2) -> List[dict]:
    """从站大爷免费代理 API 提取海外 SOCKS5 代理（无认证，Chromium 直连）。

    Args:
        app_id: 应用 ID
        password: 应用密码（会自动计算 16 位 MD5 akey）
        count: 提取数量（最大 100）
        dalu: 0=海外，1=大陆
        protocol_type: 3=socks5
        level_type: 1=高匿，2=普匿
        sleep_type: 1=1秒内，2=3秒内，3=5秒内
        alive_type: 1=10分钟，2=半小时，3=1小时以上

    Returns:
        Playwright 代理配置列表（无 username/password，免费代理无需认证）
    """
    akey = hashlib.md5(password.encode()).hexdigest()[8:24]

    params = {
        "app_id": app_id,
        "akey": akey,
        "dalu": dalu,
        "protocol_type": protocol_type,
        "level_type": level_type,
        "sleep_type": sleep_type,
        "alive_type": alive_type,
        "count": count,
        "return_type": 3,  # JSON
    }

    try:
        resp = _requests.get("http://www.zdopen.com/FreeProxy/Get/",
                             params=params, timeout=10)
        data = resp.json()

        if data.get("code") != "10001":
            logger.error(f"[FreeProxy] 提取失败: {data.get('msg', 'unknown')}")
            return []

        proxy_list = data.get("data", {}).get("proxy_list", [])
        proxies = []
        for item in proxy_list:
            ip = item.get("ip")
            port = item.get("port")
            proto = item.get("protocol", "socks5")
            if ip and port:
                proxy = {"server": f"{proto}://{ip}:{port}"}
                proxy["adr"] = item.get("adr", "")
                proxy["level"] = item.get("level", "")
                proxies.append(proxy)

        logger.info(f"[FreeProxy] 提取 {len(proxies)} 个免费海外代理")
        return proxies

    except Exception as e:
        logger.error(f"[FreeProxy] API 调用失败: {e}")
        return []


def fetch_exclusive_proxies(api_id: str, password: str, count: int = 1,
                            pro: int = 1, order: int = 1) -> List[dict]:
    """从站大爷独享 IP 池提取代理（HTTP 带认证，context 级使用）。

    Args:
        api_id: 实例 ID
        password: 实例密码
        count: 提取数量
        pro: 1=HTTP(S), 2=SOCKS5
        order: 1=存活时间从长到短, 2=随机

    Returns:
        Playwright 代理配置列表（含 username/password，用于 context 级代理）
    """
    akey = hashlib.md5(password.encode()).hexdigest()[8:24]

    params = {
        "api": api_id,
        "akey": akey,
        "pro": pro,
        "count": count,
        "order": order,
        "type": 3,
    }

    try:
        resp = _requests.get("http://www.zdopen.com/ExclusiveProxy/GetIP/",
                             params=params, timeout=10)
        data = resp.json()

        if data.get("code") != "10001":
            logger.error(f"[Exclusive] 提取失败: {data.get('msg', 'unknown')}")
            return []

        proxy_list = data.get("data", {}).get("proxy_list", [])
        proxies = []
        for item in proxy_list:
            ip = item.get("ip")
            port = item.get("port")
            if ip and port:
                proxy = {
                    "server": f"http://{ip}:{port}",
                    "username": api_id,
                    "password": password,
                    "timeout": item.get("timeout", 0),
                    "adr": item.get("adr", ""),
                }
                proxies.append(proxy)

        logger.info(f"[Exclusive] 提取 {len(proxies)} 个独享代理")
        return proxies

    except Exception as e:
        logger.error(f"[Exclusive] API 调用失败: {e}")
        return []
