"""HTTP 服务面（FastAPI）：
GET  /proxy      取一个可用代理（可按协议/国家/会话粘滞过滤）
GET  /proxies    列表（脱敏展示）
POST /report     业务反馈（成功/失败），失败进入递增冷却 —— 稳定性的另一根支柱
POST /import     手工导入私有代理
GET  /download   导出存活代理列表
GET  /stats      池状态
GET  /health     健康检查（免鉴权）
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from . import __version__
from .config import Settings, load
from .engine import Engine
from .pool import ProxyPool
from .store import Store

log = logging.getLogger("freeproxy.server")


def create_app(settings: Settings | None = None,
               pool: ProxyPool | None = None,
               engine: Engine | None = None) -> FastAPI:
    s = settings or load()
    pool = pool or ProxyPool(s)
    store = Store(s.db_path)
    engine = engine or Engine(pool, store, s)
    state = {"ready": False}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if s.loops_enabled:
            loaded = await store.load_all()
            # 恢复历史：active 的保留状态，但需复检后才可服务（serve_ttl 兜底 freshness）
            for p in loaded:
                pool.proxies[p.url] = p
            log.info("从 %s 恢复 %d 条代理", s.db_path, len(loaded))
            engine.start()
        state["ready"] = True
        yield
        await engine.stop()

    app = FastAPI(title="freeproxy-pool", version=__version__, lifespan=lifespan)

    def _auth(x_api_key: str | None):
        if s.api_key and x_api_key != s.api_key:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/health")
    async def health():
        return {"ok": True, "version": __version__, "ready": state["ready"]}

    @app.get("/proxy")
    async def get_proxy(
        protocol: Optional[str] = Query(None, description="http | socks5"),
        https: bool = Query(True, description="只返回支持 HTTPS 隧道的代理"),
        country: Optional[str] = Query(None, description="ISO 国家码，如 US"),
        session: Optional[str] = Query(None, description="会话粘滞：同 session 粘滞窗口内返回同一代理"),
        x_api_key: Optional[str] = Header(default=None),
    ):
        _auth(x_api_key)
        result = await pool.select(protocol=protocol, https_only=https,
                                   country=country, session=session)
        if result is None:
            st = pool.snapshot_stats()
            raise HTTPException(
                status_code=404,
                detail={"error": "no_proxy_ready",
                        "serveable": st["serveable"], "serveable_https": st["serveable_https"],
                        "total": st["total"],
                        "hint": "池在预热或当前网络视角下无可用出口；稍后重试或检查 /stats"})

        return result

    @app.get("/proxies")
    async def list_proxies(
        limit: int = Query(50, ge=1, le=500),
        protocol: Optional[str] = None,
        https: bool = Query(True),
        alive_only: bool = Query(True),
        x_api_key: Optional[str] = Header(default=None),
    ):
        _auth(x_api_key)
        return {"items": pool.list_items(protocol=protocol, https_only=https,
                                         alive_only=alive_only, limit=limit)}

    @app.post("/report")
    async def report(payload: dict, x_api_key: Optional[str] = Header(default=None)):
        _auth(x_api_key)
        url = str(payload.get("proxy") or "")
        if not url:
            raise HTTPException(status_code=422, detail="missing 'proxy'")
        ok = bool(payload.get("ok"))
        latency = float(payload.get("latency_ms") or 0)
        updated = await pool.report(url, ok, latency, str(payload.get("error") or ""))
        return {"updated": updated}

    @app.post("/import")
    async def import_(payload: dict, x_api_key: Optional[str] = Header(default=None)):
        _auth(x_api_key)
        urls = payload.get("urls") or []
        if not isinstance(urls, list) or not urls:
            raise HTTPException(status_code=422, detail="missing 'urls' array")
        added = await pool.import_urls([str(u) for u in urls])
        return {"added": added}

    @app.get("/download", response_class=PlainTextResponse)
    async def download(
        format: str = Query("txt", description="txt | json"),
        https_only: bool = Query(True),
        limit: int = Query(500, ge=1, le=2000),
        x_api_key: Optional[str] = Header(default=None),
    ):
        _auth(x_api_key)
        urls = pool.alive_urls(https_only=https_only, limit=limit)
        if format == "json":
            import json
            return PlainTextResponse(json.dumps(urls), media_type="application/json")
        return PlainTextResponse("\n".join(urls))

    @app.get("/stats")
    async def stats(x_api_key: Optional[str] = Header(default=None)):
        _auth(x_api_key)
        return pool.snapshot_stats()

    return app


def run():
    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load()
    app = create_app(s)
    uvicorn.run(app, host=s.host, port=s.port, log_level=s.log_level.lower())
