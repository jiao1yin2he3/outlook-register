"""统一配置管理模块

将原本分散在 4 个文件中各自读取 config.json 的逻辑集中到此处，
确保配置只读取一次，并提供类型安全的访问和校验。
"""

import json
import os
from typing import Any, Optional, Dict

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_BASE_DIR, "config.json")


class ConfigValidationError(Exception):
    """配置校验失败时抛出"""


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """读取并校验 config.json，返回完整配置字典。

    Args:
        path: 自定义配置文件路径，默认为项目根目录下的 config.json

    Returns:
        校验后的配置字典

    Raises:
        FileNotFoundError: 配置文件不存在
        ConfigValidationError: 配置项缺失或值非法
    """
    config_path = path or _CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    _validate(data)
    return data


def _validate(data: Dict[str, Any]) -> None:
    """校验配置项的完整性和合法性。"""

    # 必填外层参数
    required_keys = ["choose_browser", "email_suffix", "proxy",
                     "bot_protection_wait", "max_captcha_retries",
                     "concurrent_flows", "max_tasks"]
    for key in required_keys:
        if key not in data:
            raise ConfigValidationError(f"缺少必填配置项: '{key}'")

    # choose_browser 只能是两个值
    if data["choose_browser"] not in ("patchright", "playwright"):
        raise ConfigValidationError(
            f"choose_browser 只能为 'patchright' 或 'playwright'，"
            f"当前为 '{data['choose_browser']}'"
        )

    # email_suffix 校验
    if data["email_suffix"] not in ("@outlook.com", "@hotmail.com"):
        raise ConfigValidationError(
            f"email_suffix 只能为 '@outlook.com' 或 '@hotmail.com'，"
            f"当前为 '{data['email_suffix']}'"
        )

    # 数值范围校验
    if not isinstance(data["bot_protection_wait"], (int, float)) or data["bot_protection_wait"] < 0:
        raise ConfigValidationError("bot_protection_wait 必须为非负数")

    if not isinstance(data["max_captcha_retries"], int) or data["max_captcha_retries"] < 0:
        raise ConfigValidationError("max_captcha_retries 必须为非负整数")

    if not isinstance(data["concurrent_flows"], int) or data["concurrent_flows"] < 1:
        raise ConfigValidationError("concurrent_flows 必须为正整数")

    if not isinstance(data["max_tasks"], int) or data["max_tasks"] < 0:
        raise ConfigValidationError("max_tasks 必须为非负整数（0=自动按端口数）")

    # OAuth2 校验
    oauth2 = data.get("oauth2", {})
    if oauth2.get("enable_oauth2"):
        for key in ("client_id", "redirect_url"):
            if not oauth2.get(key):
                raise ConfigValidationError(f"启用 OAuth2 后必须填写 '{key}'")
        if not oauth2.get("Scopes"):
            raise ConfigValidationError("启用 OAuth2 后必须填写 Scopes")

    # playwright 模式下 browser_path 校验
    if data["choose_browser"] == "playwright":
        pw = data.get("playwright", {})
        if not pw.get("browser_path"):
            raise ConfigValidationError("使用 playwright 模式时必须填写 browser_path")


def get_results_dir() -> str:
    """返回结果存储目录的绝对路径。"""
    results_dir = os.path.join(_BASE_DIR, "Results")
    os.makedirs(results_dir, exist_ok=True)
    return results_dir
