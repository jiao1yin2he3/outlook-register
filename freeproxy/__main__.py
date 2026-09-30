"""CLI：serve（默认）| check <proxy_url> | fetch-once"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from urllib.parse import urlsplit

from .config import load


def main(argv=None):
    parser = argparse.ArgumentParser(prog="freeproxy", description="独立免费代理池")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="启动服务（默认）")
    p_check = sub.add_parser("check", help="单点深度验证一个代理")
    p_check.add_argument("proxy_url", help="如 http://1.2.3.4:8080 或 socks5://5.6.7.8:1080")
    p_check.add_argument("--tcp-only", action="store_true", help="只做 TCP 预检")
    p_fetch = sub.add_parser("fetch-once", help="抓取一次源，打印统计")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd in (None, "serve"):
        from .server import run
        run()
        return 0

    if args.cmd == "check":
        from .validator import tcp_reachable, validate_proxy
        u = urlsplit(args.proxy_url)
        if not u.hostname or not u.port:
            print("无效代理 URL", file=sys.stderr)
            return 2
        proto = "socks5" if u.scheme.startswith("socks") else "http"

        async def _go():
            if args.tcp_only:
                ok = await tcp_reachable(u.hostname, u.port, load().tcp_timeout)
                print(json.dumps({"tcp": ok}))
                return
            r = await validate_proxy(u.hostname, u.port, proto, load())
            print(json.dumps({"url": r.url, "ok": r.ok, "https_ok": r.https_ok,
                              "http_ok": r.http_ok, "exit_ip": r.exit_ip,
                              "country": r.country, "latency_ms": round(r.latency_ms),
                              "error": r.error}, ensure_ascii=False, indent=2))
        asyncio.run(_go())
        return 0

    if args.cmd == "fetch-once":
        from .engine import Engine
        from .pool import ProxyPool
        from .store import Store

        async def _go():
            s = load()
            s.loops_enabled = False
            pool = ProxyPool(s)
            engine = Engine(pool, Store(":memory:"), s)
            stats = await engine.fetch_once()
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        asyncio.run(_go())
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
