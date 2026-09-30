from __future__ import annotations

"""OAuth2 令牌获取模块

使用 PKCE 流程从 Microsoft 身份平台获取 access_token 和 refresh_token。

优化点：
- 不再重复读取 config.json，改为接收外部传入的配置
- 代理与浏览器一致（从外部传入，而非系统环境变量）
- 捕获具体异常而非裸 except
- 响应体做 JSON 解析保护
"""

import base64
import string
import hashlib
import secrets
from typing import Optional, Tuple, Any

import requests
from datetime import datetime
from urllib.parse import quote, parse_qs

from logger import logger


def generate_code_verifier(length: int = 128) -> str:
    """生成 PKCE code_verifier。"""
    alphabet = string.ascii_letters + string.digits + "-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_code_challenge(code_verifier: str) -> str:
    """从 code_verifier 生成 S256 code_challenge。"""
    sha256_hash = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(sha256_hash).decode().rstrip("=")


def handle_oauth2_form(page: Any, email: str) -> None:
    """自动填写 OAuth2 登录表单并同意授权。"""
    try:
        page.locator('[name="loginfmt"]').fill(email, timeout=20000)
        page.locator("#idSIButton9").click(timeout=7000)

        consent_btn = page.locator('[data-testid="appConsentPrimaryButton"]')
        consent_btn.wait_for(state="visible", timeout=20000)
        consent_btn.click(timeout=10000)
    except Exception as e:
        logger.debug(f"[OAuth2] 表单填写/同意环节异常（可能已自动跳过）: {e}")


def get_access_token(
    page: Any,
    email: str,
    config: dict,
    proxy: Optional[str] = None,
    max_retries: int = 3,
) -> Tuple:
    """获取 OAuth2 access_token 和 refresh_token。

    Args:
        page: Playwright Page 对象
        email: 邮箱用户名（不含后缀）
        config: 配置字典
        proxy: 代理 URL（与浏览器使用的代理保持一致）
        max_retries: 最大重试次数

    Returns:
        (refresh_token, access_token, expire_at) 成功
        (False, False, False) 失败
    """
    for attempt in range(max_retries):
        result = _try_get_access_token(page, email, config, proxy)
        if result[0] is not False:
            return result
        logger.debug(f"[OAuth2] 第 {attempt + 1}/{max_retries} 次尝试失败，重试中...")

    logger.error("[OAuth2] 获取令牌失败，已达最大重试次数")
    return False, False, False


def _try_get_access_token(
    page: Any,
    email: str,
    config: dict,
    proxy: Optional[str] = None,
) -> Tuple:
    """单次尝试获取令牌。"""
    oauth2 = config["oauth2"]
    scopes = oauth2["Scopes"]
    client_id = oauth2["client_id"]
    redirect_url = oauth2["redirect_url"]
    email_suffix = config["email_suffix"]

    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_url,
        "scope": " ".join(scopes),
        "response_mode": "query",
        "prompt": "select_account",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    authorize_url = (
        f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"
        f"{'&'.join(f'{k}={quote(v)}' for k, v in params.items())}"
    )

    captured_url: Optional[str] = None

    def on_request(request: Any) -> None:
        nonlocal captured_url
        if redirect_url in request.url and "code=" in request.url:
            captured_url = request.url

    page.on("request", on_request)

    try:
        try:
            page.wait_for_timeout(250)
            page.goto(authorize_url, timeout=30000)
        except Exception as e:
            logger.debug(f"[OAuth2] 导航到授权页失败: {e}")
            return False, False, False

        handle_oauth2_form(page, f"{email}{email_suffix}")

        max_refreshes = 1
        refresh_count = 0
        refresh_interval = 200

        for i in range(400):
            page.wait_for_timeout(100)
            if captured_url:
                break

            if i > 0 and i % refresh_interval == 0:
                if refresh_count >= max_refreshes:
                    return False, False, False
                refresh_count += 1
                try:
                    page.reload(timeout=10000)
                except Exception:
                    pass
        else:
            return False, False, False

    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass

    if not captured_url or "code=" not in captured_url:
        return False, False, False

    try:
        auth_code = parse_qs(captured_url.split("?")[1])["code"][0]
    except (IndexError, KeyError) as e:
        logger.error(f"[OAuth2] 解析 authorization code 失败: {e}")
        return False, False, False

    # 构造代理参数（与浏览器保持一致）
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        response = requests.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "code": auth_code,
                "redirect_uri": redirect_url,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "scope": " ".join(scopes),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=proxies,
            timeout=30,
        )
        response.raise_for_status()
        tokens = response.json()

        if "refresh_token" in tokens:
            return (
                tokens["refresh_token"],
                tokens.get("access_token", ""),
                datetime.now().timestamp() + tokens["expires_in"],
            )

        logger.error(f"[OAuth2] 响应中无 refresh_token: {tokens}")
    except requests.RequestException as e:
        logger.error(f"[OAuth2] Token 请求失败: {e}")
    except (ValueError, KeyError) as e:
        logger.error(f"[OAuth2] Token 响应解析失败: {e}")

    return False, False, False
