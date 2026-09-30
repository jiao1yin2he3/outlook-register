"""SQLite 持久化（WAL）：定期批量落盘 + 启动恢复。"""
from __future__ import annotations

import asyncio
import os
import sqlite3

from .models import PROXY_COLUMNS, Proxy, row_to_proxy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proxies (
    url TEXT PRIMARY KEY,
    protocol TEXT NOT NULL DEFAULT 'http',
    added_at REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'new',
    last_checked_at REAL NOT NULL DEFAULT 0,
    last_ok_at REAL NOT NULL DEFAULT 0,
    next_check_at REAL NOT NULL DEFAULT 0,
    https_capable INTEGER NOT NULL DEFAULT 0,
    exit_ip TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    latency_ewma REAL NOT NULL DEFAULT 0,
    ok_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    fail_streak INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0.5,
    use_count INTEGER NOT NULL DEFAULT 0,
    day_key INTEGER NOT NULL DEFAULT 0,
    last_served_at REAL NOT NULL DEFAULT 0,
    cooldown_until REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_proxies_state ON proxies(state, last_ok_at DESC);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None

    def _connect(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        self._conn = conn

    async def load_all(self) -> list[Proxy]:
        if self._conn is None:
            await asyncio.to_thread(self._connect)
        def _q():
            cur = self._conn.execute(f"SELECT {','.join(PROXY_COLUMNS)} FROM proxies")
            return [row_to_proxy(r) for r in cur.fetchall()]
        return await asyncio.to_thread(_q)

    async def upsert(self, proxies: list[Proxy]):
        if not proxies or self._conn is None:
            return
        cols = ",".join(PROXY_COLUMNS)
        placeholders = ",".join("?" for _ in PROXY_COLUMNS)
        sql = f"INSERT OR REPLACE INTO proxies ({cols}) VALUES ({placeholders})"
        def _w():
            with self._conn:
                self._conn.executemany(sql, [tuple(getattr(p, c) for c in PROXY_COLUMNS) for p in proxies])
        await asyncio.to_thread(_w)

    async def close(self):
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
