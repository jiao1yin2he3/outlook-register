"""Multi-field page state classification for recovery bind."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

ALT_EMAIL_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+@(?:example\.com|example\.org))",
    re.I,
)
ALT_MASK_RE = re.compile(
    r"([a-zA-Z0-9]{1,4}\*+@(?:example\.com|example\.org))",
    re.I,
)


def page_host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def is_chrome_net_error(blob: str) -> bool:
    # ERR_ABORTED is a common redirect race, not a dead network.
    text = blob or ""
    if re.search(r"ERR_ABORTED|NS_BINDING_ABORTED", text, re.I) and not re.search(
        r"ERR_PROXY|ERR_TUNNEL|ERR_CONNECTION|ERR_NAME_NOT_RESOLVED|"
        r"ERR_INTERNET_DISCONNECTED|ERR_TIMED_OUT|ERR_EMPTY_RESPONSE|"
        r"ERR_PROXY_CONNECTION_FAILED",
        text,
        re.I,
    ):
        return False
    return bool(
        re.search(
            r"chrome-error://|ERR_PROXY|ERR_TUNNEL|ERR_CONNECTION|ERR_NAME_NOT_RESOLVED|"
            r"ERR_INTERNET_DISCONNECTED|ERR_TIMED_OUT|ERR_EMPTY_RESPONSE|"
            r"ERR_PROXY_CONNECTION_FAILED|net::ERR_(?!ABORTED)|"
            r"This site can.?t be reached|Check your Internet connection|"
            r"\u65e0\u6cd5\u8bbf\u95ee\u6b64\u7f51\u7ad9",
            text,
            re.I,
        )
    )
def is_abuse_hard_blocked(text: str) -> bool:
    return bool(
        re.search(
            r"帐户恢复已被阻止|阻止恢复此帐户|recovery (has )?been blocked|can't recover this account|"
            r"无法恢复此帐户",
            text or "",
            re.I,
        )
    )


def is_ms_error_page(url: str, title: str, text: str) -> bool:
    blob = f"{url}\n{title}\n{text}"
    if re.search(r"errcode=1086|error\.aspx", url or "", re.I):
        return True
    return bool(
        re.search(
            r"出现问题|服务出现问题|Something went wrong|We.re sorry|服务暂时|Try again later|"
            r"请稍后重试|technical difficulties",
            blob,
            re.I,
        )
    )


def is_service_error_page(title: str, text: str) -> bool:
    blob = f"{title}\n{text}"
    return bool(
        re.search(
            r"出现问题|服务出现问题|Something went wrong|Try again|重试",
            blob,
            re.I,
        )
        and re.search(r"重试|Try again|Retry|确定|OK", blob, re.I)
    )


def is_invalid_account(text: str) -> bool:
    return bool(
        re.search(
            r"找不到.*Microsoft.*帐户|We couldn.?t find.*account|That Microsoft account doesn.?t exist|"
            r"此 Microsoft 帐户不存在",
            text or "",
            re.I,
        )
    )


def is_login_blocked(text: str) -> bool:
    return bool(
        re.search(
            r"次数过多|too many (incorrect|failed)|暂时无法登录|account.*locked.*password|"
            r"incorrect.*password.*attempts",
            text or "",
            re.I,
        )
    )


@dataclass
class Observation:
    state: str
    url: str = ""
    title: str = ""
    text: str = ""
    unique_bound: list[str] = field(default_factory=list)
    masked_hints: list[str] = field(default_factory=list)
    host: str = ""
    has_email_input: bool = False
    has_code_input: bool = False
    has_password_input: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "url": self.url,
            "title": self.title,
            "host": self.host,
            "uniqueBound": self.unique_bound,
            "maskedHints": self.masked_hints,
            "hasEmailInput": self.has_email_input,
            "hasCodeInput": self.has_code_input,
            "textSample": (self.text or "")[:400],
        }


def _visible_editable(locator) -> bool:
    try:
        if locator.count() == 0:
            return False
        loc = locator.first
        if not loc.is_visible(timeout=400):
            return False
        # Prefer enabled editable fields
        disabled = loc.get_attribute("disabled")
        readonly = loc.get_attribute("readonly")
        if disabled is not None and disabled != "false":
            return False
        if readonly is not None and readonly != "false":
            return False
        return True
    except Exception:
        return False


def find_email_input(page):
    cands = [
        page.locator('input[type="email"]').first,
        page.locator("#iProofEmail, #EmailAddress, #email, input[name='EmailAddress']").first,
        page.locator('input[name*="email" i]').first,
        page.locator('input[id*="email" i]').first,
        page.locator('input[placeholder*="example.com" i]').first,
        page.locator('input[placeholder*="someone@" i]').first,
        page.locator(
            'input[placeholder*="电子邮件" i], input[placeholder*="邮箱" i], input[placeholder*="email" i]'
        ).first,
        page.locator('input[aria-label*="电子邮件" i]').first,
        page.locator('input[aria-label*="邮箱" i]').first,
        page.locator('input[aria-label*="Email" i]').first,
        page.locator('form input[type="text"]').first,
        page.locator('input[type="text"]').first,
    ]
    for c in cands:
        if _visible_editable(c):
            return c
    return None


def find_code_input(page):
    cands = [
        page.locator('input[name*="code" i]').first,
        page.locator('input[id*="code" i]').first,
        page.locator('input[aria-label*="代码" i]').first,
        page.locator('input[aria-label*="code" i]').first,
        page.locator('input[autocomplete="one-time-code"]').first,
        page.locator('input[type="tel"]').first,
        page.locator('form input[type="text"]').first,
    ]
    for c in cands:
        if _visible_editable(c):
            return c
    return None


def find_password_input(page):
    cands = [
        page.locator('input[type="password"]').first,
        page.locator("#i0118").first,
        page.locator('input[name="passwd"]').first,
        page.locator('input[aria-label*="密码" i], input[aria-label*="Password" i]').first,
    ]
    for c in cands:
        if _visible_editable(c):
            return c
    try:
        loose = page.locator('input[type="password"], #i0118, input[name="passwd"]').first
        if loose.is_visible(timeout=500):
            return loose
    except Exception:
        pass
    return None


def find_login_email_input(page):
    cands = [
        page.locator('input[type="email"]').first,
        page.locator('input[name="loginfmt"]').first,
        page.locator("#i0116").first,
        page.locator('input[type="text"]').first,
    ]
    for c in cands:
        if _visible_editable(c):
            return c
    return None


def observe(page, alt_domains: list[str] | None = None) -> Observation:
    """Classify current page from multiple fields; never guess next page."""
    domains = alt_domains or ["example.com", "example.org"]
    domain_alt = "|".join(re.escape(d) for d in domains)
    email_re = re.compile(rf"([a-zA-Z0-9._%+\-]+@(?:{domain_alt}))", re.I)
    mask_re = re.compile(rf"([a-zA-Z0-9]{{1,4}}\*+@(?:{domain_alt}))", re.I)

    url = ""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    host = page_host(url)
    title = ""
    text = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        text = ""

    blob = f"{url}\n{title}\n{text}"
    is_login_host = bool(re.match(r"^(login\.live\.com|login\.microsoftonline\.com)$", host, re.I))
    is_account_host = bool(
        re.match(
            r"^(account\.live\.com|account\.microsoft\.com|privacynotice\.account\.microsoft\.com)$",
            host,
            re.I,
        )
    )
    is_proofs_add = bool(
        re.search(r"/proofs/Add", url, re.I)
        or re.search(
            r"让我们来保护你的帐户|Protect your account|使用个人电子邮件|备用电子邮件地址|"
            r"We'll send a security code to your alternate",
            title + text,
            re.I,
        )
    )

    bound = [m.group(1).lower() for m in email_re.finditer(text or "")]
    unique_bound = list(dict.fromkeys(bound))
    masked = [m.group(0).lower() for m in mask_re.finditer(text or "")]

    has_email = find_email_input(page) is not None
    has_code = find_code_input(page) is not None
    has_password = find_password_input(page) is not None

    state = "UNKNOWN"
    if is_chrome_net_error(blob) or re.search(r"chrome-error://", url or "", re.I):
        state = "PROXY_DEAD"
    elif re.search(r"验证你的电子邮件|Verify your email", title + text, re.I) and re.search(
        r"发送验证码|Send code|我们将向|We'll send", text or "", re.I
    ):
        state = "IDENTITY_CHALLENGE"
    elif re.search(
        r"证明你不是机器人|are you a robot|Press and hold|长按该按钮|按住该按钮",
        title + text,
        re.I,
    ) or (
        re.search(r"captcha|hip|人机验证", title + text, re.I)
        and not re.search(r"下一步|Next|继续|Continue", text or "", re.I)
        and not re.search(r"帐户已锁定|锁定了你的帐户|account.*locked", text or "", re.I)
    ):
        state = "CAPTCHA"
    elif is_abuse_hard_blocked(title + text):
        state = "ABUSE_BLOCKED"
    elif is_service_error_page(title, text) or (
        is_ms_error_page(url, title, text)
        and not is_proofs_add
        and not re.search(r"帐户已锁定|锁定了你的帐户|完成人机验证", text or "", re.I)
    ):
        state = "MS_ERROR"
    elif re.search(r"/Abuse", url or "", re.I) or re.search(
        r"似乎有问题|unusual activity|暂时锁定|已被锁定|帐户已锁定|锁定了你的帐户|完成人机验证",
        text or "",
        re.I,
    ):
        state = "ABUSE"
    elif is_invalid_account(title + text):
        state = "INVALID_ACCOUNT"
    elif is_login_blocked(title + text):
        state = "LOGIN_BLOCKED"
    elif (
        re.search(
            r"验证你的身份|Verify your identity|向 .* 发送电子邮件|我已有验证码|I have a code",
            title + text,
            re.I,
        )
        and not re.search(r"proofs/Verify|输入我们发送到", blob, re.I)
        and not is_proofs_add
    ):
        state = "IDENTITY_CHALLENGE"
    elif re.search(r"privacynotice", host, re.I) or re.search(
        r"隐私声明|privacy notice", title + text, re.I
    ):
        state = "PRIVACY"
    elif re.search(r"fido|passkey|interrupt/passkey|正在设置密钥|创建密钥", blob, re.I):
        state = "PASSKEY"
    elif re.search(r"保持登录|Stay signed in", title + text, re.I):
        state = "KMSI"
    elif re.search(r"proofs/Verify|输入我们发送|输入代码|Enter the code|输入.*代码", blob, re.I) and has_code:
        state = "CODE_FORM"
    elif is_proofs_add or re.search(r"/proofs/Add", url or "", re.I) or re.search(
        r"让我们来保护你的帐户|Protect your account", title + text, re.I
    ):
        state = "EMAIL_FORM"
    elif is_login_host and re.search(r"密码|Password", text or "", re.I) and has_password:
        state = "LOGIN_PASSWORD"
    elif (
        is_login_host
        and (find_login_email_input(page) is not None)
        and not re.search(r"验证你的电子邮件|Verify your email|发送验证码", title + text, re.I)
    ):
        state = "LOGIN_EMAIL"
    elif re.search(r"proofs/Manage|证明你的身份的方法|其他安全选项", blob, re.I):
        state = "MANAGE_PROOFS"
    elif (
        (
            re.search(
                r"使用以下方法验证|选择.*验证方法|Choose how to verify|How do you want to get",
                title + text,
                re.I,
            )
            or (
                re.search(r"备用电子邮件地址", text or "", re.I)
                and re.search(r"电话号码|Phone number", text or "", re.I)
                and re.search(r"单选|radio|下一步|Next", text or "", re.I)
            )
        )
        and not re.search(r"account\.microsoft\.com/security", url or "", re.I)
        and not re.search(r"切勿失去对|添加恢复电子邮件", text or "", re.I)
    ):
        state = "METHOD_CHOOSE"
    elif re.search(r"立即添加|你有挂起的安全操作|Account Checkup|挂起的安全", text or "", re.I) and not re.search(
        r"account\.microsoft\.com/security", url or "", re.I
    ):
        state = "CHECKUP_PENDING"
    elif re.search(r"account\.microsoft\.com/security", url or "", re.I) or re.search(
        r"添加恢复电子邮件|Add a recovery email|切勿失去对 Microsoft 帐户的访问权限",
        text or "",
        re.I,
    ):
        state = "SECURITY_HOME"
    elif re.search(r"全部完成|你已完成|You're all set|安全设置已更新", text or "", re.I) and not re.search(
        r"立即添加|添加辅助电子邮件地址|添加恢复电子邮件", text or "", re.I
    ):
        state = "DONE"
    elif is_account_host:
        state = "ACCOUNT"
    elif is_login_host:
        if re.search(r"验证|Verify|身份", title + text, re.I):
            state = "IDENTITY_CHALLENGE"
        else:
            state = "LOGIN_HOST"

    return Observation(
        state=state,
        url=url,
        title=title,
        text=text,
        unique_bound=unique_bound,
        masked_hints=masked,
        host=host,
        has_email_input=has_email,
        has_code_input=has_code,
        has_password_input=has_password,
    )
