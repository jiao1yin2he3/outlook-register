from __future__ import annotations

"""代理 IP 地理位置查询模块

通过代理查询出口 IP 的地理位置，确保浏览器时区/语言与 IP 地理位置一致。

这是反检测中最关键的一环：如果代理 IP 在美国，但浏览器时区设为 Asia/Shanghai，
检测系统通过对比 GeoIP 数据库和 navigator 时区即可判定为机器人。
"""

import requests
from typing import Optional
from logger import logger

# 缓存：proxy_url -> geo info，避免重复查询
_PROXY_GEO_CACHE: dict[str, dict] = {}


def get_proxy_geo(proxy: Optional[str] = None) -> dict:
    """查询代理出口 IP 的地理位置信息。

    通过代理访问 ip-api.com（免费、无需 API Key）获取出口 IP 的地理信息。
    结果会缓存，因为同一代理的出口 IP 通常是稳定的。

    Args:
        proxy: 代理 URL（如 http://127.0.0.1:8001），None 表示直连

    Returns:
        {
            "country": "China",
            "countryCode": "CN",
            "timezone": "Asia/Shanghai",
            "locale": "zh-CN",
        }
    """
    cache_key = proxy or "direct"
    if cache_key in _PROXY_GEO_CACHE:
        return _PROXY_GEO_CACHE[cache_key]

    # 默认值（查询失败时使用）
    default = {
        "country": "Unknown",
        "countryCode": "CN",
        "timezone": "Asia/Shanghai",
        "locale": "zh-CN",
    }

    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        resp = requests.get(
            "http://ip-api.com/json/"
            "?fields=status,message,country,countryCode,timezone",
            proxies=proxies,
            timeout=10,
        )
        data = resp.json()

        if data.get("status") == "success":
            result = {
                "country": data.get("country", "Unknown"),
                "countryCode": data.get("countryCode", "CN"),
                "timezone": data.get("timezone", "Asia/Shanghai"),
                "locale": _country_code_to_locale(
                    data.get("countryCode", "CN")
                ),
            }
            _PROXY_GEO_CACHE[cache_key] = result
            logger.info(
                f"[Geo] 代理 {cache_key} 出口 IP: "
                f"{result['country']} ({result['countryCode']}), "
                f"时区: {result['timezone']}, 语言: {result['locale']}"
            )
            return result
        else:
            logger.warning(
                f"[Geo] IP 查询返回失败: {data.get('message', 'unknown')}"
            )
    except requests.RequestException as e:
        logger.warning(f"[Geo] 查询代理地理位置失败（网络异常）: {e}")
    except Exception as e:
        logger.warning(f"[Geo] 查询代理地理位置失败: {e}")

    _PROXY_GEO_CACHE[cache_key] = default
    logger.info("[Geo] 使用默认地理位置: CN, Asia/Shanghai, zh-CN")
    return default


def _country_code_to_locale(country_code: str) -> str:
    """将 ISO 国家代码转换为浏览器 locale。

    浏览器 locale 决定了 Accept-Language 头部和 navigator.language，
    必须与 IP 所在国家的官方语言一致。
    """
    mapping = {
        "CN": "zh-CN",
        "TW": "zh-TW",
        "HK": "zh-HK",
        "US": "en-US",
        "GB": "en-GB",
        "JP": "ja-JP",
        "KR": "ko-KR",
        "DE": "de-DE",
        "FR": "fr-FR",
        "SG": "en-SG",
        "CA": "en-CA",
        "AU": "en-AU",
        "IN": "en-IN",
        "BR": "pt-BR",
        "RU": "ru-RU",
        "VN": "vi-VN",
        "TH": "th-TH",
        "ID": "id-ID",
        "MY": "en-MY",
        "PH": "en-PH",
        "NL": "nl-NL",
        "ES": "es-ES",
        "IT": "it-IT",
    }
    return mapping.get(country_code, "en-US")
