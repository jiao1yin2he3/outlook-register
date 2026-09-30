"""内存池：准入/选择/退避/统计。持久化由 Store 负责，本模块只标脏。"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from .config import Settings
from .models import DAY, STATE_ACTIVE, STATE_CANDIDATE, STATE_NEW, Proxy


def _mask(url: str) -> str:
    """观测面脱敏：隐藏可能存在的 user:pass 凭据。"""
    try:
        u = urlsplit(url)
        host = u.hostname or url
        return f"{u.scheme}://{host}:{u.port}" if u.port else f"{u.scheme}://{host}"
    except (ValueError, TypeError):
        return url[:24]


class ProxyPool:
    def __init__(self, settings: Settings):
        self.s = settings
        self.proxies: dict[str, Proxy] = {}
        # 说明：临界区内均无 await，单事件循环下无需 asyncio.Lock（3.9 下还会绑定错循环）
        self._dirty: set[str] = set()
        self._sticky: dict[str, tuple[str, float]] = {}
        self.stats = {
            "ingested": 0, "tcp_passed": 0, "validated_ok": 0, "validated_fail": 0,
            "hijacked": 0, "served": 0, "reported_ok": 0, "reported_fail": 0,
        }
        self.last_fetch_stats: dict = {}

    # ---- 准入 ----
    async def ingest(self, triplets) -> int:
        """triplets: [(host, port, protocol)]。新增返回数；已知 url 忽略（保留历史状态）。"""
        added = 0
        now = time.time()
        for host, port, proto in triplets:
            scheme = "socks5" if proto == "socks5" else "http"
            url = f"{scheme}://{host}:{port}"
            if url in self.proxies:
                continue
            self.proxies[url] = Proxy(url=url, protocol=scheme, added_at=now)
            self._dirty.add(url)
            added += 1
        self.stats["ingested"] += added
        self._enforce_cap()
        return added

    async def import_urls(self, urls: list[str]) -> int:
        """手工导入（支持凭据/任意 scheme），直接进入候选队列。"""
        added = 0
        now = time.time()
        for raw in urls:
            url = raw.strip()
            if not url or url in self.proxies:
                continue
            u = urlsplit(url)
            proto = "socks5" if u.scheme.startswith("socks") else "http"
            self.proxies[url] = Proxy(url=url, protocol=proto, added_at=now)
            self._dirty.add(url)
            added += 1
        self.stats["ingested"] += added
        return added

    def _enforce_cap(self):
        if len(self.proxies) <= self.s.max_entries:
            return
        worst = sorted(
            (p for p in self.proxies.values()
             if p.state != STATE_ACTIVE),
            key=lambda p: (p.score, p.fail_count, p.added_at),
        )
        for p in worst[: len(self.proxies) - self.s.max_entries]:
            self.proxies.pop(p.url, None)

    # ---- 验证结果回填 ----
    def apply_validation(self, results, hijacked_urls: set):
        """results: [ValidationResult]。hijacked_urls: 判定劫持的代理 host 集合。"""
        now = time.time()
        s = self.s
        for r in results:
            p = self.proxies.get(r.url)
            if p is None:
                continue
            if r.url in hijacked_urls:
                self.stats["hijacked"] += 1
                p.on_validated(False, False, 0, "", "", "hijack_suspect", now)
                p.next_check_at = now + s.candidate_backoff(min(p.fail_streak, 6) or 1)
                p.last_error = "hijack_suspect"
                self._dirty.add(p.url)
                continue
            p.on_validated(r.ok, r.https_ok, r.latency_ms, r.exit_ip, r.country, r.error, now)
            if r.ok:
                self.stats["validated_ok"] += 1
            else:
                self.stats["validated_fail"] += 1
                p.next_check_at = now + s.candidate_backoff(p.fail_streak)
            self._dirty.add(p.url)

    # ---- 候选领取 ----
    def take_tcp_candidates(self, limit: int) -> list[Proxy]:
        now = time.time()
        picked = [p for p in self.proxies.values()
                  if p.state == STATE_NEW and p.next_check_at <= now]
        picked.sort(key=lambda p: (p.next_check_at, p.added_at))
        return picked[:limit]

    def take_due_recheck(self, limit: int) -> list[Proxy]:
        now = time.time()
        active = [p for p in self.proxies.values()
                  if p.state == STATE_ACTIVE and now - p.last_checked_at >= self.s.recheck_ttl]
        candidate = [p for p in self.proxies.values()
                     if p.state == STATE_CANDIDATE and p.next_check_at <= now]
        active.sort(key=lambda p: p.last_checked_at)      # 最久未验优先
        candidate.sort(key=lambda p: p.next_check_at)
        merged, seen = [], set()
        for p in active + candidate:
            if p.url not in seen:
                seen.add(p.url)
                merged.append(p)
        return merged[:limit]

    # ---- 服务面 ----
    async def select(self, protocol: str | None = None, https_only: bool = False,
                     country: str | None = None, session: str | None = None) -> dict | None:
        """取一个可服务代理。临界区内无 await，单事件循环下天然原子。"""
        now = time.time()
        # 粘滞命中
        if session:
            hit = self._sticky.get(session)
            if hit and (now - hit[1]) <= self.s.sticky_ttl:
                p = self.proxies.get(hit[0])
                if p and p.is_fresh(now, self.s.serve_ttl) and not p.is_cooling(now) \
                        and (not https_only or p.https_capable) \
                        and (not protocol or p.protocol == protocol) \
                        and (not country or p.country == country.upper()):
                    self._serve(p, now)
                    return self._served_payload(p, now)
            self._sticky.pop(session, None)

        serveable = [
            p for p in self.proxies.values()
            if p.is_fresh(now, self.s.serve_ttl)
            and not p.is_cooling(now)
            and (not https_only or p.https_capable)
            and (not protocol or p.protocol == protocol)
            and (not country or p.country == country.upper())
        ]
        if not serveable:
            return None

        def rank(p: Proxy):
            idle = min(now - (p.last_served_at or 0), 3600)
            return (round(p.score, 3), -p.latency_ewma, idle)

        pick = max(serveable, key=rank)
        self._serve(pick, now)
        if session:
            if len(self._sticky) > 10000:
                for k in [k for k, v in self._sticky.items() if now - v[1] > self.s.sticky_ttl]:
                    self._sticky.pop(k, None)
            self._sticky[session] = (pick.url, now)
        return self._served_payload(pick, now)

    def _serve(self, p: Proxy, now: float):
        p.roll_day(now)
        p.use_count += 1
        p.last_served_at = now
        self.stats["served"] += 1
        self._dirty.add(p.url)

    def _served_payload(self, p: Proxy, now: float) -> dict:
        return {"proxy": p.url, "protocol": p.protocol, "https_capable": p.https_capable,
                "exit_ip": p.exit_ip, "country": p.country,
                "latency_ms": round(p.latency_ewma) if p.latency_ewma else None,
                "score": round(p.score, 3),
                "age_since_check": round(now - p.last_checked_at, 1)}

    # ---- 业务反馈 ----
    async def report(self, url: str, ok: bool, latency_ms: float = 0.0, error: str = "") -> bool:
        p = self.proxies.get(url)
        if p is None:
            return False
        now = time.time()
        p.on_report(ok, latency_ms, self.s.cooldown_for(p.fail_streak), now)
        if ok:
            self.stats["reported_ok"] += 1
        else:
            self.stats["reported_fail"] += 1
        self._dirty.add(url)
        return True

    # ---- 观测 ----
    def snapshot_stats(self) -> dict:
        now = time.time()
        total = len(self.proxies)
        fresh = sum(1 for p in self.proxies.values() if p.is_fresh(now, self.s.serve_ttl))
        fresh_https = sum(1 for p in self.proxies.values()
                          if p.is_fresh(now, self.s.serve_ttl) and p.https_capable)
        return {
            "total": total,
            "by_state": {
                "new": sum(1 for p in self.proxies.values() if p.state == STATE_NEW),
                "active": sum(1 for p in self.proxies.values() if p.state == STATE_ACTIVE),
                "candidate": sum(1 for p in self.proxies.values() if p.state == STATE_CANDIDATE),
            },
            "serveable": fresh,
            "serveable_https": fresh_https,
            "countries": dict(_top_countries(self.proxies.values())),
            "counters": dict(self.stats),
            "last_fetch": self.last_fetch_stats,
        }

    def list_items(self, protocol=None, https_only=False, alive_only=True, limit=50) -> list[dict]:
        now = time.time()
        items = []
        for p in sorted(self.proxies.values(), key=lambda p: -(p.score * 1000) - (1 if p.last_ok_at else 0)):
            if protocol and p.protocol != protocol:
                continue
            if https_only and not p.https_capable:
                continue
            if alive_only and not (p.is_fresh(now, self.s.serve_ttl) or p.state == STATE_ACTIVE):
                continue
            snap = p.snapshot(now)
            snap["url"] = _mask(p.url)
            items.append(snap)
            if len(items) >= max(1, min(limit, 500)):
                break
        return items

    def alive_urls(self, https_only: bool, limit: int) -> list[str]:
        now = time.time()
        out = [p.url for p in self.proxies.values()
               if p.is_fresh(now, self.s.serve_ttl) and (not https_only or p.https_capable)]
        out.sort(key=lambda u: -(self.proxies[u].score * 1000))
        return out[:max(1, min(limit, 2000))]

    def dirty_proxies(self) -> list[Proxy]:
        out = [self.proxies[u] for u in self._dirty if u in self.proxies]
        self._dirty.clear()
        return out

    def mark_checked_pending(self, proxies: list[Proxy]):
        """领取后立刻打 last_checked 标记，防止并发轮重复领取（落盘时一并写回）。"""
        now = time.time()
        for p in proxies:
            p.last_checked_at = now
            self._dirty.add(p.url)

    def mark_tcp_failed(self, proxies: list[Proxy]):
        """TCP 预检失败：计入失败并按候选退避推迟重试（保持 new 态，后续仍走 TCP 通道重验）。"""
        now = time.time()
        for p in proxies:
            p.fail_count += 1
            p.fail_streak += 1
            p.score = 0.7 * p.score
            p.next_check_at = now + self.s.candidate_backoff(p.fail_streak)
            p.last_error = "tcp_unreachable"
            self._dirty.add(p.url)
        self.stats["tcp_failed"] = self.stats.get("tcp_failed", 0) + len(proxies)


def _top_countries(proxies, n=10):
    counter: dict[str, int] = {}
    for p in proxies:
        if p.state == STATE_ACTIVE and p.country:
            counter[p.country] = counter.get(p.country, 0) + 1
    return sorted(counter.items(), key=lambda kv: -kv[1])[:n]
