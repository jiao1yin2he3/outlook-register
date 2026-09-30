"""本地 SOCKS5 中继模块。

Chromium 不支持带认证的 SOCKS5 代理。本模块在本地启动一个无认证的
SOCKS5 服务，把流量转发到带认证的远端 SOCKS5，让 Chromium 通过本地
中继访问远端代理。

每个 Relay 实例独占一个本地端口和独立的事件循环线程，互不干扰。
"""

import asyncio
import socket
import threading
from typing import Optional, Tuple
from urllib.parse import urlparse, unquote

import pproxy

from logger import logger


def _parse_upstream(proxy_url: str) -> Tuple[str, str, str, int]:
    """解析 socks5://user:pass@ip:port 格式的代理 URL。

    Returns:
        (user, password, ip, port)
    """
    parsed = urlparse(proxy_url)
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    ip = parsed.hostname or ""
    port = int(parsed.port or 0)
    return user, password, ip, port


def _find_free_port() -> int:
    """让 OS 分配一个空闲端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Relay:
    """本地无认证 SOCKS5 → 远端带认证 SOCKS5 的中继。

    用法：
        r = Relay("socks5://user:pass@1.2.3.4:5678")
        r.start()              # 阻塞直到端口监听
        local_url = r.local_url  # socks5://127.0.0.1:<port>
        ...
        r.stop()
    """

    def __init__(self, upstream_url: str):
        self._upstream_url = upstream_url
        self._user, self._pass, self._ip, self._port = _parse_upstream(upstream_url)
        self._local_port: Optional[int] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None  # asyncio.Server
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    @property
    def local_url(self) -> str:
        return f"socks5://127.0.0.1:{self._local_port}"

    @property
    def upstream_ip(self) -> str:
        return self._ip

    def start(self, timeout: float = 5.0) -> bool:
        """启动中继，阻塞直到本地端口监听或超时。"""
        if self._thread is not None:
            return True

        self._local_port = _find_free_port()
        # pproxy 用 # 分隔 user 和 password（: 会被当作 cipher 字段）
        server = pproxy.Server(f"socks5://127.0.0.1:{self._local_port}")
        remote = pproxy.Connection(f"socks5://{self._user}#{self._pass}@{self._ip}:{self._port}")

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._server = self._loop.run_until_complete(
                    server.start_server({"rserver": [remote], "authtime": 0})
                )
            except Exception as e:
                logger.error(f"[Relay] 启动失败: {e}")
                return
            self._started.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return self._started.wait(timeout=timeout)

    def stop(self) -> None:
        """停止中继，关闭本地服务。"""
        if self._loop is None:
            return

        if self._server is not None:
            try:
                self._loop.call_soon_threadsafe(self._server.close)
            except Exception:
                pass

        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self._server = None
        self._loop = None
        self._local_port = None
