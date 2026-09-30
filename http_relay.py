from __future__ import annotations

"""极简本地 HTTP 代理 → 远端带认证 SOCKS5 中继。

Chromium 支持 HTTP 代理但不支持 SOCKS5 认证。
本模块实现一个本地无认证 HTTP 代理，把所有请求转发到带认证的远端 SOCKS5。
"""

import socket
import threading
import select
import time
from typing import Optional
from urllib.parse import urlparse

import socks

from logger import logger


def _create_socks5_socket(host: str, port: int, user: str, passwd: str) -> socks.socksocket:
    """创建一个通过带认证 SOCKS5 代理连接的 socket。"""
    s = socks.socksocket()
    s.set_proxy(socks.SOCKS5, host, port, username=user, password=passwd)
    return s


def _tunnel(a_socket, b_socket, timeout: float = 300):
    """双向管道 a ↔ b，阻塞直到任一端关闭或超时。"""
    a_socket.setblocking(False)
    b_socket.setblocking(False)
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r, _, _ = select.select([a_socket, b_socket], [], [], min(remaining, 5))
            if not r:
                continue
            for sock in r:
                try:
                    data = sock.recv(32768)
                except Exception:
                    return
                if not data:
                    return
                other = b_socket if sock is a_socket else a_socket
                try:
                    other.sendall(data)
                except Exception:
                    return
    except Exception:
        pass


class HttpRelay:
    """本地无认证 HTTP/HTTPS 代理 → 远端带认证 SOCKS5。

    用法：
        r = HttpRelay("socks5://user:pass@ip:port")
        r.start()
        # Chromium: --proxy-server=http://127.0.0.1:<r.port>
        r.stop()
    """

    def __init__(self, upstream_url: str):
        parsed = urlparse(upstream_url)
        self._user = parsed.username or ""
        self._pass = parsed.password or ""
        self._host = parsed.hostname or ""
        self._port = parsed.port or 1080
        self._local_port: Optional[int] = None
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    @property
    def port(self) -> int:
        return self._local_port or 0

    @property
    def upstream_ip(self) -> str:
        return self._host

    def start(self) -> bool:
        if self._running:
            return True
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._local_port = self._server.getsockname()[1]
        self._server.listen(5)
        self._server.settimeout(1)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        logger.info(f"[HttpRelay] :{self._local_port} -> socks5://***@{self._host}:{self._port}")
        return True

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
        self._local_port = None

    def _accept_loop(self):
        while self._running:
            try:
                client, _ = self._server.accept()
                t = threading.Thread(target=self._handle, args=(client,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _handle(self, client: socket.socket):
        """处理一个 HTTP/HTTPS 代理请求。"""
        try:
            client.settimeout(60)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = client.recv(4096)
                if not chunk:
                    return
                request += chunk
                if len(request) > 65536:
                    return

            first_line = request.split(b"\r\n")[0].decode(errors="ignore")
            parts = first_line.split()
            if len(parts) < 2:
                return

            method, path = parts[0], parts[1]

            if method == "CONNECT":
                # HTTPS: 隧道模式
                host, port_str = path.split(":")
                target_port = int(port_str)
                try:
                    remote = _create_socks5_socket(self._host, self._port, self._user, self._pass)
                    remote.settimeout(15)
                    remote.connect((host, target_port))
                except Exception:
                    try:
                        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    except Exception:
                        pass
                    return
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                _tunnel(client, remote)
            else:
                # HTTP: 转发模式
                parsed = urlparse(path)
                if not parsed.hostname:
                    return
                target_host = parsed.hostname
                target_port = parsed.port or 80
                try:
                    remote = _create_socks5_socket(self._host, self._port, self._user, self._pass)
                    remote.settimeout(15)
                    remote.connect((target_host, target_port))
                except Exception:
                    try:
                        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    except Exception:
                        pass
                    return
                # 重写请求行（去掉 scheme，用相对路径）
                new_path = parsed.path or "/"
                if parsed.query:
                    new_path += "?" + parsed.query
                modified = request.replace(
                    path.encode(),
                    new_path.encode(),
                    1,
                )
                # 添加 Host 头（如果不存在）
                if b"\r\nHost:" not in modified:
                    modified = modified.replace(
                        b"\r\n\r\n",
                        f"\r\nHost: {target_host}\r\n\r\n".encode(),
                    )
                try:
                    remote.sendall(modified)
                except Exception:
                    return
                _tunnel(client, remote)
        except Exception:
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass
