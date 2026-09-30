"""浏览器控制器基类

定义所有浏览器通用的接口和共享逻辑：
- 配置管理（通过 config.py 统一读取）
- 日志输出（通过 logger.py）
- 浏览器生命周期管理
- 注册流程
- 资源清理
- 随机化 context 创建（viewport / UA / 时区）
"""

import os
import time
import random
import threading
from typing import Any, Optional, Tuple

from faker import Faker
from abc import ABC, abstractmethod

from config import load_config, get_results_dir
from logger import logger
from geo import get_proxy_geo
from utils import human_type, human_scroll, random_email


class BaseBrowserController(ABC):
    """所有浏览器通用的接口和共享逻辑。"""

    def __init__(self, config: Optional[dict] = None):
        """
        Args:
            config: 外部传入的配置字典，若为 None 则自动读取 config.json
        """
        data = config if config is not None else load_config()

        # 核心参数
        self.wait_time: int = int(data["bot_protection_wait"] * 1000)
        self.max_captcha_retries: int = data["max_captcha_retries"]
        self.enable_oauth2: bool = data["oauth2"]["enable_oauth2"]
        self.proxy: str = data["proxy"]
        self.proxy_pool: Optional[dict] = data.get("proxy_pool")
        self.headless: bool = data.get("headless", False)
        self.email_suffix: str = data["email_suffix"]

        # OAuth2 参数
        self.oauth2_scopes = data["oauth2"].get("Scopes", [])
        self.oauth2_client_id = data["oauth2"].get("client_id", "")
        self.oauth2_redirect_url = data["oauth2"].get("redirect_url", "")

        # 线程安全
        self.thread_local = threading.local()
        self.cleanup_lock = threading.Lock()
        self.active_resources: list[tuple[Any, Any]] = []

        # 结果目录
        self.results_dir = get_results_dir()

    # ------------------------------------------------------------------
    # 抽象方法 —— 子类必须实现
    # ------------------------------------------------------------------

    @abstractmethod
    def launch_browser(self) -> Tuple[Any, Any]:
        """启动浏览器，返回 (playwright_instance, browser_instance)。"""
        ...

    @abstractmethod
    def handle_captcha(self, page: Any) -> bool:
        """验证码处理流程。"""
        ...

    @abstractmethod
    def get_thread_page(self) -> Any:
        """返回一个新页面。"""
        ...

    # ------------------------------------------------------------------
    # 共享实现
    # ------------------------------------------------------------------

    def _build_random_context_options(self, geo: Optional[dict] = None) -> dict:
        """生成浏览器 context 参数。

        不覆写 user_agent：Patchright 自带真实 Chrome UA 和匹配的 sec-ch-ua，
        手动覆写反而会导致 UA 版本与 sec-ch-ua 不一致。
        只设置时区（必须与代理 IP 一致）和语言。

        Args:
            geo: get_proxy_geo() 返回的地理信息字典
        """
        if geo:
            timezone_id = geo.get("timezone", "Asia/Shanghai")
        else:
            timezone_id = "Asia/Shanghai"

        return {
            "locale": "zh-CN",
            "timezone_id": timezone_id,
        }

    # _setup_anti_detection 已移除：Patchright 自带 navigator 属性补丁，
    # 手动覆写反而可能与其冲突，降低成功率。

    def get_current_proxy(self) -> Optional[str]:
        """返回当前线程注册用代理 URL（含 user:pass 的 context 代理）。"""
        p = getattr(self.thread_local, "current_proxy", None)
        if p:
            return p
        pc = getattr(self.thread_local, "proxy_config", None)
        if isinstance(pc, dict) and pc.get("server"):
            try:
                from proxy_pool import proxy_to_url

                return proxy_to_url(pc)
            except Exception:
                return pc.get("server")
        return self.proxy or None

    def get_thread_browser(self) -> Any:
        """获取当前线程的浏览器实例。

        首次调用启动浏览器，后续复用。
        如果浏览器崩溃（连接断开），自动重启。
        """
        # 检查缓存的浏览器是否还活着
        if hasattr(self.thread_local, "browser"):
            try:
                if not self.thread_local.browser.is_connected():
                    logger.warning("[Browser] 浏览器已断开，正在重启...")
                    delattr(self.thread_local, "browser")
            except Exception:
                logger.warning("[Browser] 浏览器状态检查失败，正在重启...")
                delattr(self.thread_local, "browser")

        if not hasattr(self.thread_local, "browser"):
            p, b = self.launch_browser()
            if not p:
                logger.error("浏览器启动失败，无法继续")
                return None

            self.thread_local.playwright = p
            self.thread_local.browser = b

            with self.cleanup_lock:
                self.active_resources.append((p, b))

        return self.thread_local.browser

    def outlook_register(self, page: Any, email: str, password: str) -> bool:
        """通用注册逻辑：模拟人类填写 Outlook 注册表单。

        Args:
            page: Playwright Page 对象
            email: 邮箱用户名（不含后缀）
            password: 密码

        Returns:
            True 表示注册成功
        """
        fake = Faker()

        lastname = fake.last_name()
        firstname = fake.first_name()
        year = str(random.randint(1980, 2008))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 28))

        def check_deadline() -> None:
            deadline = getattr(self.thread_local, "deadline", None)
            if deadline and time.monotonic() > deadline:
                raise TimeoutError("单任务超过300秒")

        # --- 1. 进入注册页面 ---
        try:
            page.goto(
                "https://outlook.live.com/mail/0/?prompt=create_account",
                timeout=40000,
                wait_until="commit",
            )
            page.get_by_text("同意并继续").wait_for(timeout=30000)
            start_time = time.time()
            page.wait_for_timeout(0.1 * self.wait_time)
            page.get_by_text("同意并继续").click(timeout=30000)
        except Exception as e:
            logger.error(f"[Error: IP] - IP质量不佳，无法进入注册界面。原因: {e}")
            return False

        # --- 2. 填写注册表单 ---
        try:
            check_deadline()
            if self.email_suffix == "@hotmail.com":
                # 等下拉框稳定后再点击
                page.get_by_text("@outlook.com").wait_for(state="visible", timeout=10000)
                page.wait_for_timeout(500)
                page.get_by_text("@outlook.com").click(timeout=10000)
                page.locator('[role="option"]:text-is("@hotmail.com")').click(timeout=10000)

            # Step 1: 邮箱名 — 失败时优先用页面建议，没有建议就自己生成，最多 3 次
            # 动态超时：基础5s，每失败一次加3s
            email_timeout = 8000
            for email_attempt in range(3):
                if email_attempt > 0:
                    email = random_email()
                    logger.info(f"[Step 1] 第 {email_attempt+1} 次尝试: {email} (超时{email_timeout}ms)")
                    page.locator('[aria-label="新建电子邮件"]').fill("")
                else:
                    logger.info(f"[Step 1] 填写邮箱: {email}{self.email_suffix}")

                human_type(
                    page, page.locator('[aria-label="新建电子邮件"]'),
                    email, base_delay=0.006 * self.wait_time,
                )
                page.locator('[data-testid="primaryButton"]').click(timeout=email_timeout)

                # 等密码框出现 = 邮箱可用
                try:
                    page.locator('[type="password"]').wait_for(timeout=email_timeout)
                    break
                except Exception:
                    pass

                # 邮箱被占用，看页面有没有提供可用建议
                logger.warning(f"[Step 1] 邮箱被占用: {email}")

                if page.get_by_text("可用选项").count() > 0:
                    all_buttons = page.locator('button')
                    btn_count = all_buttons.count()
                    clicked = False
                    suggestion_tried = 0
                    for i in range(btn_count):
                        if suggestion_tried >= 2:
                            break  # 最多试2个建议
                        text = all_buttons.nth(i).text_content() or ""
                        if len(text) > 3 and text[0].islower() and any(c.isdigit() for c in text):
                            # 跳过和原邮箱同名的建议
                            if text == email:
                                continue
                            suggestion_tried += 1
                            logger.info(f"[Step 1] 使用页面建议: {text}")
                            try:
                                all_buttons.nth(i).click(timeout=1500)
                                # 短等密码框
                                try:
                                    page.locator('[type="password"]').wait_for(timeout=3000)
                                    email = text
                                    clicked = True
                                    break
                                except Exception:
                                    # 点"下一步"再等
                                    try:
                                        page.locator('[data-testid="primaryButton"]').click(timeout=2000)
                                        page.locator('[type="password"]').wait_for(timeout=3000)
                                        email = text
                                        clicked = True
                                        break
                                    except Exception:
                                        continue  # 不行，试下一个
                            except Exception:
                                continue

                    if not clicked:
                        logger.warning("[Step 1] 建议都不行，自己生成新的")
                    else:
                        break

                # 没有建议或建议失败，下一轮用更长的超时
                email_timeout += 3000  # 失败一次加3s

            else:
                logger.error("[Step 1] 3 次都没找到可用邮箱，放弃")
                return False

            # Step 2: 密码
            check_deadline()
            logger.info("[Step 2] 填写密码")
            human_type(
                page, page.locator('[type="password"]'),
                password, base_delay=0.004 * self.wait_time,
            )
            page.wait_for_timeout(0.02 * self.wait_time)
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)

            # Step 3: 生日
            check_deadline()
            logger.info(f"[Step 3] 填写生日: {year}-{month}-{day}")
            page.wait_for_timeout(0.03 * self.wait_time)
            page.locator('[name="BirthYear"]').fill(year, timeout=10000)

            try:
                page.wait_for_timeout(0.02 * self.wait_time)
                page.locator('[name="BirthMonth"]').select_option(
                    value=month, timeout=1000
                )
                page.wait_for_timeout(0.05 * self.wait_time)
                page.locator('[name="BirthDay"]').select_option(value=day, timeout=5000)
            except Exception:
                page.locator('[name="BirthMonth"]').click(timeout=5000)
                page.wait_for_timeout(0.02 * self.wait_time)
                page.locator(f'[role="option"]:text-is("{month}月")').click(timeout=5000)
                page.wait_for_timeout(0.04 * self.wait_time)
                page.locator('[name="BirthDay"]').click(timeout=5000)
                page.wait_for_timeout(0.03 * self.wait_time)
                page.locator(f'[role="option"]:text-is("{day}日")').click(timeout=5000)

            # 生日填完后点"下一步"
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            page.wait_for_timeout(0.02 * self.wait_time)

            # Step 4: 姓名
            check_deadline()
            logger.info(f"[Step 4] 填写姓名: {firstname} {lastname}")
            page.locator("#lastNameInput").wait_for(timeout=10000)
            human_type(
                page, page.locator("#lastNameInput"),
                lastname, base_delay=0.002 * self.wait_time,
            )
            page.wait_for_timeout(0.02 * self.wait_time)
            human_type(
                page, page.locator("#firstNameInput"),
                firstname, base_delay=0.003 * self.wait_time,
            )

            # 确保总操作时间不低于 wait_time，模拟真人节奏
            if time.time() - start_time < self.wait_time / 1000:
                page.wait_for_timeout(
                    self.wait_time - (time.time() - start_time) * 1000
                )

            # Step 5: 提交表单
            check_deadline()
            logger.info("[Step 5] 提交表单...")
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            page.locator(
                'span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]'
            ).wait_for(state="detached", timeout=22000)
            page.wait_for_timeout(400)

            # --- 3. 检测异常状态 ---
            if (
                page.get_by_text("一些异常活动").count()
                or page.get_by_text(
                    "此站点正在维护，暂时无法使用，请稍后重试。"
                ).count() or page.get_by_text("我们遇到了问题").count()
                > 0
            ):
                logger.error(
                    "[Error: IP or browser] - 当前IP注册频率过快。"
                    "检查IP与是否为指纹浏览器并关闭了无头模式。"
                )
                return False

            if page.locator("iframe#enforcementFrame").count() > 0:
                logger.error("[Error: FunCaptcha] - 验证码类型错误，非按压验证码。")
                return False

            # --- 4. 处理验证码 ---
            check_deadline()
            logger.info("[Step 6] 处理验证码...")
            self.thread_local.captcha_deadline = time.monotonic() + 120
            try:
                captcha_result = self.handle_captcha(page)
            except Exception as e:
                if "Execution context was destroyed" not in str(e):
                    raise
                logger.info("[Captcha] 页面已跳转，按验证码通过处理")
                captcha_result = True
            finally:
                if hasattr(self.thread_local, "captcha_deadline"):
                    delattr(self.thread_local, "captcha_deadline")
            if not captcha_result:
                raise TimeoutError("验证码处理失败")

        except TimeoutError as e:
            logger.error(
                f"[Error: Timeout] - 操作超时。原因: {e}"
            )
            return False
        except Exception as e:
            logger.error(f"[Error: Registration] - 注册流程异常。原因: {e}")
            return False

        # --- 5. 确认注册完成 ---
        # 验证码通过后，等页面跳转到邮箱界面确认注册真正完成
        # 事件驱动：出现了立刻继续，最多等 15 秒
        # 存储实际使用的邮箱（可能被页面建议替换过）
        self.thread_local.used_email = email
        confirmed = False
        try:
            # 检查是否已经出现成功注册的文本（比如“备用电子邮件”页面）
            # 使用 is_visible() 更精确，且可以设置较短超时，避免长时间阻塞
            has_backup_email = page.get_by_text("备用电子邮件地址").is_visible(timeout=5000)
            has_protection = page.get_by_text("让我们保护你的账户").is_visible(timeout=5000)

            if has_backup_email or has_protection:
                logger.info("[Step 7] 备用电子邮件显示填写！注册成功")
                confirmed = True
            else:
                # 如果没出现上述文本，则等待“新邮件”按钮（邮箱界面）
                page.locator('[aria-label="新邮件"]').wait_for(timeout=20000)
                logger.info("[Step 7] 邮箱界面已加载，注册确认完成")
                confirmed = True

        except Exception as e:
            # 根据业务逻辑决定：超时也当成功？还是只当未知异常？
            logger.warning("[Step 7] 未按预期跳转，但验证码已通过，保存账号。异常")
            confirmed = True  # 按你的要求，验证码通过即成功

        # --- 6. 保存结果 ---
        from datetime import datetime
        filename = os.path.join(self.results_dir, "accounts.txt")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "confirmed" if confirmed else "unconfirmed"
        with open(filename, "a", encoding="utf-8") as f:
            f.write(f"{timestamp}  {email}{self.email_suffix}  {password}  [{status}]\n")
        logger.info(f"[Success] {email}{self.email_suffix} ({status})")

        # --- 7. 绑定辅助邮箱 ---
        try:
            from recovery_binder import bind_recovery_emails
            from recovery_binder.config import BindConfig

            bind_cfg = BindConfig.from_host({
                "bind_enabled": True,
                "target_count": 2,
                "timeout_sec": 180,
            })
            bind_result = bind_recovery_emails(
                page,
                account_email=f"{email}{self.email_suffix}",
                password=password,
                config=bind_cfg,
                results_dir=self.results_dir,
            )
            if bind_result.status == "ok":
                logger.info(f"[Bind] 辅助邮箱绑定成功: {bind_result.emails}")
            else:
                logger.warning(f"[Bind] 辅助邮箱绑定状态: {bind_result.status} - {bind_result.note or bind_result.error}")
        except Exception as e:
            logger.warning(f"[Bind] 辅助邮箱绑定异常，不影响注册结果: {e}")

        if not self.enable_oauth2:
            return True

        # --- 8. 等待邮箱初始化（OAuth2 模式需要收件功能） ---
        try:
            page.locator('[aria-label="新邮件"]').wait_for(timeout=32000)
            return True
        except Exception as e:
            logger.error(f"[Error: Timeout] - 邮箱未初始化，无法正常收件。原因: {e}")
            return False

    def clean_up(self, page: Any = None, cleanup_type: str = "all_browser") -> None:
        """清理浏览器资源。

        Args:
            page: 页面对象（单任务结束时传入）
            cleanup_type:
                - "done_browser": 关闭单个 context
                - "all_browser": 关闭所有浏览器实例和 Playwright 进程
        """
        if cleanup_type == "done_browser" and page:
            try:
                context = page.context
                context.close()
            except Exception as e:
                logger.debug(f"关闭 context 时出错: {e}")

        elif cleanup_type == "all_browser":
            with self.cleanup_lock:
                resources = list(self.active_resources)
                self.active_resources.clear()

            for p, b in resources:
                try:
                    b.close()
                except Exception as e:
                    logger.debug(f"关闭 browser 时出错: {e}")
                try:
                    p.stop()
                except Exception as e:
                    logger.debug(f"停止 playwright 时出错: {e}")
