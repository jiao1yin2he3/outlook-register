"""Page actions for recovery-email binding (no browser close)."""
from __future__ import annotations

import re
from typing import Any, Callable

from .observe import (
    Observation,
    find_code_input,
    find_email_input,
    find_login_email_input,
    find_password_input,
    is_abuse_hard_blocked,
    is_chrome_net_error,
    is_invalid_account,
    is_login_blocked,
    observe,
    page_host,
)

LogFn = Callable[[str], None]


class BindTerminalError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class AbuseBlockedError(BindTerminalError):
    def __init__(self, message: str = "ABUSE_BLOCKED"):
        super().__init__("ABUSE_BLOCKED", message)


class CaptchaSkipError(BindTerminalError):
    def __init__(self, message: str = "CAPTCHA_SKIP"):
        super().__init__("CAPTCHA_SKIP", message)


class ProxyDeadError(BindTerminalError):
    def __init__(self, message: str = "PROXY_DEAD"):
        super().__init__("PROXY_DEAD", message)


def click_first_visible(page, builders, label: str = "", log: LogFn | None = None) -> bool:
    _log = log or (lambda _m: None)
    for build in builders:
        try:
            loc = build() if callable(build) else build
            if loc is None:
                continue
            first = loc.first if hasattr(loc, "first") else loc
            if first.is_visible(timeout=600):
                enabled = True
                try:
                    enabled = first.is_enabled()
                except Exception:
                    enabled = True
                if not enabled:
                    continue
                _log(f"[Bind: Click] {label or 'action'}")
                first.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def dismiss_overlays(page, log: LogFn | None = None) -> None:
    click_first_visible(
        page,
        [
            lambda: page.get_by_role("button", name=re.compile(r"^拒绝$|Reject", re.I)),
            lambda: page.get_by_role("button", name=re.compile(r"^接受$|Accept all|Accept", re.I)),
        ],
        "cookie-overlay",
        log=log,
    )


def try_use_password(page, log: LogFn | None = None) -> bool:
    return click_first_visible(
        page,
        [
            lambda: page.get_by_role(
                "button",
                name=re.compile(r"使用密码|Use (your )?password|用密码|Sign in with password", re.I),
            ),
            lambda: page.get_by_role(
                "link",
                name=re.compile(r"使用密码|Use (your )?password|用密码|Sign in with password", re.I),
            ),
            lambda: page.locator('a:has-text("使用密码"), button:has-text("使用密码")'),
            lambda: page.get_by_text(re.compile(r"使用密码|Use your password|Sign in with password", re.I)),
        ],
        "use-password",
        log=log,
    )


def _is_nav_abort_message(msg: str) -> bool:
    """OAuth/client redirects often abort the previous navigation — not a dead proxy."""
    return bool(
        re.search(
            r"ERR_ABORTED|NS_BINDING_ABORTED|frame was detached|navigating.*interrupted|"
            r"Navigation interrupted|Target closed|Execution context was destroyed",
            msg or "",
            re.I,
        )
    )


def _is_proxy_failure_message(msg: str) -> bool:
    s = msg or ""
    if _is_nav_abort_message(s):
        return False
    return bool(
        re.search(
            r"ERR_PROXY|ERR_TUNNEL|ERR_SOCKS|ERR_CONNECTION_RESET|ERR_CONNECTION_TIMED_OUT|"
            r"ERR_CONNECTION_CLOSED|ERR_CONNECTION_REFUSED|ERR_NAME_NOT_RESOLVED|"
            r"ERR_INTERNET_DISCONNECTED|ERR_TIMED_OUT|ERR_EMPTY_RESPONSE|ERR_NETWORK_CHANGED|"
            r"ERR_SSL_PROTOCOL_ERROR|ERR_PROXY_CONNECTION_FAILED|ECONNREFUSED|ECONNRESET|"
            r"NS_ERROR_PROXY|tunnel connection failed|net::ERR_PROXY|net::ERR_TUNNEL|"
            r"net::ERR_CONNECTION_|net::ERR_NAME_NOT_RESOLVED|net::ERR_INTERNET_DISCONNECTED|"
            r"net::ERR_TIMED_OUT|net::ERR_EMPTY_RESPONSE",
            s,
            re.I,
        )
    )


def safe_goto(
    page,
    url: str,
    timeout: int = 60000,
    log: LogFn | None = None,
    *,
    attempts: int = 3,
) -> None:
    """Goto with retries. ERR_ABORTED / interrupted nav retries; real proxy errors raise."""
    _log = log or (lambda _m: None)
    attempts = max(1, int(attempts or 3))
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as e:
            last_err = e
            msg = str(e)
            if _is_proxy_failure_message(msg):
                raise ProxyDeadError(f"goto failed url={url}: {msg}") from e
            # Soft retry: aborted / race during post-login redirect
            if (
                _is_nav_abort_message(msg)
                or re.search(r"Timeout|timeout|Navigation", msg, re.I)
            ) and i < attempts - 1:
                _log(f"[Bind: Goto] retry i={i + 1} url={url} {msg[:120]}")
                try:
                    page.wait_for_timeout(1500 + i * 800)
                except Exception:
                    pass
                continue
            _log(f"[Bind: Goto] soft fail {url}: {msg[:160]}")
            return
    if last_err is not None:
        _log(f"[Bind: Goto] exhausted retries {url}: {str(last_err)[:160]}")
def click_add_recovery_cta(page, label: str = "security-cta", log: LogFn | None = None) -> bool:
    return click_first_visible(
        page,
        [
            lambda: page.get_by_role(
                "link",
                name=re.compile(
                    r"添加恢复电子邮件|添加辅助电子邮件|Add recovery email|Add a recovery|立即添加",
                    re.I,
                ),
            ),
            lambda: page.get_by_role(
                "button",
                name=re.compile(
                    r"添加恢复电子邮件|添加辅助电子邮件|Add recovery email|Add a recovery|立即添加",
                    re.I,
                ),
            ),
            lambda: page.locator(
                'a:has-text("添加恢复电子邮件"), button:has-text("添加恢复电子邮件")'
            ),
            lambda: page.locator(
                'a:has-text("Add a recovery email"), button:has-text("Add a recovery email")'
            ),
            lambda: page.get_by_text(re.compile(r"添加恢复电子邮件")),
        ],
        label,
        log=log,
    )


def handle_interrupt(
    page,
    obs: Observation,
    ctx: dict[str, Any],
    *,
    captcha_solver=None,
    log: LogFn | None = None,
    account_email: str = "",
) -> bool:
    """Handle interrupt states. Returns True if something was done (re-observe)."""
    _log = log or (lambda _m: None)
    dismiss_overlays(page, log=_log)

    if obs.state == "PROXY_DEAD" or is_chrome_net_error(f"{obs.url}\n{obs.title}\n{obs.text}"):
        raise ProxyDeadError(f"PROXY_DEAD url={(obs.url or '')[:120]}")

    if obs.state == "INVALID_ACCOUNT" or is_invalid_account(f"{obs.title}\n{obs.text}"):
        raise BindTerminalError("INVALID_ACCOUNT", f"INVALID_ACCOUNT account={account_email}")

    if obs.state == "LOGIN_BLOCKED" or is_login_blocked(f"{obs.title}\n{obs.text}"):
        raise BindTerminalError("LOGIN_BLOCKED", f"LOGIN_BLOCKED account={account_email}")

    if obs.state == "ABUSE_BLOCKED" or is_abuse_hard_blocked(f"{obs.title}\n{obs.text}"):
        raise AbuseBlockedError(f"ABUSE_BLOCKED account={account_email} url={(obs.url or '')[:120]}")

    if obs.state == "PRIVACY":
        btn = page.get_by_role(
            "button", name=re.compile(r"接受|Agree|I accept|继续|Continue|确定|OK", re.I)
        ).first
        try:
            for _ in range(8):
                if page.is_closed():
                    return True
                cur = page.url + (page.title() or "")
                if not re.search(r"privacynotice|隐私声明|privacy notice", cur, re.I):
                    _log("[Bind: Privacy] auto-navigated away")
                    return True
                if btn.is_visible(timeout=500):
                    if btn.is_enabled():
                        _log("[Bind: Privacy] accept")
                        btn.click(timeout=5000)
                        page.wait_for_timeout(1000)
                        return True
                page.wait_for_timeout(1000)
        except Exception as e:
            _log(f"[Bind: Privacy] wait {e}")
        return True

    if obs.state == "PASSKEY":
        skipped = click_first_visible(
            page,
            [
                lambda: page.get_by_role(
                    "button",
                    name=re.compile(
                        r"取消|Cancel|关闭|Close|跳过|Skip|暂时跳过|Not now|以后再说|使用密码|用密码登录|Password",
                        re.I,
                    ),
                ),
                lambda: page.get_by_role(
                    "link",
                    name=re.compile(r"取消|Cancel|跳过|Skip|使用密码|Password", re.I),
                ),
                lambda: page.locator('button:has-text("取消")'),
            ],
            "passkey-skip",
            log=_log,
        )
        if not skipped:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        return True

    if obs.state == "KMSI":
        click_first_visible(
            page,
            [lambda: page.get_by_role("button", name=re.compile(r"^是$|Yes", re.I))],
            "kmsi-yes",
            log=_log,
        )
        return True

    if obs.state == "MS_ERROR":
        _log("[Bind: MS_ERROR] retry/dismiss then re-observe")
        retried = click_first_visible(
            page,
            [
                lambda: page.get_by_role(
                    "button",
                    name=re.compile(
                        r"重试|Try again|再试|Retry|确定|OK|关闭|Close|继续|Continue|返回|Back",
                        re.I,
                    ),
                ),
                lambda: page.get_by_role(
                    "link",
                    name=re.compile(
                        r"重试|Try again|再试|Retry|确定|OK|关闭|Close|继续|Continue|返回|Back",
                        re.I,
                    ),
                ),
                lambda: page.locator('[data-testid="primaryButton"], button[type="submit"]'),
            ],
            "ms-error-retry",
            log=_log,
        )
        if not retried:
            page.wait_for_timeout(1500)
        return True

    if obs.state == "ABUSE":
        if is_abuse_hard_blocked(f"{obs.title}\n{obs.text}"):
            raise AbuseBlockedError(f"ABUSE_BLOCKED account={account_email}")
        _log("[Bind: Abuse] click next then re-observe")
        clicked = click_first_visible(
            page,
            [
                lambda: page.get_by_role(
                    "button",
                    name=re.compile(r"下一步|Next|继续|Continue|我知道了|OK|确定", re.I),
                ),
                lambda: page.get_by_role(
                    "link", name=re.compile(r"下一步|Next|继续|Continue", re.I)
                ),
                lambda: page.locator('[data-testid="primaryButton"], button[type="submit"]'),
            ],
            "abuse-continue",
            log=_log,
        )
        if clicked:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
        else:
            page.wait_for_timeout(800)
        return True

    if obs.state == "CAPTCHA":
        if captcha_solver is None:
            raise CaptchaSkipError("CAPTCHA_SKIP no solver")
        return captcha_solver(page, ctx)

    if obs.state == "IDENTITY_CHALLENGE" or (
        obs.state == "CODE_FORM"
        and re.match(r"^(login\.live\.com|login\.microsoftonline\.com)$", obs.host or page_host(obs.url), re.I)
    ):
        if try_use_password(page, log=_log):
            return True
        # Prefer password re-fill if password field appears
        pwd = find_password_input(page)
        if pwd and ctx.get("password"):
            try:
                pwd.click(click_count=3)
            except Exception:
                pass
            pwd.fill("")
            pwd.fill(str(ctx.get("password")))
            click_first_visible(
                page,
                [
                    lambda: page.get_by_role(
                        "button", name=re.compile(r"下一步|Next|登录|Sign in", re.I)
                    ),
                    lambda: page.locator(
                        '#idSIButton9, #iNext, input[type="submit"], button[type="submit"]'
                    ),
                ],
                "identity-password-submit",
                log=_log,
            )
            page.wait_for_timeout(2000)
            return True
        # Soft continue — re-observe; post-register rarely needs recovery identity
        _log("[Bind: Identity] challenge seen — soft continue")
        page.wait_for_timeout(1000)
        return True

    return False


def open_add_flow(
    page,
    ctx: dict[str, Any],
    *,
    captcha_solver=None,
    log: LogFn | None = None,
    account_email: str = "",
    alt_domains: list[str] | None = None,
) -> None:
    _log = log or (lambda _m: None)
    _log("[Bind: Action] openAddFlow")
    targets = [
        "https://account.live.com/proofs/Add",
        "https://account.microsoft.com/security",
        "https://account.live.com/proofs/Add?mkt=zh-CN",
    ]
    for i in range(6):
        obs0 = observe(page, alt_domains)
        if obs0.state == "EMAIL_FORM" or re.search(r"/proofs/Add", obs0.url or "", re.I):
            _log(f"[Bind: OpenAdd] already on email form state={obs0.state}")
            return
        if handle_interrupt(
            page, obs0, ctx, captcha_solver=captcha_solver, log=_log, account_email=account_email
        ):
            page.wait_for_timeout(1200)
            continue
        target = targets[min(i, len(targets) - 1)]
        try:
            safe_goto(page, target, timeout=90000, log=_log)
        except ProxyDeadError:
            raise
        except Exception as e:
            _log(f"[Bind: OpenAdd] goto err {e}")
        page.wait_for_timeout(2200)
        dismiss_overlays(page, log=_log)
        obs = observe(page, alt_domains)
        _log(f"[Bind: OpenAdd] i={i} state={obs.state} url={(obs.url or '')[:120]}")
        if handle_interrupt(
            page, obs, ctx, captcha_solver=captcha_solver, log=_log, account_email=account_email
        ):
            page.wait_for_timeout(1500)
            continue
        if obs.state == "EMAIL_FORM" or re.search(r"/proofs/Add", obs.url or "", re.I):
            break
        if obs.state in ("SECURITY_HOME", "CHECKUP_PENDING", "ACCOUNT"):
            clicked = click_add_recovery_cta(page, f"security-cta-{i}", log=_log)
            page.wait_for_timeout(2000)
            after = observe(page, alt_domains)
            if after.state == "EMAIL_FORM" or re.search(r"/proofs/Add", after.url or "", re.I):
                break
            if not clicked or i >= 2:
                _log("[Bind: OpenAdd] force proofs/Add")
                try:
                    safe_goto(page, "https://account.live.com/proofs/Add", timeout=90000, log=_log)
                except ProxyDeadError:
                    raise
                except Exception:
                    pass
                page.wait_for_timeout(2500)


def navigate_toward_email_form(
    page,
    step: int,
    ctx: dict[str, Any],
    *,
    target_count: int = 2,
    captcha_solver=None,
    log: LogFn | None = None,
    account_email: str = "",
    password: str = "",
    alt_domains: list[str] | None = None,
) -> dict[str, Any]:
    _log = log or (lambda _m: None)
    for i in range(14):
        obs = observe(page, alt_domains)
        _log(
            f"[Bind: NavForm] step={step} i={i} state={obs.state} "
            f"bound={len(obs.unique_bound)} url={(obs.url or '')[:100]}"
        )

        if obs.state == "MANAGE_PROOFS" and len(obs.unique_bound) >= target_count:
            return {"done": True, "bound": obs.unique_bound}
        if obs.state == "DONE" or (
            obs.state == "SECURITY_HOME"
            and not re.search(r"添加恢复|立即添加|挂起", obs.text or "", re.I)
        ):
            if len(obs.unique_bound) >= target_count:
                return {"done": True, "bound": obs.unique_bound}

        if handle_interrupt(
            page, obs, ctx, captcha_solver=captcha_solver, log=_log, account_email=account_email
        ):
            page.wait_for_timeout(1000)
            continue

        if obs.state == "EMAIL_FORM" or re.search(r"/proofs/Add", obs.url or "", re.I):
            email_input = find_email_input(page)
            if email_input:
                return {"done": False, "email_input": email_input, "bound": obs.unique_bound}
            page.wait_for_timeout(1500)
            continue

        if obs.state == "CODE_FORM":
            return {"done": False, "code_only": True, "bound": obs.unique_bound}

        if obs.state == "CHECKUP_PENDING":
            click_first_visible(
                page,
                [
                    lambda: page.get_by_role("button", name=re.compile(r"立即添加|Add now", re.I)),
                    lambda: page.get_by_role("link", name=re.compile(r"立即添加|Add now", re.I)),
                    lambda: page.locator('button:has-text("立即添加"), a:has-text("立即添加")'),
                ],
                "checkup-add-now",
                log=_log,
            )
            page.wait_for_timeout(2000)
            continue

        if obs.state == "METHOD_CHOOSE":
            click_first_visible(
                page,
                [
                    lambda: page.get_by_text(
                        re.compile(r"备用电子邮件地址|备用电子邮件|Alternate email", re.I)
                    ),
                    lambda: page.get_by_role(
                        "radio",
                        name=re.compile(r"备用电子邮件|Alternate email|电子邮件", re.I),
                    ),
                ],
                "choose-email-method",
                log=_log,
            )
            click_first_visible(
                page,
                [
                    lambda: page.get_by_role(
                        "button", name=re.compile(r"下一步|Next|继续|Continue", re.I)
                    )
                ],
                "method-next",
                log=_log,
            )
            page.wait_for_timeout(1800)
            if i >= 2:
                try:
                    safe_goto(page, "https://account.live.com/proofs/Add", timeout=60000, log=_log)
                except ProxyDeadError:
                    raise
                except Exception:
                    pass
                page.wait_for_timeout(2000)
            continue

        host = obs.host or page_host(obs.url)
        if (
            obs.state in ("LOGIN_EMAIL", "LOGIN_PASSWORD", "LOGIN_HOST")
            and re.match(r"^(login\.live\.com|login\.microsoftonline\.com)$", host, re.I)
        ):
            if obs.state == "LOGIN_EMAIL":
                box = find_login_email_input(page)
                if box:
                    box.fill(account_email)
                    click_first_visible(
                        page,
                        [lambda: page.get_by_role("button", name=re.compile(r"下一步|Next", re.I))],
                        "reauth-email",
                        log=_log,
                    )
                    page.wait_for_timeout(1500)
            elif obs.state == "LOGIN_PASSWORD":
                box = find_password_input(page)
                if box:
                    try:
                        box.click(click_count=3)
                    except Exception:
                        pass
                    box.fill("")
                    box.fill(password)
                    clicked = click_first_visible(
                        page,
                        [
                            lambda: page.get_by_role(
                                "button", name=re.compile(r"下一步|Next|登录|Sign in", re.I)
                            ),
                            lambda: page.locator(
                                '#idSIButton9, #iNext, input[type="submit"], button[type="submit"]'
                            ),
                        ],
                        "reauth-password",
                        log=_log,
                    )
                    if not clicked:
                        try:
                            box.press("Enter")
                        except Exception:
                            pass
                    page.wait_for_timeout(2000)
            else:
                try_use_password(page, log=_log)
                page.wait_for_timeout(1000)
            continue

        if obs.state in ("SECURITY_HOME", "ACCOUNT", "CHECKUP_PENDING"):
            click_add_recovery_cta(page, f"security-add-cta-{i}", log=_log)
            page.wait_for_timeout(2000)
            if i in (0, 2, 4, 7, 11):
                _log("[Bind: NavForm] fallback proofs/Add")
                try:
                    safe_goto(page, "https://account.live.com/proofs/Add", timeout=60000, log=_log)
                except ProxyDeadError:
                    raise
                except Exception:
                    pass
                page.wait_for_timeout(2500)
            continue

        if obs.state == "MANAGE_PROOFS":
            if len(obs.unique_bound) >= target_count:
                return {"done": True, "bound": obs.unique_bound}
            clicked = click_first_visible(
                page,
                [
                    lambda: page.get_by_role(
                        "link",
                        name=re.compile(r"添加另一种|Add another|添加.*方式|添加.*方法", re.I),
                    ),
                    lambda: page.get_by_role(
                        "button", name=re.compile(r"添加另一种|Add another|添加", re.I)
                    ),
                    lambda: page.get_by_text(re.compile(r"添加另一种登录帐户的方式|添加另一种", re.I)),
                ],
                "manage-add-another",
                log=_log,
            )
            if not clicked:
                try:
                    safe_goto(page, "https://account.live.com/proofs/Add", timeout=60000, log=_log)
                except ProxyDeadError:
                    raise
                except Exception:
                    pass
                page.wait_for_timeout(2000)
            continue

        if i in (5, 9, 12):
            try:
                safe_goto(page, "https://account.live.com/proofs/Add", timeout=60000, log=_log)
            except ProxyDeadError:
                raise
            except Exception:
                pass
            page.wait_for_timeout(2000)
            continue

        page.wait_for_timeout(1200)

    raise RuntimeError(f"could not reach email form for step {step}")


def _click_primary_next(page, label: str, log: LogFn | None = None) -> bool:
    """Click primary Next/Verify — never 暂时跳过."""
    return click_first_visible(
        page,
        [
            lambda: page.get_by_role(
                "button",
                name=re.compile(r"下一步|Next|验证|Verify|继续|Continue|完成|Done|提交|Submit", re.I),
            ),
            lambda: page.locator(
                'button:has-text("下一步"), input[type="submit"][value*="下一步"], '
                'button:has-text("Next"), #idSIButton9, #iNext, '
                'input[type="submit"], button[type="submit"]'
            ),
            lambda: page.get_by_role(
                "link",
                name=re.compile(r"^下一步$|^Next$|验证|Verify", re.I),
            ),
        ],
        label,
        log=log,
    )


def settle_after_verify(
    page,
    ctx: dict[str, Any],
    step: int,
    *,
    captcha_solver=None,
    log: LogFn | None = None,
    account_email: str = "",
    alt_domains: list[str] | None = None,
) -> Observation:
    _log = log or (lambda _m: None)
    for i in range(12):
        obs = observe(page, alt_domains)
        _log(f"[Bind: PostVerify] step={step} i={i} state={obs.state}")
        if obs.state in (
            "MANAGE_PROOFS",
            "EMAIL_FORM",
            "CHECKUP_PENDING",
            "SECURITY_HOME",
            "DONE",
            "METHOD_CHOOSE",
        ):
            return obs
        # Stuck on code form after fill — re-click primary Next, never Skip
        if obs.state == "CODE_FORM":
            clicked = _click_primary_next(page, f"post-verify-code-next-{i}", log=_log)
            if not clicked:
                try:
                    code_box = find_code_input(page)
                    if code_box:
                        code_box.press("Enter")
                        _log(f"[Bind: PostVerify] code Enter fallback i={i}")
                except Exception:
                    pass
            page.wait_for_timeout(2000)
            continue
        if obs.state in ("MS_ERROR", "PROXY_DEAD") or re.search(r"errcode=1086", obs.url or "", re.I):
            _log(f"[Bind: PostVerify] MS_ERROR recover i={i}")
            click_first_visible(
                page,
                [
                    lambda: page.get_by_role(
                        "button",
                        name=re.compile(
                            r"确定|OK|关闭|Close|继续|Continue|重试|Try again|返回|Back", re.I
                        ),
                    ),
                    lambda: page.get_by_role(
                        "link",
                        name=re.compile(
                            r"确定|OK|关闭|Close|继续|Continue|返回|Back|安全|Security", re.I
                        ),
                    ),
                ],
                f"post-verify-error-dismiss-{i}",
                log=_log,
            )
            recover = [
                "https://account.live.com/proofs/Manage",
                "https://account.microsoft.com/security",
                "https://account.live.com/proofs/Add",
            ]
            t = recover[min(i, len(recover) - 1)]
            try:
                safe_goto(page, t, timeout=60000, log=_log)
            except ProxyDeadError:
                raise
            except Exception as e:
                _log(f"[Bind: PostVerify] recover goto fail {e}")
            page.wait_for_timeout(2000)
            continue
        if handle_interrupt(
            page, obs, ctx, captcha_solver=captcha_solver, log=_log, account_email=account_email
        ):
            page.wait_for_timeout(1200)
            continue
        if obs.state in ("KMSI", "PRIVACY", "PASSKEY"):
            page.wait_for_timeout(800)
            continue
        page.wait_for_timeout(1000)
    return observe(page, alt_domains)


def verify_bound_on_manage(
    page,
    ctx: dict[str, Any],
    *,
    captcha_solver=None,
    log: LogFn | None = None,
    account_email: str = "",
    alt_domains: list[str] | None = None,
) -> list[str]:
    _log = log or (lambda _m: None)
    targets = [
        "https://account.live.com/proofs/Manage",
        "https://account.microsoft.com/security",
        "https://account.live.com/proofs/Manage?mkt=zh-CN",
    ]
    for attempt in range(6):
        obs = observe(page, alt_domains)
        if obs.state == "PROXY_DEAD" or is_chrome_net_error(f"{obs.url}\n{obs.title}\n{obs.text}"):
            raise ProxyDeadError(f"verifyBound network dead url={(obs.url or '')[:120]}")
        if handle_interrupt(
            page, obs, ctx, captcha_solver=captcha_solver, log=_log, account_email=account_email
        ):
            page.wait_for_timeout(1200)
            continue
        if obs.unique_bound and (
            obs.state in ("MANAGE_PROOFS", "SECURITY_HOME")
            or re.search(r"proofs/Manage", page.url or "", re.I)
        ):
            return obs.unique_bound
        if re.search(r"proofs/Manage", page.url or "", re.I) and obs.state == "MANAGE_PROOFS":
            return obs.unique_bound
        target = targets[attempt % len(targets)]
        try:
            safe_goto(page, target, timeout=60000, log=_log)
        except ProxyDeadError:
            raise
        except Exception as e:
            _log(f"[Bind: VerifyManage] goto err {e}")
        page.wait_for_timeout(2200)
    obs = observe(page, alt_domains)
    return obs.unique_bound


def finish_if_possible(
    page,
    ctx: dict[str, Any],
    *,
    captcha_solver=None,
    log: LogFn | None = None,
    account_email: str = "",
    alt_domains: list[str] | None = None,
    allow_skip: bool = True,
) -> Observation:
    """Dismiss post-bind prompts. Never skip while CODE_FORM or EMAIL_FORM still needs action."""
    _log = log or (lambda _m: None)
    for i in range(8):
        obs = observe(page, alt_domains)
        _log(f"[Bind: Finish] i={i} state={obs.state}")
        if handle_interrupt(
            page, obs, ctx, captcha_solver=captcha_solver, log=_log, account_email=account_email
        ):
            continue
        # Still on code entry — must click 下一步, never 暂时跳过
        if obs.state == "CODE_FORM":
            _click_primary_next(page, f"finish-code-next-{i}", log=_log)
            page.wait_for_timeout(1500)
            continue
        # Second protect-account email form — leave for bind loop; do not skip
        if obs.state == "EMAIL_FORM":
            _log("[Bind: Finish] EMAIL_FORM still open — not skipping")
            return obs
        builders = [
            lambda: page.get_by_role(
                "button",
                name=re.compile(r"完成|Done|继续|Continue|关闭|Close|确定|OK", re.I),
            ),
            lambda: page.get_by_role(
                "link", name=re.compile(r"完成|Done|继续|Continue", re.I)
            ),
        ]
        if allow_skip and obs.state not in ("CODE_FORM", "EMAIL_FORM"):
            builders.extend(
                [
                    lambda: page.get_by_role(
                        "button",
                        name=re.compile(
                            r"暂时跳过|Skip for now|以后再说|Not now|跳过|Skip", re.I
                        ),
                    ),
                    lambda: page.get_by_role(
                        "link",
                        name=re.compile(
                            r"暂时跳过|Skip for now|以后再说|Not now|跳过|Skip", re.I
                        ),
                    ),
                ]
            )
        click_first_visible(page, builders, "finish-or-skip", log=_log)
        if obs.state in ("MANAGE_PROOFS", "SECURITY_HOME", "DONE"):
            return obs
        page.wait_for_timeout(1000)
    return observe(page, alt_domains)
