"""Observe->act recovery bind runner (sync). Never closes page/browser."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .actions import (
    AbuseBlockedError,
    BindTerminalError,
    CaptchaSkipError,
    ProxyDeadError,
    click_first_visible,
    finish_if_possible,
    handle_interrupt,
    navigate_toward_email_form,
    open_add_flow,
    settle_after_verify,
    verify_bound_on_manage,
)
from .captcha import solve_captcha_or_raise
from .codes import create_alt_mailbox, poll_verification_code
from .config import BindConfig
from .observe import find_code_input, find_email_input, observe
from .records import append_recovery_email, dump_debug, record_bind_outcome

LogFn = Callable[[str], None]


@dataclass
class BindResult:
    status: str  # ok|partial|captcha|abuse|error|timeout|skipped
    emails: list[str] = field(default_factory=list)
    note: str = ""
    error: str | None = None


def _log_print(msg: str) -> None:
    print(msg)


def _map_exception(err: BaseException, added: list[str]) -> BindResult:
    msg = str(err) or type(err).__name__
    code = getattr(err, "code", "") or ""
    blob = f"{code} {msg}"
    if isinstance(err, CaptchaSkipError) or re.search(r"CAPTCHA_SKIP", blob, re.I):
        return BindResult(status="captcha", emails=list(added), note="captcha", error=msg)
    if isinstance(err, AbuseBlockedError) or re.search(
        r"ABUSE_BLOCKED|账户恢复已被阻止|阻止恢复", blob, re.I
    ):
        return BindResult(status="abuse", emails=list(added), note="abuse", error=msg)
    # ERR_ABORTED is not a dead proxy (redirect race); only real proxy/net failures.
    if isinstance(err, ProxyDeadError) or re.search(
        r"PROXY_DEAD|ERR_PROXY|ERR_TUNNEL|ERR_PROXY_CONNECTION_FAILED|ERR_CONNECTION_|"
        r"ERR_NAME_NOT_RESOLVED|ERR_INTERNET_DISCONNECTED|ERR_TIMED_OUT|ERR_EMPTY_RESPONSE",
        blob,
        re.I,
    ):
        if re.search(r"ERR_ABORTED|NS_BINDING_ABORTED", blob, re.I) and not re.search(
            r"ERR_PROXY|ERR_TUNNEL|ERR_CONNECTION|PROXY_DEAD", blob, re.I
        ):
            pass  # fall through — treat as soft/transient
        else:
            return BindResult(status="error", emails=list(added), note="proxy", error=msg)
    if isinstance(err, TimeoutError) or re.search(r"timeout|deadline", blob, re.I):
        status = "timeout" if added else "timeout"
        return BindResult(status=status, emails=list(added), note="timeout", error=msg)
    if isinstance(err, BindTerminalError):
        if code in ("LOGIN_BLOCKED", "INVALID_ACCOUNT"):
            return BindResult(status="error", emails=list(added), note=code.lower(), error=msg)
        if code == "ABUSE_BLOCKED":
            return BindResult(status="abuse", emails=list(added), note="abuse", error=msg)
        if code == "CAPTCHA_SKIP":
            return BindResult(status="captcha", emails=list(added), note="captcha", error=msg)
    if re.search(r"Abuse|锁定", blob, re.I):
        return BindResult(status="abuse", emails=list(added), note="abuse", error=msg)
    return BindResult(status="error", emails=list(added), note="error", error=msg)


def _is_real_editable(loc) -> bool:
    if loc is None:
        return False
    try:
        if hasattr(loc, "count") and loc.count() == 0:
            return False
        if not loc.is_visible(timeout=600):
            return False
        disabled = loc.get_attribute("disabled")
        readonly = loc.get_attribute("readonly")
        if disabled is not None and disabled != "false":
            return False
        if readonly is not None and readonly != "false":
            return False
        return True
    except Exception:
        return False


def add_one_email(
    page,
    ctx: dict[str, Any],
    step: int,
    *,
    config: BindConfig,
    account_email: str,
    password: str,
    results_dir: str | None,
    log: LogFn,
    captcha_solver,
) -> dict[str, Any]:
    seen_codes: set[str] = ctx.setdefault("seen_codes", set())
    mailbox = create_alt_mailbox(config, log=log)
    alt_email = mailbox.email
    # Runtime CF domains must be visible to observe regex
    ctx.setdefault("extra_domains", set()).add(mailbox.domain)
    log(f"[Bind: Add] === #{step} {alt_email} source={mailbox.source} ===")

    nav = navigate_toward_email_form(
        page,
        step,
        ctx,
        target_count=config.target_count,
        captcha_solver=captcha_solver,
        log=log,
        account_email=account_email,
        password=password,
        alt_domains=config.alt_domains,
    )
    if nav.get("done"):
        log(f"[Bind: Add] already bound enough: {nav.get('bound')}")
        return {"skipped": True, "alt_email": None, "code": None, "bound": nav.get("bound") or []}
    if nav.get("code_only"):
        raise RuntimeError(f"unexpected code form before submitting email at step {step}")

    email_input = nav.get("email_input") or find_email_input(page)
    if not _is_real_editable(email_input):
        raise RuntimeError("email locator is not a real editable input")

    after_send_text = ""
    for attempt in range(4):
        if attempt > 0:
            mailbox = create_alt_mailbox(config, log=log)
            alt_email = mailbox.email
            ctx.setdefault("extra_domains", set()).add(mailbox.domain)
            log(f"[Bind: Add] retry alt attempt={attempt} {alt_email} source={mailbox.source}")
            email_input = find_email_input(page) or email_input
        try:
            email_input.click(click_count=3)
        except Exception:
            pass
        email_input.fill("")
        email_input.fill(alt_email)
        log(f"[Bind: Add] filled {alt_email}")
        click_first_visible(
            page,
            [
                lambda: page.get_by_role(
                    "button",
                            name=re.compile(r"下一步|Next|发送|Send|继续|Continue|添加|Add", re.I),
                ),
                        lambda: page.locator(
                            'button:has-text("下一步"), button:has-text("Next"), '
                            'input[type="submit"], button[type="submit"]'
                        ),
                    ],
                    f"send-{step}-{attempt}",
                    log=log,
                )
        page.wait_for_timeout(2500)
        try:
            after_send_text = page.locator("body").inner_text(timeout=3000) or ""
        except Exception:
            after_send_text = ""
        if re.search(
                    r"此内容已经是你的安全信息|already.*security info|"
                    r"已是你的安全|已在使用|already in use|already part of",
            after_send_text,
            re.I,
        ):
            log(f"[Bind: Add] alt already used {alt_email}")
            continue
        break

    # 点击"发送验证码"按钮（如果存在）
    send_code_clicked = click_first_visible(
        page,
        [
            lambda: page.get_by_role(
                "button",
                name=re.compile(r"发送验证码|Send code|发送代码|Send the code", re.I),
            ),
            lambda: page.get_by_role(
                "link",
                name=re.compile(r"发送验证码|Send code|发送代码|Send the code", re.I),
            ),
            lambda: page.locator(
                'button:has-text("发送验证码"), button:has-text("Send code"), '
                'button:has-text("发送代码"), a:has-text("发送验证码")'
            ),
        ],
        f"send-code-{step}",
        log=log,
    )
    if send_code_clicked:
        log(f"[Bind: Add] clicked send verification code button")
        page.wait_for_timeout(3000)

    code_input = None
    for i in range(10):
        obs = observe(page, config.alt_domains)
        if handle_interrupt(
            page,
            obs,
            ctx,
            captcha_solver=captcha_solver,
            log=log,
            account_email=account_email,
        ):
            page.wait_for_timeout(800)
            continue
        # 如果页面上有"发送验证码"按钮，再点一次
        if i > 0 and i % 3 == 0:
            click_first_visible(
                page,
                [
                    lambda: page.get_by_role(
                        "button",
                        name=re.compile(r"发送验证码|Send code|发送代码|Send the code", re.I),
                    ),
                    lambda: page.locator(
                        'button:has-text("发送验证码"), button:has-text("Send code")'
                    ),
                ],
                f"send-code-retry-{step}-{i}",
                log=log,
            )
        code_input = find_code_input(page)
        if code_input:
            break
        if re.search(r"错误|invalid|无法|失败|已在使用|already", obs.text or "", re.I) and not re.search(
            r"代码|code|验证", obs.text or "", re.I
        ):
            raise RuntimeError(f"error after email submit: {(obs.text or '')[:240]}")
        page.wait_for_timeout(1000)
    if not code_input:
        raise RuntimeError(f"code input not found after sending {alt_email}")

    log(f"[Bind: Code] polling for {alt_email} source={mailbox.source}")
    code = poll_verification_code(
        alt_email,
        config=config,
        seen_codes=seen_codes,
        log=log,
        mailbox=mailbox,
    )
    seen_codes.add(code)
    log(f"[Bind: Code] got {code}")

    code_input.fill(code)
    # Prefer primary Next / Verify — never skip
    clicked_verify = click_first_visible(
        page,
        [
            lambda: page.get_by_role(
                "button",
                name=re.compile(
                    r"下一步|Next|验证|Verify|继续|Continue|完成|Done|提交|Submit",
                    re.I,
                ),
            ),
            lambda: page.locator(
                'button:has-text("下一步"), button:has-text("Next"), '
                '#idSIButton9, #iNext, input[type="submit"], button[type="submit"]'
            ),
            lambda: page.get_by_role(
                "link",
                name=re.compile(r"^下一步$|^Next$|验证|Verify", re.I),
            ),
        ],
        f"verify-{step}",
        log=log,
    )
    if not clicked_verify:
        try:
            code_input.press("Enter")
            log(f"[Bind: Code] verify Enter fallback step={step}")
        except Exception:
            pass
    page.wait_for_timeout(3000)

    known = ctx.setdefault("known_emails", [])
    known.append(alt_email)

    settle_after_verify(
        page,
        ctx,
        step,
        captcha_solver=captcha_solver,
        log=log,
        account_email=account_email,
        alt_domains=config.alt_domains,
    )

    if results_dir:
        try:
            append_recovery_email(
                results_dir,
                account=account_email,
                alt_email=alt_email,
                code=code,
                step=step,
            )
        except Exception as e:
            log(f"[Bind: Record] append recovery email failed: {e}")

    log(f"[Bind: Add] recorded {alt_email} code={code} source={mailbox.source}")
    return {
        "skipped": False,
        "alt_email": alt_email,
        "code": code,
        "step": step,
        "source": mailbox.source,
    }


def bind_recovery_emails(
    page,
    account_email: str,
    password: str,
    config: BindConfig | dict | None = None,
    *,
    results_dir: str | None = None,
    log: LogFn | None = None,
) -> BindResult:
    """Bind recovery emails on the current authenticated page/session.

    Never closes page/browser. Exceptions are caught into BindResult.
    """
    _log = log or _log_print
    if isinstance(config, BindConfig):
        cfg = config
    elif isinstance(config, dict):
        cfg = BindConfig.from_host(config)
    elif config is None:
        cfg = BindConfig()
    else:
        cfg = BindConfig.from_host({})

    account_email = str(account_email or "").strip()
    password = str(password or "")

    if not cfg.enabled:
        br = BindResult(status="skipped", emails=[], note="bind_enabled=false")
        record_bind_outcome(
            results_dir,
            account=account_email,
            status=br.status,
            emails=br.emails,
            note=br.note,
        )
        _log("[Bind: Result] status=skipped (disabled)")
        return br

    if page is None:
        br = BindResult(status="error", emails=[], note="no page", error="page is None")
        record_bind_outcome(
            results_dir,
            account=account_email,
            status=br.status,
            emails=[],
            note=br.note,
            error=br.error,
        )
        return br

    deadline = time.time() + cfg.timeout_sec
    added: list[str] = []
    ctx: dict[str, Any] = {
        "seen_codes": set(),
        "known_emails": [],
        "password": password,
        "config": cfg,
        "ms_error_retries": 0,
    }

    def captcha_solver(p, c):
        return solve_captcha_or_raise(p, c, log=_log)

    try:
        _log(
            f"[Bind: Start] account={account_email} target={cfg.target_count} "
            f"timeout={cfg.timeout_sec}s mode={cfg.mail_source_mode}"
        )

        # Quick observe + open add flow
        open_add_flow(
            page,
            ctx,
            captcha_solver=captcha_solver,
            log=_log,
            account_email=account_email,
            alt_domains=cfg.alt_domains,
        )

        step = 0
        max_steps = max(cfg.target_count, 1) + 2  # allow forced 2nd protect form beyond target
        while step < max_steps:
            if time.time() > deadline:
                raise TimeoutError("bind timeout budget exceeded")

            step += 1
            before = observe(page, cfg.alt_domains)
            _log(f"[Bind: Loop] step={step} state={before.state} bound={before.unique_bound}")
            if handle_interrupt(
                page,
                before,
                ctx,
                captcha_solver=captcha_solver,
                log=_log,
                account_email=account_email,
            ):
                page.wait_for_timeout(1000)

            again = observe(page, cfg.alt_domains)
            if again.state == "MANAGE_PROOFS" and len(again.unique_bound) >= cfg.target_count:
                _log(f"[Bind: EarlyStop] bound={again.unique_bound}")
                break
            # Target met and no forced email form — stop
            if len(added) >= cfg.target_count and again.state not in ("EMAIL_FORM", "CODE_FORM"):
                if again.state in ("MANAGE_PROOFS", "SECURITY_HOME", "DONE", "CHECKUP_PENDING"):
                    _log(f"[Bind: TargetMet] added={len(added)} state={again.state}")
                    break
            # Need more only if under target OR MS still shows email form (forced 2nd)
            if len(added) >= cfg.target_count and again.state != "EMAIL_FORM":
                if again.state != "CODE_FORM":
                    break

            one = add_one_email(
                page,
                ctx,
                step,
                config=cfg,
                account_email=account_email,
                password=password,
                results_dir=results_dir,
                log=_log,
                captcha_solver=captcha_solver,
            )
            if one.get("skipped"):
                break
            if one.get("alt_email"):
                added.append(str(one["alt_email"]).lower().strip())
            page.wait_for_timeout(1200)

            # After each add: if still on EMAIL_FORM, MS may force another recovery
            post = observe(page, cfg.alt_domains)
            if post.state == "EMAIL_FORM" and len(added) < max_steps:
                _log(f"[Bind: Loop] forced EMAIL_FORM after #{len(added)} — continue")
                continue

        finish_if_possible(
            page,
            ctx,
            captcha_solver=captcha_solver,
            log=_log,
            account_email=account_email,
            alt_domains=cfg.alt_domains,
            allow_skip=len(added) >= cfg.target_count,
        )
        bound_manage = verify_bound_on_manage(
            page,
            ctx,
            captcha_solver=captcha_solver,
            log=_log,
            account_email=account_email,
            alt_domains=cfg.alt_domains,
        )

        # Merge manage-listed emails into result list
        merged = list(dict.fromkeys([*added, *[e.lower().strip() for e in bound_manage if e]]))
        manage_ok = len(bound_manage) >= cfg.target_count
        soft_ok = len(added) >= cfg.target_count
        if manage_ok:
            status = "ok"
            note = "manage confirmed"
        elif soft_ok:
            status = "ok"
            note = f"soft ok: codes verified added={len(added)} manage={len(bound_manage)}"
            _log(note)
        elif added:
            status = "partial"
            note = f"added={len(added)} manage={len(bound_manage)}"
        else:
            status = "error"
            note = f"added=0 manage={len(bound_manage)}"

        br = BindResult(status=status, emails=merged, note=note)
        record_bind_outcome(
            results_dir,
            account=account_email,
            status=br.status,
            emails=br.emails,
            note=br.note,
        )
        _log(f"[Bind: Result] status={br.status} emails={br.emails} note={br.note}")
        return br

    except Exception as e:
        br = _map_exception(e, added)
        # Soft ok: codes already verified/recorded for target count even if Manage goto aborts.
        if added and len(added) >= cfg.target_count and br.status in ("error", "partial", "timeout"):
            if br.status not in ("captcha", "abuse"):
                br = BindResult(
                    status="ok",
                    emails=list(added),
                    note=f"soft ok: codes verified added={len(added)} after {br.note or 'nav flake'}",
                    error=None,
                )
                _log(br.note)
        elif added and br.status == "error":
            # Partial progress without full target — still better than pure error
            br = BindResult(
                status="partial",
                emails=list(added),
                note=br.note or "partial after error",
                error=br.error,
            )
        if cfg.debug and results_dir:
            try:
                text = ""
                html = ""
                try:
                    text = page.locator("body").inner_text(timeout=2000)
                except Exception:
                    pass
                try:
                    html = page.content()
                except Exception:
                    pass
                dump_debug(
                    results_dir,
                    f"bind-error-{br.status}",
                    text=text,
                    html=html,
                    meta={"account": account_email, "error": br.error, "status": br.status},
                )
            except Exception:
                pass
        record_bind_outcome(
            results_dir,
            account=account_email,
            status=br.status,
            emails=br.emails,
            note=br.note,
            error=br.error,
        )
        _log(f"[Bind: Result] status={br.status} emails={br.emails} error={br.error}")
        return br


__all__ = ["BindResult", "bind_recovery_emails", "add_one_email"]
