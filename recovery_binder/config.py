"""Bind configuration loaded from host config dict with safe defaults."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any


DEFAULT_DOMAINS = [
    {
        "domain": "example.com",
        "code_api": "https://proxy.example.org/account1-code",
    },
    {
        "domain": "example.org",
        "code_api": "https://proxy.example.org/account2-code",
    },
]

DEFAULT_CF_DOMAINS = [
    "mail3.example.org",
    "mail4.example.org",
    "mail5.example.org",
    "mail6.example.org",
    "mail1.example.org",
    "mail7.example.org",
    "mail8.example.org",
    "mail9.example.org",
    "mail2.example.org",
    "mail10.example.org",
    "mail11.example.org",
    "mail12.example.org",
]

DEFAULT_CF_API_BASE = "https://email.example.org"
DEFAULT_CF_CREATE_PATH = "/api/new_address"
DEFAULT_CF_MAILS_PATH = "/api/parsed_mails?limit=10&offset=0"

# Recommended production values are 1 or 2; higher values remain allowed.
MAX_TARGET_COUNT = 5


def _safe_int(value: Any, default: int) -> int:
    try:
        if value is None or value is False:
            return default
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def _norm_mode(value: Any, default: str = "random") -> str:
    s = str(value or default).strip().lower()
    if s in ("cf", "legacy", "random"):
        return s
    return default


@dataclass
class DomainEntry:
    domain: str
    code_api: str


@dataclass
class BindConfig:
    enabled: bool = True
    target_count: int = 2
    timeout_sec: int = 180
    captcha_max_rounds: int = 3
    captcha_deadline_ms: int = 120_000
    code_poll_timeout_ms: int = 90_000
    code_poll_interval_ms: int = 3_000
    domains: list[DomainEntry] = field(default_factory=list)
    code_proxy: str | None = None  # legacy alias; folded into chain
    code_proxy_fallback: str | None = None  # fixed hop after register proxy
    session_proxy: str | None = None  # runtime: proxy used for this register task
    register_proxy: str | None = None  # config.proxy snapshot
    debug: bool = False
    stuck_dump_after: int = 4
    max_observe_ticks: int = 80
    max_ms_error_retries: int = 6
    # Mail source: cf | legacy | random
    mail_source_mode: str = "random"
    legacy_enabled: bool = True
    cf_enabled: bool = True
    cf_api_base: str = DEFAULT_CF_API_BASE
    cf_create_path: str = DEFAULT_CF_CREATE_PATH
    cf_mails_path: str = DEFAULT_CF_MAILS_PATH
    cf_domains: list[str] = field(default_factory=list)
    cf_timeout_ms: int = 120_000
    cf_poll_interval_ms: int = 3_000
    cf_http_direct: bool = True  # kept; cascade always ends with direct
    # Runtime domains from CF create (and any extra) for observe regex
    extra_domains: list[str] = field(default_factory=list)

    def code_proxy_chain(self) -> list[tuple[str, str | None]]:
        """register/session → fixed fallback → direct. None proxy = direct."""
        chain: list[tuple[str, str | None]] = []
        seen: set[str] = set()

        def _add_proxy(label: str, raw: str | None) -> None:
            if raw is None:
                return
            p = str(raw).strip()
            if not p or p.lower() in ("0", "none", "direct", "false"):
                return
            if p not in seen:
                seen.add(p)
                chain.append((label, p))

        _add_proxy("register", self.session_proxy or self.register_proxy or self.code_proxy)
        _add_proxy("fixed", self.code_proxy_fallback)
        chain.append(("direct", None))
        return chain

    def __post_init__(self) -> None:
        if not self.domains:
            self.domains = [
                DomainEntry(d["domain"], d["code_api"]) for d in DEFAULT_DOMAINS
            ]
        if not self.cf_domains:
            self.cf_domains = list(DEFAULT_CF_DOMAINS)
        self.mail_source_mode = _norm_mode(self.mail_source_mode, "random")
        n = _safe_int(self.target_count, 2)
        self.target_count = max(1, min(n, MAX_TARGET_COUNT))
        self.timeout_sec = max(30, _safe_int(self.timeout_sec, 180))
        self.captcha_max_rounds = max(1, _safe_int(self.captcha_max_rounds, 3))
        self.cf_timeout_ms = max(5_000, _safe_int(self.cf_timeout_ms, 120_000))
        self.cf_poll_interval_ms = max(500, _safe_int(self.cf_poll_interval_ms, 3_000))
        self.cf_api_base = str(self.cf_api_base or DEFAULT_CF_API_BASE).rstrip("/")
        self.cf_create_path = str(self.cf_create_path or DEFAULT_CF_CREATE_PATH)
        self.cf_mails_path = str(self.cf_mails_path or DEFAULT_CF_MAILS_PATH)

    @property
    def alt_domains(self) -> list[str]:
        """Legacy + CF + runtime domains for observe bound-email regex."""
        out: list[str] = []
        seen: set[str] = set()
        for d in self.domains:
            x = str(d.domain or "").lower().strip()
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        for d in self.cf_domains:
            x = str(d or "").lower().strip()
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        for d in self.extra_domains:
            x = str(d or "").lower().strip()
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def register_extra_domain(self, domain: str) -> None:
        d = str(domain or "").lower().strip()
        if not d:
            return
        if d not in {x.lower() for x in self.extra_domains}:
            self.extra_domains.append(d)

    def code_api_for_email(self, email: str) -> str:
        dom = str(email or "").split("@")[-1].lower().strip()
        for entry in self.domains:
            if entry.domain.lower() == dom:
                return entry.code_api
        return self.domains[0].code_api

    def source_enabled(self, name: str) -> bool:
        n = str(name or "").lower().strip()
        if n == "legacy":
            return bool(self.legacy_enabled) and bool(self.domains)
        if n == "cf":
            return bool(self.cf_enabled) and bool(str(self.cf_api_base or "").strip())
        return False

    def enabled_mail_sources(self) -> list[str]:
        return [n for n in ("cf", "legacy") if self.source_enabled(n)]

    @classmethod
    def from_host(cls, config: dict[str, Any] | None = None) -> "BindConfig":
        cfg = dict(config or {})
        nested = cfg.get("bind_mail_sources")
        nested = nested if isinstance(nested, dict) else {}
        nested_sources = nested.get("sources") if isinstance(nested.get("sources"), dict) else {}
        nested_legacy = nested_sources.get("legacy") if isinstance(nested_sources.get("legacy"), dict) else {}
        nested_cf = nested_sources.get("cf") if isinstance(nested_sources.get("cf"), dict) else {}

        raw_domains = cfg.get("bind_domains")
        if not raw_domains and isinstance(nested_legacy.get("domains"), dict):
            raw_domains = [
                {"domain": k, "code_api": v}
                for k, v in nested_legacy["domains"].items()
                if k and v
            ]
        if not raw_domains:
            raw_domains = DEFAULT_DOMAINS
        domains: list[DomainEntry] = []
        for item in raw_domains:
            if isinstance(item, dict) and item.get("domain") and item.get("code_api"):
                domains.append(
                    DomainEntry(
                        domain=str(item["domain"]).strip(),
                        code_api=str(item["code_api"]).strip(),
                    )
                )
        if not domains:
            domains = [DomainEntry(d["domain"], d["code_api"]) for d in DEFAULT_DOMAINS]

        # chain: session/register → fixed fallback → direct
        register_proxy = cfg.get("proxy")
        if register_proxy is not None:
            register_proxy = str(register_proxy).strip() or None
        code_proxy_fallback = cfg.get("bind_code_proxy_fallback")
        if code_proxy_fallback is None:
            # bind_code_proxy = fixed hop only when它不同于注册 proxy
            bcp = cfg.get("bind_code_proxy")
            if bcp is not None:
                bcp = str(bcp).strip() or None
                if bcp and bcp != register_proxy:
                    code_proxy_fallback = bcp
        if code_proxy_fallback is not None:
            code_proxy_fallback = str(code_proxy_fallback).strip() or None
        code_proxy = register_proxy  # legacy alias = first hop default
        session_proxy = cfg.get("bind_session_proxy")
        if session_proxy is not None:
            session_proxy = str(session_proxy).strip() or None

        mode = cfg.get("bind_mail_source")
        if mode is None:
            mode = nested.get("mode")
        mode = _norm_mode(mode, "random")

        legacy_enabled = cfg.get("bind_legacy_enabled")
        if legacy_enabled is None:
            legacy_enabled = nested_legacy.get("enabled", True)
        legacy_enabled = _safe_bool(legacy_enabled, True)

        cf_enabled = cfg.get("bind_cf_enabled")
        if cf_enabled is None:
            cf_enabled = nested_cf.get("enabled", True)
        cf_enabled = _safe_bool(cf_enabled, True)

        cf_api_base = (
            cfg.get("bind_cf_api_base")
            or nested_cf.get("apiBase")
            or nested_cf.get("api_base")
            or DEFAULT_CF_API_BASE
        )
        cf_create_path = (
            cfg.get("bind_cf_create_path")
            or nested_cf.get("createPath")
            or nested_cf.get("create_path")
            or DEFAULT_CF_CREATE_PATH
        )
        cf_mails_path = (
            cfg.get("bind_cf_mails_path")
            or nested_cf.get("mailsPath")
            or nested_cf.get("mails_path")
            or DEFAULT_CF_MAILS_PATH
        )

        raw_cf_domains = cfg.get("bind_cf_domains")
        if raw_cf_domains is None:
            raw_cf_domains = nested_cf.get("domains")
        cf_domains: list[str] = []
        if isinstance(raw_cf_domains, list):
            cf_domains = [str(x).strip() for x in raw_cf_domains if str(x).strip()]
        elif isinstance(raw_cf_domains, str) and raw_cf_domains.strip():
            cf_domains = [x.strip() for x in raw_cf_domains.split(",") if x.strip()]
        if not cf_domains:
            cf_domains = list(DEFAULT_CF_DOMAINS)

        cf_timeout_ms = cfg.get("bind_cf_timeout_ms")
        if cf_timeout_ms is None:
            cf_timeout_ms = nested_cf.get("timeoutMs", nested_cf.get("timeout_ms", 120_000))
        cf_poll_interval_ms = cfg.get("bind_cf_poll_interval_ms")
        if cf_poll_interval_ms is None:
            cf_poll_interval_ms = nested_cf.get(
                "pollIntervalMs", nested_cf.get("poll_interval_ms", 3_000)
            )

        cf_http_direct = cfg.get("bind_cf_http_direct")
        if cf_http_direct is None:
            cf_http_direct = nested_cf.get("httpDirect", nested_cf.get("http_direct", True))
        cf_http_direct = _safe_bool(cf_http_direct, True)

        raw_target = cfg.get("bind_target_count", 2)
        if isinstance(raw_target, (list, tuple)) and len(raw_target) >= 2:
            lo = _safe_int(raw_target[0], 1)
            hi = _safe_int(raw_target[1], 2)
            target_count = random.randint(min(lo, hi), max(lo, hi))
        elif isinstance(raw_target, str) and "-" in raw_target:
            a, b = raw_target.split("-", 1)
            lo, hi = _safe_int(a.strip(), 1), _safe_int(b.strip(), 2)
            target_count = random.randint(min(lo, hi), max(lo, hi))
        else:
            target_count = _safe_int(raw_target, 2)

        return cls(
            enabled=bool(cfg.get("bind_enabled", True)),
            target_count=target_count,
            timeout_sec=_safe_int(cfg.get("bind_timeout_sec", 180), 180),
            captcha_max_rounds=_safe_int(cfg.get("bind_captcha_max_rounds", 3), 3),
            captcha_deadline_ms=_safe_int(
                cfg.get("bind_captcha_deadline_ms", 120_000), 120_000
            ),
            code_poll_timeout_ms=_safe_int(
                cfg.get("bind_code_poll_timeout_ms", 90_000), 90_000
            ),
            code_poll_interval_ms=_safe_int(
                cfg.get("bind_code_poll_interval_ms", 3_000), 3_000
            ),
            domains=domains,
            code_proxy=code_proxy,
            code_proxy_fallback=code_proxy_fallback,
            session_proxy=session_proxy,
            register_proxy=register_proxy,
            debug=bool(cfg.get("bind_debug", False)),
            stuck_dump_after=_safe_int(cfg.get("bind_stuck_dump_after", 4), 4),
            max_observe_ticks=_safe_int(cfg.get("bind_max_observe_ticks", 80), 80),
            max_ms_error_retries=_safe_int(
                cfg.get("bind_max_ms_error_retries", 6), 6
            ),
            mail_source_mode=mode,
            legacy_enabled=legacy_enabled,
            cf_enabled=cf_enabled,
            cf_api_base=str(cf_api_base).strip().rstrip("/"),
            cf_create_path=str(cf_create_path).strip() or DEFAULT_CF_CREATE_PATH,
            cf_mails_path=str(cf_mails_path).strip() or DEFAULT_CF_MAILS_PATH,
            cf_domains=cf_domains,
            cf_timeout_ms=_safe_int(cf_timeout_ms, 120_000),
            cf_poll_interval_ms=_safe_int(cf_poll_interval_ms, 3_000),
            cf_http_direct=cf_http_direct,
        )
