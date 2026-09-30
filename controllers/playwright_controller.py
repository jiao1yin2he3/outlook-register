"""Playwright 浏览器控制器

适用于需要专业指纹浏览器配合的场景（通过 browser_path 指定）。
本控制器实现了：
- 指纹浏览器启动
- 基于网络请求监听的验证码自动处理
- 随机化 context（viewport / UA / 时区）
"""

from typing import Optional, Tuple, Any

from playwright.sync_api import sync_playwright, Error as PlaywrightError
from .base_controller import BaseBrowserController
from logger import logger
from geo import get_proxy_geo


class PlaywrightController(BaseBrowserController):
    """使用标准 Playwright + 指纹浏览器的控制器。"""

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        data = config if config is not None else {}
        self.browser_path: str = data.get("playwright", {}).get("browser_path", "")

    # ------------------------------------------------------------------
    # 浏览器生命周期
    # ------------------------------------------------------------------

    def launch_browser(self) -> Tuple[Any, Any]:
        """启动指纹浏览器实例。"""
        try:
            p = sync_playwright().start()

            proxy_settings = None
            if self.proxy:
                proxy_settings = {"server": self.proxy, "bypass": "localhost"}

            b = p.chromium.launch(
                executable_path=self.browser_path,
                headless=False,
                args=["--lang=zh-CN"],
                proxy=proxy_settings,
            )
            logger.debug("Playwright 指纹浏览器启动成功")
            return p, b

        except Exception as e:
            logger.error(f"启动浏览器失败: {e}")
            return None, None

    def get_thread_page(self) -> Any:
        """创建一个带随机化指纹的页面。"""
        browser = self.get_thread_browser()
        if browser is None:
            return None

        self.thread_local.current_proxy = self.proxy
        # 根据代理 IP 地理位置动态匹配时区和语言
        geo = get_proxy_geo(self.proxy)
        self.thread_local.geo = geo
        context_options = self._build_random_context_options(geo)
        context = browser.new_context(**context_options)
        return context.new_page()

    # ------------------------------------------------------------------
    # 验证码处理
    # ------------------------------------------------------------------

    def handle_captcha(self, page: Any) -> bool:
        """通过监听网络请求判断验证码状态。

        Playwright 配合指纹浏览器时，验证码 iframe 由第三方服务加载，
        通过监听特定 URL 的网络请求来判断验证是否完成。
        """
        try:
            page.wait_for_event(
                "request",
                lambda req: req.url.startswith("blob:https://iframe.hsprotect.net/"),
                timeout=22000,
            )
        except PlaywrightError as e:
            logger.warning(f"[Captcha] 等待验证码 iframe 加载超时: {e}")
            return False

        page.wait_for_timeout(1800)

        for attempt in range(self.max_captcha_retries + 1):
            page.keyboard.press("Enter")
            page.wait_for_timeout(11500)
            page.keyboard.press("Enter")

            try:
                page.wait_for_event(
                    "request",
                    lambda req: req.url.startswith(
                        "https://browser.events.data.microsoft.com"
                    ),
                    timeout=8000,
                )

                try:
                    page.wait_for_event(
                        "request",
                        lambda req: req.url.startswith(
                            "https://collector-pxzc5j78di.hsprotect.net/assets/js/bundle"
                        ),
                        timeout=1700,
                    )
                    page.wait_for_timeout(2000)
                    continue

                except PlaywrightError:
                    if (
                        page.get_by_text("一些异常活动").count()
                        or page.get_by_text(
                            "此站点正在维护，暂时无法使用，请稍后重试。"
                        ).count()
                        > 0
                    ):
                        logger.error(
                            "[Error: Rate limit] - 正常通过验证码，但当前IP注册频率过快。"
                        )
                        return False
                    break

            except PlaywrightError:
                # 第一阶段超时，尝试补救
                page.wait_for_timeout(5000)
                page.keyboard.press("Enter")

                try:
                    page.wait_for_event(
                        "request",
                        lambda req: req.url.startswith(
                            "https://browser.events.data.microsoft.com"
                        ),
                        timeout=10000,
                    )
                except PlaywrightError as e:
                    logger.warning(f"[Captcha] 补救阶段请求监听超时: {e}")
                    break

                try:
                    page.wait_for_event(
                        "request",
                        lambda req: req.url.startswith(
                            "https://collector-pxzc5j78di.hsprotect.net/assets/js/bundle"
                        ),
                        timeout=4000,
                    )
                except PlaywrightError:
                    break

                page.wait_for_timeout(500)
        else:
            return False

        logger.info("[Captcha] 验证码通过")
        return True
