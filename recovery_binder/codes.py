"""Dual-source recovery email generation and code polling (legacy + CF)."""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .config import BindConfig, DomainEntry

FIRST_NAMES = [
    "james", "john", "robert", "michael", "david", "william", "richard", "joseph",
    "thomas", "charles", "mary", "patricia", "jennifer", "linda", "elizabeth",
    "barbara", "susan", "jessica", "sarah", "karen", "daniel", "matthew", "anthony",
    "mark", "donald", "steven", "paul", "andrew", "joshua", "kenneth", "emily",
    "ashley", "amanda", "melissa", "deborah", "stephanie", "rebecca", "sharon",
    "laura", "cynthia", "brian", "kevin", "jason", "jeffrey", "ryan", "jacob",
    "gary", "nicholas", "eric", "jonathan", "amy", "angela", "brenda", "emma",
    "olivia", "sophia", "isabella", "mia", "charlotte", "amelia",
]
LAST_NAMES = [
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis",
    "rodriguez", "martinez", "hernandez", "lopez", "gonzalez", "wilson", "anderson",
    "thomas", "taylor", "moore", "jackson", "martin", "lee", "perez", "thompson",
    "white", "harris", "sanchez", "clark", "ramirez", "lewis", "robinson", "walker",
    "young", "allen", "king", "wright", "scott", "torres", "nguyen", "hill",
    "flores", "green", "adams", "nelson", "baker", "hall", "rivera", "campbell",
    "mitchell", "carter", "roberts",
]


@dataclass
class Mailbox:
    email: str
    source: str  # cf | legacy
    domain: str = ""
    jwt: str | None = None
    code_api: str | None = None


def random_local_part() -> str:
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    n = random.randint(10, 9009)
    styles = [
        lambda: f"{first}.{last}{n}",
        lambda: f"{first}{last}{n}",
        lambda: f"{first}.{last}.{n}",
        lambda: f"{first[0]}{last}{n}",
        lambda: f"{first}_{last}{n}",
    ]
    return random.choice(styles)().lower()


def random_alt_email(
    domains: list[DomainEntry] | None = None,
    config: BindConfig | None = None,
) -> str:
    """Backward-compat: create a legacy random local@domain address."""
    return create_legacy_mailbox(config=config, domains=domains).email


def create_legacy_mailbox(
    config: BindConfig | None = None,
    domains: list[DomainEntry] | None = None,
) -> Mailbox:
    pool = domains or (config.domains if config else None)
    if not pool:
        pool = [DomainEntry("example.com", ""), DomainEntry("example.org", "")]
    entry = random.choice(pool)
    email = f"{random_local_part()}@{entry.domain}"
    domain = entry.domain.lower()
    if config is not None:
        config.register_extra_domain(domain)
    return Mailbox(
        email=email,
        source="legacy",
        domain=domain,
        jwt=None,
        code_api=entry.code_api or None,
    )


def _pick_mail_source_name(
    config: BindConfig,
    force_source: str | None = None,
) -> str:
    mode = (force_source or config.mail_source_mode or "random").lower().strip()
    enabled = config.enabled_mail_sources()
    if not enabled:
        raise RuntimeError("no mail sources enabled (check bind_mail_source / CF/legacy flags)")
    if mode in ("cf", "legacy"):
        if mode not in enabled:
            raise RuntimeError(f"mail source {mode} not enabled")
        return mode
    return random.choice(enabled)


def _proxy_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    """None/direct → empty ProxyHandler (no env inherit). Else http(s) via proxy."""
    if not proxy or str(proxy).strip().lower() in ("0", "none", "direct", "false"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    p = str(proxy).strip()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": p, "https": p})
    )


def _scrub_proxy_env() -> dict[str, str | None]:
    saved: dict[str, str | None] = {}
    for k in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        saved[k] = os.environ.pop(k, None)
    return saved


def _restore_proxy_env(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


def _cf_opener(config: BindConfig, proxy: str | None = None) -> urllib.request.OpenerDirector:
    """Build opener for one hop. proxy=None means direct."""
    if proxy is None and config.cf_http_direct and not (config.session_proxy or config.code_proxy):
        return _proxy_opener(None)
    return _proxy_opener(proxy)


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: float = 45,
) -> dict[str, Any]:
    data = None
    hdrs = {"User-Agent": "OutlookRegister-recovery-binder/1.0"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    op = opener or urllib.request.build_opener()
    # Temporarily clear process proxy env for direct when opener uses empty ProxyHandler
    with op.open(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace").strip()
    parsed: Any = text
    try:
        parsed = json.loads(text)
    except Exception:
        pass
    return {"text": text, "json": parsed, "url": url}


def create_cf_mailbox(config: BindConfig) -> Mailbox:
    api_base = str(config.cf_api_base or "").rstrip("/")
    create_path = config.cf_create_path or "/api/new_address"
    if not create_path.startswith("/"):
        create_path = "/" + create_path
    url = f"{api_base}{create_path}"
    body: dict[str, Any] = {}
    domains = [d for d in (config.cf_domains or []) if d]
    if domains:
        body["domain"] = random.choice(domains)
    last_err: Exception | None = None
    for label, proxy in config.code_proxy_chain():
        opener = _proxy_opener(proxy)
        saved_env = _scrub_proxy_env()
        try:
            hit = _http_json("POST", url, body=body, opener=opener, timeout=45)
        except Exception as e:
            last_err = e
            _restore_proxy_env(saved_env)
            continue
        _restore_proxy_env(saved_env)
        data = hit.get("json") if isinstance(hit.get("json"), dict) else {}
        address = str(data.get("address") or data.get("email") or "").strip()
        jwt = str(data.get("jwt") or data.get("token") or "").strip()
        if not address or not jwt:
            last_err = RuntimeError(
                f"CF create missing address/jwt via {label}: {str(hit.get('text') or '')[:200]}"
            )
            continue
        domain = address.split("@")[-1].lower() if "@" in address else ""
        config.register_extra_domain(domain)
        return Mailbox(email=address, source="cf", domain=domain, jwt=jwt, code_api=None)
    raise RuntimeError(f"CF create failed all proxy hops: {last_err}")


def create_alt_mailbox(
    config: BindConfig,
    *,
    force_source: str | None = None,
    allow_fallback: bool = True,
    log: Callable[[str], None] | None = None,
) -> Mailbox:
    """Create one alt mailbox per mail_source_mode (cf | legacy | random)."""
    _log = log or (lambda _m: None)
    mode = (config.mail_source_mode or "random").lower().strip()
    do_fallback = (
        allow_fallback
        and not force_source
        and mode == "random"
    )
    source = _pick_mail_source_name(config, force_source=force_source)
    try:
        mb = create_cf_mailbox(config) if source == "cf" else create_legacy_mailbox(config=config)
        _log(f"[Bind: Mail] mode={mode} pick={mb.source} email={mb.email}")
        if mb.source == "cf":
            _log(f"[Bind: Mail] CF_CREATE ok address={mb.email}")
        return mb
    except Exception as e:
        _log(f"[Bind: Mail] MAIL_CREATE_FAIL source={source} {e}")
        if do_fallback:
            other = "legacy" if source == "cf" else "cf"
            if config.source_enabled(other):
                _log(f"[Bind: Mail] MAIL_CREATE_FALLBACK -> {other}")
                return create_alt_mailbox(
                    config,
                    force_source=other,
                    allow_fallback=False,
                    log=log,
                )
        raise


def extract_code(payload: Any) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        s = str(int(payload))
        return s if re.fullmatch(r"\d{4,8}", s) else None
    if isinstance(payload, str):
        pure = payload.strip()
        if not pure or pure == "[]":
            return None
        if re.fullmatch(r"\d{4,8}", pure):
            return pure
        try:
            return extract_code(json.loads(pure))
        except Exception:
            pass
        m = (
            re.search(r"(?:code|security\s*code|otp|verify)[^\d]{0,20}(\d{4,8})", pure, re.I)
            or re.search(r"\b(\d{6})\b", pure)
            or re.search(r"\b(\d{4,8})\b", pure)
        )
        return m.group(1) if m else None
    if isinstance(payload, list):
        for item in reversed(payload):
            c = extract_code(item)
            if c:
                return c
        return None
    if isinstance(payload, dict):
        for k in (
            "code", "otp", "verifyCode", "verificationCode", "securityCode",
            "data", "message", "result", "body", "text", "content", "subject",
        ):
            if payload.get(k) is not None:
                c = extract_code(payload.get(k))
                if c:
                    return c
        for v in payload.values():
            c = extract_code(v)
            if c:
                return c
    return None


def pick_code_from_payload(
    payload: Any,
    alt_email: str,
    seen_codes: set[str] | None = None,
) -> str | None:
    """Only accept codes whose `to` matches the alt email exactly (legacy)."""
    want = str(alt_email or "").lower().strip()
    if not want:
        return None
    seen = seen_codes or set()
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = [payload]
    else:
        return None
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        to = str(item.get("to") or item.get("email") or item.get("recipient") or "").lower().strip()
        code = extract_code(item)
        if not code or code in seen:
            continue
        if to == want:
            return code
    return None


def pick_code_from_cf_payload(
    payload: Any,
    alt_email: str,
    seen_codes: set[str] | None = None,
) -> str | None:
    """Extract MS 4-8 digit codes from CF parsed_mails payload."""
    want = str(alt_email or "").lower().strip()
    seen = seen_codes or set()
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            items = payload["results"]
        elif isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("mails"), list):
            items = payload["mails"]
        else:
            items = [payload]
    else:
        return None

    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        recipients: list[str] = []
        to_val = item.get("to")
        if to_val is not None:
            if isinstance(to_val, list):
                for t in to_val:
                    if isinstance(t, str):
                        recipients.append(t.lower())
                    elif isinstance(t, dict) and t.get("address"):
                        recipients.append(str(t["address"]).lower())
            else:
                recipients.append(str(to_val).lower())
        addr = str(
            item.get("address") or item.get("email") or item.get("recipient") or ""
        ).lower()
        if want:
            # If message lists recipients, require match; empty recipients = accept (CF inbox-scoped).
            if recipients:
                matched = any(r == want or want in r for r in recipients)
                if not matched:
                    continue
            elif addr and addr != want and want not in addr:
                continue
        blobs = [
            item.get("subject"),
            item.get("text"),
            item.get("html"),
            item.get("body"),
            item.get("content"),
            item.get("raw"),
        ]
        for blob in blobs:
            if blob is None:
                continue
            code = extract_code(str(blob))
            if code and code not in seen:
                return code
        nested = extract_code(item)
        if nested and nested not in seen:
            return nested
    return None


def fetch_code_api_once(url: str, proxy: str | None = None) -> dict[str, Any]:
    opener = _proxy_opener(proxy)
    saved_env = _scrub_proxy_env()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "OutlookRegister-recovery-binder/1.0"},
            method="GET",
        )
        with opener.open(req, timeout=45) as resp:
            text = resp.read().decode("utf-8", errors="replace").strip()
    finally:
        _restore_proxy_env(saved_env)
    parsed: Any = text
    try:
        parsed = json.loads(text)
    except Exception:
        pass
    return {"text": text, "json": parsed, "apiUrl": url}


def _ensure_mails_query(mails_path: str) -> str:
    path = str(mails_path or "/api/parsed_mails")
    if not re.search(r"[?&]limit=", path):
        path += ("&" if "?" in path else "?") + "limit=10&offset=0"
    elif not re.search(r"[?&]offset=", path):
        path += ("&" if "?" in path else "?") + "offset=0"
    return path


def fetch_cf_mails_once(
    mailbox: Mailbox,
    config: BindConfig,
    proxy: str | None = None,
) -> dict[str, Any]:
    api_base = str(config.cf_api_base or "").rstrip("/")
    mails_path = _ensure_mails_query(config.cf_mails_path or "/api/parsed_mails")
    if not mails_path.startswith("/"):
        mails_path = "/" + mails_path
    url = f"{api_base}{mails_path}"
    opener = _proxy_opener(proxy)
    saved_env = _scrub_proxy_env()
    try:
        return _http_json(
            "GET",
            url,
            headers={"Authorization": f"Bearer {mailbox.jwt or ''}"},
            opener=opener,
            timeout=45,
        )
    finally:
        _restore_proxy_env(saved_env)


def poll_verification_code(
    alt_email: str,
    *,
    config: BindConfig,
    seen_codes: set[str] | None = None,
    timeout_ms: int | None = None,
    interval_ms: int | None = None,
    log: Callable[[str], None] | None = None,
    fetch_once: Callable[[str, str | None], dict[str, Any]] | None = None,
    mailbox: Mailbox | None = None,
    fetch_cf_once: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Poll code API. Network errors advance proxy chain: register → fixed → direct."""
    seen = seen_codes if seen_codes is not None else set()
    mb = mailbox or Mailbox(
        email=alt_email,
        source="legacy",
        domain=str(alt_email or "").split("@")[-1].lower(),
        code_api=config.code_api_for_email(alt_email),
    )
    timeout = timeout_ms if timeout_ms is not None else config.code_poll_timeout_ms
    interval = interval_ms if interval_ms is not None else config.code_poll_interval_ms
    if mb.source == "cf":
        timeout = timeout_ms if timeout_ms is not None else config.cf_timeout_ms
        interval = interval_ms if interval_ms is not None else config.cf_poll_interval_ms
    fetch = fetch_once or fetch_code_api_once
    fetch_cf = fetch_cf_once or fetch_cf_mails_once
    _log = log or (lambda _m: None)
    start = time.time()
    last_raw = ""
    chain = config.code_proxy_chain()
    hop_i = 0
    label, proxy = chain[hop_i]
    if mb.source == "cf" and mb.jwt:
        api_hint = f"{config.cf_api_base}{_ensure_mails_query(config.cf_mails_path)}"
    else:
        api_hint = mb.code_api or config.code_api_for_email(alt_email)
    _log(
        f"[Bind: Code] poll start source={mb.source} want={alt_email} "
        f"api={api_hint} hop={label}"
    )
    while (time.time() - start) * 1000 < timeout:
        try:
            code = None
            if mb.source == "cf" and mb.jwt:
                try:
                    hit = fetch_cf(mb, config, proxy)
                except TypeError:
                    hit = fetch_cf(mb, config)
                raw = hit.get("json")
                last_raw = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                code = pick_code_from_cf_payload(raw, alt_email, seen)
                _log(
                    f"[Bind: Code] CF_POLL hop={label} want={alt_email} "
                    f"code={code or '-'} elapsed={int((time.time() - start) * 1000)}ms"
                )
            else:
                api_url = mb.code_api or config.code_api_for_email(alt_email)
                hit = fetch(api_url, proxy)
                raw = hit.get("json")
                last_raw = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                code = pick_code_from_payload(raw, alt_email, seen)
                _log(
                    f"[Bind: Code] hop={label} want={alt_email} code={code or '-'} "
                    f"elapsed={int((time.time() - start) * 1000)}ms"
                )
            if code:
                return code
        except Exception as e:
            _log(f"[Bind: Code] poll error hop={label}: {e}")
            if hop_i + 1 < len(chain):
                hop_i += 1
                label, proxy = chain[hop_i]
                _log(f"[Bind: Code] switch hop → {label}")
        time.sleep(max(0.5, interval / 1000.0))
    raise TimeoutError(f"timeout waiting code for {alt_email}. last={str(last_raw)[:300]}")


__all__ = [
    "Mailbox",
    "random_local_part",
    "random_alt_email",
    "create_legacy_mailbox",
    "create_cf_mailbox",
    "create_alt_mailbox",
    "extract_code",
    "pick_code_from_payload",
    "pick_code_from_cf_payload",
    "fetch_code_api_once",
    "fetch_cf_mails_once",
    "poll_verification_code",
]
