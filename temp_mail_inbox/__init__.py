"""临时邮箱获取与收件功能（从 ~/nvidia-register 提取的独立版本）。

三个 provider 统一接口:
  - create_inbox(name) -> TempEmailInbox(address, token)
  - poll_verification_code(inbox, timeout) -> str | None
  - fetch_verification_links(inbox, timeout) -> list[str]   (duckmail/mailtm 支持)

provider:
  - cloudflare_temp_email : 自托管 Cloudflare 临时邮箱 API
  - duckmail              : DuckMail (api.duckmail.sbs, Hydra API)
  - mailtm                : mail.tm 免费临时邮箱 (自动发现域名, 无需配置)

仅依赖 requests。
"""
from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

import requests


@dataclass(frozen=True)
class TempEmailInbox:
    address: str
    token: str


class TempEmailProvider(Protocol):
    def create_inbox(self, name: str) -> TempEmailInbox:
        ...

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        ...


def _proxy_from_env() -> dict | None:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    return {"http": proxy, "https": proxy} if proxy else None


# ---------------------------------------------------------------- cloudflare

class CloudflareTempEmailProvider:
    """自托管 Cloudflare 临时邮箱 (需要 api_url / admin_auth / domain)。"""

    def __init__(self, api_url: str, admin_auth: str, domain: str):
        self.api_url = api_url.rstrip("/")
        self.admin_auth = admin_auth
        self.domain = domain
        self.PROXIES = _proxy_from_env()

    def create_inbox(self, name: str) -> TempEmailInbox:
        response = requests.post(
            f"{self.api_url}/admin/new_address",
            headers={"x-admin-auth": self.admin_auth, "Content-Type": "application/json"},
            json={"name": name, "domain": self.domain, "enablePrefix": False},
            proxies=self.PROXIES,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        address = data.get("address", "")
        token = data.get("jwt", "")
        if not address or not token:
            raise RuntimeError(f"Email creation failed: {data}")
        return TempEmailInbox(address=address, token=token)

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        deadline = time.time() + timeout_seconds
        headers = {"Authorization": f"Bearer {inbox.token}"}
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{self.api_url}/api/mails?limit=5&offset=0",
                    headers=headers,
                    timeout=15,
                )
                data = response.json()
                mails = data.get("results") or data.get("data") or []
                for mail in mails:
                    mail_id = mail.get("id") or mail.get("_id")
                    if not mail_id:
                        continue
                    detail_response = requests.get(
                        f"{self.api_url}/api/mail/{mail_id}",
                        headers=headers,
                        timeout=15,
                    )
                    code = _extract_verification_code(detail_response.json().get("raw", ""))
                    if code:
                        return code
            except Exception as exc:
                print(f"  email poll: {exc}", flush=True)
            time.sleep(2)
        return None


# ---------------------------------------------------------------- duckmail

class DuckMailProvider:
    """DuckMail 服务 (Hydra API)。api_key 可选 (部分部署需要 Bearer)。"""

    def __init__(self, api_url: str, domain: str, api_key: str | None = None):
        self.api_url = api_url.rstrip("/")
        self.domain = domain
        self.api_key = api_key
        self.PROXIES = _proxy_from_env()

    def create_inbox(self, name: str) -> TempEmailInbox:
        address = f"{name}@{self.domain}"
        password = f"dm_{secrets.token_hex(8)}"

        response = requests.post(
            f"{self.api_url}/accounts",
            headers=self._account_headers(),
            json={"address": address, "password": password},
            proxies=self.PROXIES,
            timeout=15,
        )
        response.raise_for_status()

        token_response = requests.post(
            f"{self.api_url}/token",
            headers={"Content-Type": "application/json"},
            json={"address": address, "password": password},
            proxies=self.PROXIES,
            timeout=15,
        )
        token_response.raise_for_status()
        data = token_response.json()
        token = data.get("token", "")
        if not token:
            raise RuntimeError(f"DuckMail token acquisition failed: {data}")
        return TempEmailInbox(address=address, token=token)

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        deadline = time.time() + timeout_seconds
        headers = {"Authorization": f"Bearer {inbox.token}"}
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{self.api_url}/messages?page=1",
                    headers=headers,
                    proxies=self.PROXIES,
                    timeout=15,
                )
                response.raise_for_status()
                data = response.json()
                messages = data.get("hydra:member") or []
                for message in messages:
                    message_id = message.get("id")
                    if not message_id:
                        continue
                    detail_response = requests.get(
                        f"{self.api_url}/messages/{message_id}",
                        headers=headers,
                        proxies=self.PROXIES,
                        timeout=15,
                    )
                    detail_response.raise_for_status()
                    code = _extract_verification_code(_duckmail_message_body(detail_response.json()))
                    if code:
                        return code
            except Exception as exc:
                print(f"  email poll: {exc}", flush=True)
            time.sleep(2)
        return None

    def fetch_verification_links(self, inbox: TempEmailInbox, timeout_seconds: int = 120) -> list[str]:
        """轮询收件箱, 提取邮件正文里的 http(s) 链接 (NVIDIA 域优先, 新邮件优先)。"""
        deadline = time.time() + timeout_seconds
        headers = {"Authorization": f"Bearer {inbox.token}"}
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{self.api_url}/messages?page=1",
                    headers=headers,
                    proxies=self.PROXIES,
                    timeout=15,
                )
                response.raise_for_status()
                messages = response.json().get("hydra:member") or []
                for message in reversed(messages):
                    mid = message.get("id")
                    if not mid:
                        continue
                    detail = requests.get(
                        f"{self.api_url}/messages/{mid}",
                        headers=headers,
                        proxies=self.PROXIES,
                        timeout=15,
                    ).json()
                    links = re.findall(r"https?://[^\s\"'<>\\]+", _duckmail_message_body(detail))
                    nv = [l for l in links if "nvidia" in l.lower()]
                    if nv:
                        return nv
                    if links:
                        return links
            except Exception as exc:
                print(f"  email link poll: {exc}")
            time.sleep(3)
        return []

    def _account_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


# ---------------------------------------------------------------- mail.tm

class MailTmProvider:
    """mail.tm 免费临时邮箱 — 零配置, 自动发现域名。"""

    API = "https://api.mail.tm"

    def __init__(self, proxies: dict | None = None, domain: str | None = None):
        self.proxies = proxies if proxies is not None else _proxy_from_env()
        self.domain = domain or self._get_domain()

    def _get_domain(self) -> str:
        r = requests.get(f"{self.API}/domains", proxies=self.proxies, timeout=15)
        domains = r.json().get("hydra:member", [])
        return domains[0]["domain"] if domains else "emalupe.com"

    def create_inbox(self, name: str) -> TempEmailInbox:
        address = f"{name}@{self.domain}"
        password = f"tm_{secrets.token_hex(8)}"
        r = requests.post(
            f"{self.API}/accounts",
            json={"address": address, "password": password},
            proxies=self.proxies,
            timeout=15,
        )
        if r.status_code == 409:
            raise RuntimeError(f"mail.tm 账号已存在 {address} (密码已丢, 无法取 token), 请换名重试")
        r.raise_for_status()
        tr = requests.post(
            f"{self.API}/token",
            json={"address": address, "password": password},
            proxies=self.proxies,
            timeout=15,
        )
        if tr.status_code == 401:
            raise RuntimeError(f"mail.tm token 401: {address} 账号已存在但密码不符, 请换名重试")
        tr.raise_for_status()
        return TempEmailInbox(address=address, token=tr.json()["token"])

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        deadline = time.time() + timeout_seconds
        headers = {"Authorization": f"Bearer {inbox.token}"}
        while time.time() < deadline:
            try:
                r = requests.get(f"{self.API}/messages", headers=headers,
                                 proxies=self.proxies, timeout=15)
                for msg in r.json().get("hydra:member", []):
                    detail = requests.get(
                        f"{self.API}/messages/{msg['id']}",
                        headers=headers,
                        proxies=self.proxies,
                        timeout=15,
                    ).json()
                    body = detail.get("text") or (detail.get("html") or [""])[0] or ""
                    for pat in [r"\b(\d{4,6})\b", r"code[^0-9]{0,10}(\d{4,6})",
                                r"verification[^0-9]{0,10}(\d{4,6})"]:
                        m = re.search(pat, body, re.I)
                        if m:
                            return m.group(1)
            except Exception as exc:
                print(f"  email poll: {exc}")
            time.sleep(3)
        return None

    def fetch_verification_links(self, inbox: TempEmailInbox, timeout_seconds: int = 120) -> list[str]:
        """轮询收件箱, 提取邮件里的 http(s) 链接 (NVIDIA 域优先, 新邮件优先)。"""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                r = requests.get(
                    f"{self.API}/messages",
                    headers={"Authorization": f"Bearer {inbox.token}"},
                    proxies=self.proxies,
                    timeout=15,
                )
                msgs = r.json().get("hydra:member", [])
                for msg in reversed(msgs):
                    detail = requests.get(
                        f"{self.API}/messages/{msg['id']}",
                        headers={"Authorization": f"Bearer {inbox.token}"},
                        proxies=self.proxies,
                        timeout=15,
                    ).json()
                    body = (detail.get("text") or "") + " "
                    for h in (detail.get("html") or []):
                        body += re.sub(r"<[^>]+>", " ", h or "")
                    links = re.findall(r"https?://[^\s\"'<>\\]+", body)
                    nv = [l for l in links if "nvidia" in l.lower()]
                    if nv:
                        return nv
                    if links:
                        return links
            except Exception as exc:
                print(f"  email link poll: {exc}")
            time.sleep(3)
        return []


# ---------------------------------------------------------------- helpers

def _extract_verification_code(raw_message: str) -> str | None:
    """提取 123-456 形式的 6 位验证码 (NVIDIA 邮件格式)。"""
    clean = re.sub(r"=\r?\n", "", raw_message)
    index = clean.lower().find("verification code")
    if index >= 0:
        snippet = clean[index: index + 500]
        match = re.search(r"(\d{3})\s*[-–]\s*(\d{3})", snippet)
        if match:
            return match.group(1) + match.group(2)
    match = re.search(r"(?<!\d)(\d{3})[-–](\d{3})(?!\d)", clean)
    if match:
        return match.group(1) + match.group(2)
    return None


def _duckmail_message_body(detail: dict) -> str:
    parts: list[str] = []
    text = detail.get("text")
    if isinstance(text, str) and text.strip():
        parts.append(text)
    html = detail.get("html") or []
    if isinstance(html, list):
        for item in html:
            if isinstance(item, str) and item.strip():
                parts.append(item)
    elif isinstance(html, str) and html.strip():
        parts.append(html)
    return "\n".join(parts)


def build_provider(kind: str, **kwargs) -> TempEmailProvider:
    """按名称构造 provider。kind: cloudflare_temp_email | duckmail | mailtm"""
    kind = kind.lower()
    if kind == "mailtm":
        return MailTmProvider(proxies=kwargs.get("proxies"), domain=kwargs.get("domain"))
    if kind == "duckmail":
        return DuckMailProvider(
            api_url=kwargs.get("api_url", "https://api.duckmail.sbs"),
            domain=kwargs.get("domain", "duckmail.sbs"),
            api_key=kwargs.get("api_key"),
        )
    if kind == "cloudflare_temp_email":
        return CloudflareTempEmailProvider(
            api_url=kwargs["api_url"],
            admin_auth=kwargs["admin_auth"],
            domain=kwargs["domain"],
        )
    raise ValueError(f"Unsupported email provider: {kind}")
