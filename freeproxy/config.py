"""配置：环境变量驱动（FREPROXY_ 前缀），全部带可用默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default):
    v = os.environ.get("FREPROXY_" + name)
    if v is None or v == "":
        return default
    t = type(default)
    if t is bool:
        return v.lower() in ("1", "true", "yes", "on")
    if t is int:
        return int(float(v))
    if t is float:
        return float(v)
    return v


@dataclass
class Settings:
    # 服务
    host: str = "127.0.0.1"
    port: int = 8300
    api_key: str = ""                # 非空则所有接口要求 X-API-Key（/health 除外）
    db_path: str = "freeproxy.db"
    log_level: str = "INFO"
    loops_enabled: bool = True       # False 仅供测试（不起后台循环）

    # 抓取
    refresh_interval: int = 1800     # 源抓取周期（秒）
    fetch_timeout: float = 20.0
    fetch_concurrency: int = 6
    fetch_proxy: str = ""            # 抓源走的上级代理（源被墙时用，如国内环境）

    # TCP 预检
    tcp_concurrency: int = 200
    tcp_timeout: float = 3.0

    # L7 深度验证
    l7_concurrency: int = 100
    l7_timeout: float = 10.0
    candidates_per_round: int = 300  # 每轮新候选验证上限

    # 复检
    recheck_interval: int = 300      # 复检周期（秒）
    recheck_batch: int = 200
    recheck_ttl: int = 300           # active 代理超过该秒数未复检 → 进入待复检
    candidate_backoff_base: int = 900    # 首验失败后重试间隔基数（秒），指数退避
    candidate_backoff_max: int = 21600   # 退避上限 6h
    fail_evict_streak: int = 3       # 已激活代理连续失败该次数 → 降级回候选（保留历史）

    # 服务面
    serve_ttl: float = 120.0         # 只服务「最近该秒数内 L7 验证通过」的代理 —— 稳定性核心闸门
    sticky_ttl: float = 300.0        # session 粘滞窗口（秒）
    max_entries: int = 120000        # 内存池上限，超出淘汰最差候选

    # 反馈冷却（连续失败 → 递增冷却秒数，借鉴原版 USE_COOLDOWN_MAP）
    cooldown_map: str = "0,30,90,300,900"

    # 出口劫持检测：同一 exit_ip 一轮内被超过该数量的不同代理声明 → 判为劫持
    hijack_threshold: int = 2

    def cooldown_for(self, fail_streak: int) -> int:
        vals = [int(x) for x in self.cooldown_map.split(",") if x.strip()]
        if not vals:
            return 0
        return vals[min(max(fail_streak, 0), len(vals) - 1)]

    def candidate_backoff(self, fail_streak: int) -> int:
        return min(self.candidate_backoff_base * (2 ** max(0, fail_streak - 1)),
                   self.candidate_backoff_max)


def load() -> Settings:
    s = Settings()
    for f in s.__dataclass_fields__:
        cur = getattr(s, f)
        setattr(s, f, _env(f.upper(), cur))
    return s
