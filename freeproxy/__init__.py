"""独立免费代理池：多源抓取 → TCP 预检 → L7 真实转发验证 → 劫持检测 → 持续复检。

从 Aggregate-to-2api 的 proxy_pool/free_proxy_fetcher/proxy_tracer 提取并强化：
- L7 准入（原版只做 TCP 预检，实测 44% 端口开放但 ~0% 真实可用）
- 出口劫持检测（不同代理共享同一 exit_ip → 运营商透明劫持，拒绝准入）
- SQLite 持久化 + 指数退避复检（原版纯内存，重启即丢）
- 独立 HTTP 服务（/proxy /report /stats），供其他项目直接消费
"""

__version__ = "1.0.0"
