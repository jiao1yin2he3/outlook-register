"""后台引擎：抓取循环 → 验证循环（TCP+L7）→ 复检循环 → 落盘循环。"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import sources as src_mod
from .config import Settings
from .pool import ProxyPool
from .store import Store
from .validator import HijackGuard, tcp_reachable, validate_proxy

log = logging.getLogger("freeproxy.engine")


class Engine:
    def __init__(self, pool: ProxyPool, store: Store, settings: Settings):
        self.pool = pool
        self.store = store
        self.s = settings
        self.guard = HijackGuard(threshold=settings.hijack_threshold)
        self._tasks: list[asyncio.Task] = []
        # 3.9 兼容：asyncio.Semaphore 构造期绑定事件循环，必须延迟到协程内创建
        self._fetch_sem: asyncio.Semaphore | None = None

    def start(self):
        self._tasks = [
            asyncio.create_task(self._fetch_loop(), name="fetch"),
            asyncio.create_task(self._validate_loop(), name="validate"),
            asyncio.create_task(self._recheck_loop(), name="recheck"),
            asyncio.create_task(self._flush_loop(), name="flush"),
        ]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        await self.store.upsert(self.pool.dirty_proxies())

    # ---- 抓取 ----
    async def _fetch_loop(self):
        while True:
            try:
                await self.fetch_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("抓取循环异常: %s", e)
            await asyncio.sleep(self.s.refresh_interval)

    async def fetch_once(self) -> dict:
        srcs = src_mod.load_sources()
        results = await asyncio.gather(*(self._fetch_source(s) for s in srcs))
        triplets, per_source = [], {}
        for (name, items, err) in results:
            per_source[name] = {"count": len(items), "error": err}
            triplets.extend(items)
        added = await self.pool.ingest(triplets)
        stats = {"at": time.time(), "sources": per_source,
                 "raw": len(triplets), "added": added, "pool": len(self.pool.proxies)}
        self.pool.last_fetch_stats = stats
        log.info("源抓取: raw=%d added=%d pool=%d", len(triplets), added, len(self.pool.proxies))
        return stats

    async def _fetch_source(self, src):
        name, url, fmt, proto = src
        try:
            if self._fetch_sem is None:
                self._fetch_sem = asyncio.Semaphore(self.s.fetch_concurrency)
            async with self._fetch_sem:
                async with httpx.AsyncClient(
                        timeout=httpx.Timeout(self.s.fetch_timeout),
                        headers={"User-Agent": "Mozilla/5.0 (compatible; freeproxy-pool/1.0)"},
                        proxy=self.s.fetch_proxy or None, trust_env=False) as cli:
                    r = await cli.get(url)
            if r.status_code != 200:
                return name, [], f"http_{r.status_code}"
            return name, src_mod.parse_payload(r.text, fmt, proto), ""
        except Exception as e:
            return name, [], type(e).__name__

    # ---- 新候选验证：TCP 预检 → L7 ----
    async def _validate_loop(self):
        while True:
            try:
                batch = self.pool.take_tcp_candidates(self.s.candidates_per_round)
                if not batch:
                    await asyncio.sleep(2)
                    continue
                self.pool.mark_checked_pending(batch)
                tcp_alive = await self._tcp_precheck(batch)
                alive_ids = {id(p) for p in tcp_alive}
                tcp_dead = [p for p in batch if id(p) not in alive_ids]
                if tcp_dead:
                    self.pool.mark_tcp_failed(tcp_dead)
                log.info("TCP 预检: %d/%d 通过", len(tcp_alive), len(batch))
                await self._l7_round(tcp_alive, recheck=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("验证循环异常: %s", e)
                await asyncio.sleep(5)

    # ---- 复检：活跃代理保鲜 + 候选退避重试 ----
    async def _recheck_loop(self):
        while True:
            try:
                # 先干活再睡：启动时立即复检到期代理，消除重启后的服务空窗
                batch = self.pool.take_due_recheck(self.s.recheck_batch)
                if batch:
                    self.pool.mark_checked_pending(batch)
                    await self._l7_round(batch, recheck=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("复检循环异常: %s", e)
            await asyncio.sleep(self.s.recheck_interval)

    async def _tcp_precheck(self, batch) -> list:
        sem = asyncio.Semaphore(self.s.tcp_concurrency)

        async def one(p):
            from urllib.parse import urlsplit
            u = urlsplit(p.url)
            ok = await tcp_reachable(u.hostname, u.port, self.s.tcp_timeout)
            if ok:
                self.pool.stats["tcp_passed"] += 1
            return p if ok else None

        results = await asyncio.gather(*(one(p) for p in batch))
        return [p for p in results if p is not None]

    async def _l7_round(self, proxies, recheck: bool):
        """L7 深度验证一轮 + 劫持聚合 + 回填。"""
        sem = asyncio.Semaphore(self.s.l7_concurrency)

        async def one(p):
            from urllib.parse import urlsplit
            u = urlsplit(p.url)
            async with sem:
                return await validate_proxy(u.hostname, u.port, p.protocol, self.s)

        results = await asyncio.gather(*(one(p) for p in proxies))
        pairs = []
        from urllib.parse import urlsplit
        for p, r in zip(proxies, results):
            if r.ok and r.exit_ip:
                pairs.append((urlsplit(p.url).hostname, r.exit_ip))
        hijacked = self.guard.inspect(pairs) if pairs else set()
        if hijacked:
            log.warning("劫持检测: %d 个代理共享可疑出口，拒绝准入", len(hijacked))
        self.pool.apply_validation(results, hijacked)
        ok_n = sum(1 for r in results if r.ok)
        log.info("%s L7: %d/%d 通过 (劫持拦截 %d)", "复检" if recheck else "验证",
                 ok_n, len(results), len(hijacked))

    # ---- 落盘 ----
    async def _flush_loop(self):
        while True:
            try:
                await asyncio.sleep(10)
                dirty = self.pool.dirty_proxies()
                if dirty:
                    await self.store.upsert(dirty)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("落盘异常: %s", e)
