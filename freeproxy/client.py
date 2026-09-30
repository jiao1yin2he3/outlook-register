"""零依赖同步客户端：其他项目三行接入。

    from freeproxy.client import ProxyClient
    pc = ProxyClient("http://127.0.0.1:8300")
    proxy = pc.url(protocol="https", session="my-job")   # -> "http://1.2.3.4:8080"
    ... 用 proxy 发请求 ...
    pc.report(proxy, ok=True, latency_ms=450)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class NoProxyAvailable(RuntimeError):
    pass


class ProxyClient:
    def __init__(self, base_url: str, api_key: str = "", timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(self.base + path, method=method)
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=self.timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(body).get("detail", body)
            except ValueError:
                detail = body
            if e.code == 404 and isinstance(detail, dict):
                raise NoProxyAvailable(str(detail)) from None
            raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from None

    def get(self, **params) -> dict:
        """取一个可用代理，返回 {proxy, https_capable, exit_ip, country, latency_ms, score}。"""
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        return self._request("GET", "/proxy" + ("?" + qs if qs else ""))

    def url(self, **params) -> str:
        return self.get(**params)["proxy"]

    def report(self, proxy: str, ok: bool, latency_ms: float = 0.0, error: str = ""):
        return self._request("POST", "/report",
                             {"proxy": proxy, "ok": ok, "latency_ms": latency_ms, "error": error})

    def stats(self) -> dict:
        return self._request("GET", "/stats")

    def download(self, https_only: bool = True, limit: int = 500) -> list:
        qs = f"?format=json&https_only={str(https_only).lower()}&limit={limit}"
        return self._request("GET", "/download" + qs)
