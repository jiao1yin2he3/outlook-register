"""深度验证器：真实 L7 转发探针（原版最大的缺口 —— 只做 TCP 预检）。

探针设计（实测教训：44% TCP 端口开放 ≈ 0% 真实可用）：
- HTTPS 探针：经代理 CONNECT 隧道访问 Cloudflare trace（返回 exit ip/loc），成功 →
  https_capable=True（消费方 https 场景的硬门槛）。备用 api.ipify.org。
- HTTP 探针：明文 absolute-URI 转发访问 http trace（http-only 代理的补充能力标记）。
- no-forward 检测：exit_ip == 代理 host → 未真正转发（透明中继/假代理），判失败。
- 劫持检测：运营商透明劫持会对「发往海外任意 IP:port 的明文请求」统一代答 —— 表现为
  大量不相干代理返回同一 exit_ip（实测 12 个阿里云代理共享 1 个出口，含 6379 端口）。
  同一轮里同一 exit_ip 被 > hijack_threshold 个不同代理声明 → 全部拒绝准入并拉黑该出口。
"""
from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from dataclasses import dataclass, field

import httpx

from .config import Settings

TRACE_KEYS_RE = {"ip": re.compile(r"(?:^|\n)ip=([\dabcdef.:]+)", re.I),
                 "loc": re.compile(r"(?:^|\n)loc=([A-Za-z]{2})")}

HTTPS_TARGETS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://one.one.one.one/cdn-cgi/trace",
    "https://api.ipify.org/?format=text",
]
HTTP_TARGETS = [
    "http://www.cloudflare.com/cdn-cgi/trace",
    "http://ip-api.com/json/?fields=query",
]


@dataclass
class ValidationResult:
    url: str
    ok: bool = False
    https_ok: bool = False
    http_ok: bool = False
    exit_ip: str = ""
    country: str = ""
    latency_ms: float = 0.0
    error: str = ""


def _extract_exit_ip(target: str, text: str):
    if "trace" in target:
        m = TRACE_KEYS_RE["ip"].search(text or "")
        loc = TRACE_KEYS_RE["loc"].search(text or "")
        return (m.group(1) if m else "", loc.group(1) if loc else "")
    if "ipify" in target:
        t = (text or "").strip()
        return (t, "") if re.fullmatch(r"[\d.]{7,15}", t) else ("", "")
    if "ip-api" in target:
        try:
            return json.loads(text).get("query", ""), ""
        except (ValueError, AttributeError):
            return "", ""
    return "", ""


def _proxy_scheme_url(host: str, port: int, protocol: str) -> str:
    return f"{protocol}://{host}:{port}" if protocol.startswith("socks") else f"http://{host}:{port}"


async def _probe(client: httpx.AsyncClient, targets):
    """按序尝试 targets，返回 (exit_ip, country, latency_ms) 或 ("", "", 0)。延迟只计成功请求。"""
    for target in targets:
        t0 = time.monotonic()
        try:
            r = await client.get(target)
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        exit_ip, country = _extract_exit_ip(target, r.text)
        if exit_ip:
            return exit_ip, country, (time.monotonic() - t0) * 1000
    return "", "", 0.0


async def validate_proxy(host: str, port: int, protocol: str, settings: Settings) -> ValidationResult:
    """对单个代理做 L7 验证。socks 验证失败会自动用 http scheme 复试一次（源常标错协议）。"""
    res = ValidationResult(url=_proxy_scheme_url(host, port, protocol))
    timeout = httpx.Timeout(settings.l7_timeout)
    attempts = [protocol]
    if protocol.startswith("socks"):
        attempts.append("http")
    for attempt in attempts:
        proxy_url = _proxy_scheme_url(host, port, attempt)
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout,
                                         verify=ssl.create_default_context(),
                                         trust_env=False, follow_redirects=False) as cli:
                exit_ip, country, ms = await _probe(cli, HTTPS_TARGETS)
                if exit_ip:
                    res.ok = exit_ip != host
                    res.https_ok = True
                    res.http_ok = True  # 能 CONNECT 的隧道同样可承载明文
                    res.exit_ip, res.country, res.latency_ms = exit_ip, country, ms
                    if exit_ip == host:
                        res.error = "no_forward:exit_equals_host"
                        res.ok = False
                    return res
                if attempt == "http" or protocol == "http":
                    exit_ip, country, ms = await _probe(cli, HTTP_TARGETS)
                    if exit_ip:
                        res.http_ok = True
                        res.ok = exit_ip != host
                        res.exit_ip, res.country, res.latency_ms = exit_ip, country, ms
                        if exit_ip == host:
                            res.error = "no_forward:exit_equals_host"
                            res.ok = False
                        return res
        except Exception as e:  # noqa: BLE001 —— 任何单代理异常（含 python-socks）都不允许上抛
            res.error = f"{type(e).__module__}.{type(e).__name__}"
            if attempt == attempts[-1]:
                return res
        if attempt != "http" and protocol.startswith("socks"):
            res.error = "socks_fail_retry_as_http"
            continue
        return res
    return res


async def tcp_reachable(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:
            pass
        return True
    except Exception:
        return False


@dataclass
class HijackGuard:
    """滚动窗口出口劫持检测：exit_ip → 声明它的不同代理 host 集合。"""
    threshold: int = 2
    window: float = 3600.0
    _map: dict = field(default_factory=dict)      # exit_ip -> {host: ts}
    blocked: dict = field(default_factory=dict)   # exit_ip -> blocked_ts

    def inspect(self, pairs) -> set:
        """pairs: [(proxy_host, exit_ip)]。返回本轮判定为劫持的代理 url 集合。"""
        now = time.time()
        for ep in [e for e, _t in self.blocked.items() if now - _t > self.window]:
            self.blocked.pop(ep, None)
        for host, exit_ip in pairs:
            if not exit_ip:
                continue
            self._map.setdefault(exit_ip, {})
            hosts = self._map[exit_ip]
            hosts[host] = now
            for h in [h for h, ts in hosts.items() if now - ts > self.window]:
                hosts.pop(h, None)
            if len(hosts) > self.threshold:
                self.blocked[exit_ip] = now
        bad = set()
        for host, exit_ip in pairs:
            if exit_ip and exit_ip in self.blocked:
                bad.add(host)
        return bad

    def is_blocked(self, exit_ip: str) -> bool:
        return exit_ip in self.blocked
