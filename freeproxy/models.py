"""Proxy 数据模型：状态机 new → active ⇄ cooldown →(streak 超限) candidate(退避)。"""
from __future__ import annotations

import time
from dataclasses import dataclass

DAY = 86400

# state 取值：
#   new       已抓到、尚未通过 L7 验证
#   active    通过 L7，可被服务
#   candidate 首验失败或连续失败，按 next_check_at 指数退避等待重验
STATE_NEW, STATE_ACTIVE, STATE_CANDIDATE = "new", "active", "candidate"


@dataclass
class Proxy:
    url: str                       # http://host:port 或 socks5://host:port（验证时确定的最终形态）
    protocol: str = "http"         # http | socks5（源声明/探测校正后的协议）
    added_at: float = 0.0
    state: str = STATE_NEW
    # 验证
    last_checked_at: float = 0.0
    last_ok_at: float = 0.0
    next_check_at: float = 0.0
    https_capable: bool = False    # 通过 HTTPS（CONNECT 隧道）探针 —— 消费方多数场景的硬要求
    exit_ip: str = ""
    country: str = ""
    latency_ewma: float = 0.0      # ms
    # 计数与健康
    ok_count: int = 0
    fail_count: int = 0
    fail_streak: int = 0
    score: float = 0.5             # EWMA 健康分 [0,1]，初值中性
    # 使用
    use_count: int = 0
    day_key: int = 0
    last_served_at: float = 0.0
    cooldown_until: float = 0.0
    last_error: str = ""

    def __post_init__(self):
        if not self.added_at:
            self.added_at = time.time()
        if not self.day_key:
            self.day_key = int(time.time() / DAY)

    # ---- 生命周期 ----
    def is_fresh(self, now: float, serve_ttl: float) -> bool:
        return self.state == STATE_ACTIVE and (now - self.last_ok_at) <= serve_ttl

    def is_cooling(self, now: float) -> bool:
        return now < self.cooldown_until

    def roll_day(self, now: float):
        day = int(now / DAY)
        if day != self.day_key:
            self.day_key = day
            self.use_count = 0

    def on_validated(self, ok: bool, https: bool, latency_ms: float,
                     exit_ip: str, country: str, error: str, now: float | None = None):
        now = now if now is not None else time.time()
        self.last_checked_at = now
        self.last_error = error or ""
        if ok:
            self.state = STATE_ACTIVE
            self.ok_count += 1
            self.fail_streak = 0
            self.next_check_at = 0.0
            self.last_ok_at = now
            self.https_capable = https or self.https_capable
            if exit_ip:
                self.exit_ip = exit_ip
            if country:
                self.country = country
            self.latency_ewma = latency_ms if not self.latency_ewma else 0.5 * self.latency_ewma + 0.5 * latency_ms
            self.score = 0.7 * self.score + 0.3 * 1.0
        else:
            self.fail_count += 1
            self.fail_streak += 1
            self.score = 0.7 * self.score + 0.3 * 0.0
            if self.state == STATE_ACTIVE:
                # 曾经验证通过：降级回候选（保留历史与能力标记），退避重验
                self.state = STATE_CANDIDATE
                self.https_capable = False if not self.ok_count else self.https_capable
            else:
                self.state = STATE_CANDIDATE

    def on_report(self, ok: bool, latency_ms: float, cooldown_seconds: int, now: float | None = None):
        """消费方业务反馈：比验证探针更真实的信号。失败进入递增冷却。"""
        now = now if now is not None else time.time()
        if ok:
            self.fail_streak = 0
            self.score = 0.7 * self.score + 0.3 * 1.0
            self.last_ok_at = max(self.last_ok_at, now)
            if latency_ms > 0:
                self.latency_ewma = latency_ms if not self.latency_ewma else 0.5 * self.latency_ewma + 0.5 * latency_ms
        else:
            self.fail_count += 1
            self.fail_streak += 1
            self.score = 0.7 * self.score + 0.3 * 0.0
            self.cooldown_until = now + cooldown_seconds

    def snapshot(self, now: float) -> dict:
        return {
            "url": self.url,
            "protocol": self.protocol,
            "state": self.state,
            "https_capable": self.https_capable,
            "country": self.country,
            "exit_ip": self.exit_ip,
            "latency_ms": round(self.latency_ewma) if self.latency_ewma else None,
            "score": round(self.score, 3),
            "ok_count": self.ok_count,
            "fail_count": self.fail_count,
            "fail_streak": self.fail_streak,
            "use_count": self.use_count,
            "cooling": self.is_cooling(now),
            "last_ok_ago": round(now - self.last_ok_at) if self.last_ok_at else None,
            "last_error": self.last_error,
        }


def row_to_proxy(row) -> Proxy:
    return Proxy(
        url=row[0], protocol=row[1], added_at=row[2], state=row[3],
        last_checked_at=row[4], last_ok_at=row[5], next_check_at=row[6],
        https_capable=bool(row[7]), exit_ip=row[8], country=row[9],
        latency_ewma=row[10], ok_count=row[11], fail_count=row[12],
        fail_streak=row[13], score=row[14], use_count=row[15], day_key=row[16],
        last_served_at=row[17], cooldown_until=row[18], last_error=row[19] or "",
    )


PROXY_COLUMNS = ("url", "protocol", "added_at", "state", "last_checked_at", "last_ok_at",
                 "next_check_at", "https_capable", "exit_ip", "country", "latency_ewma",
                 "ok_count", "fail_count", "fail_streak", "score", "use_count", "day_key",
                 "last_served_at", "cooldown_until", "last_error")
