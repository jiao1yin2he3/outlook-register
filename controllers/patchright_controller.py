"""Patchright 浏览器控制器

Patchright 是 Playwright 的反检测分支，自带指纹补丁。
本控制器实现了：
- 代理池轮换（加锁防止并发竞态）
- 按压式验证码自动处理（贝塞尔曲线轨迹 + 随机偏移）
- 随机化 context（viewport / UA / 时区）
"""

import os
import json
import random
import threading
from typing import Optional, Tuple, Any

from patchright.sync_api import sync_playwright
from .base_controller import BaseBrowserController
from logger import logger
from geo import get_proxy_geo
from utils import human_mouse_move
from proxy_pool import load_proxy_file, ProxyRotator, proxy_to_url, fetch_proxies_from_api, fetch_exclusive_proxies
from http_relay import HttpRelay


class PatchrightController(BaseBrowserController):
    """使用 Patchright（反检测 Playwright 分支）的控制器。"""

    _state_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".proxy_state.json"
    )
    _proxy_lock = threading.Lock()

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self._rotator: Optional[ProxyRotator] = None
        self._exclusive_rotator: Optional[ProxyRotator] = None
        self._api_config = config.get("proxy_api", {}) if config else {}
        self._exclusive_config = config.get("exclusive_proxy", {}) if config else {}
        self._proxy_auth = config.get("proxy_auth", None) if config else None
        # single | file | pool | auto（auto=旧优先级）
        mode = str((config or {}).get("proxy_mode") or "auto").strip().lower()
        self._proxy_mode = mode if mode in ("single", "file", "pool", "auto") else "auto"

        # 独享 IP 池（HTTP 带认证，context 级代理）
        if self._exclusive_config.get("enable"):
            proxies = fetch_exclusive_proxies(
                self._exclusive_config["api"],
                self._exclusive_config["password"],
                count=self._exclusive_config.get("count", 5),
                pro=self._exclusive_config.get("pro", 1),
                order=self._exclusive_config.get("order", 1),
            )
            if proxies:
                self._exclusive_rotator = ProxyRotator(proxies)
                self._exclusive_uses = 0
        elif self._api_config.get("enable") and self._proxy_mode in ("auto", "file"):
            proxies = fetch_proxies_from_api(self._api_config, auth=self._proxy_auth)
            if proxies:
                self._rotator = ProxyRotator(proxies)
        elif self._proxy_mode in ("auto", "file"):
            proxy_file = config.get("proxy_file", "") if config else ""
            if proxy_file:
                proxies = load_proxy_file(proxy_file, auth=self._proxy_auth)
                if proxies:
                    self._rotator = ProxyRotator(proxies)

        logger.info(f"[Proxy] mode={self._proxy_mode}")

    # ------------------------------------------------------------------
    # 代理池管理
    # ------------------------------------------------------------------

    @classmethod
    def _load_port(cls) -> Optional[int]:
        """从状态文件读取上次使用的代理端口。"""
        try:
            with open(cls._state_file) as f:
                return json.load(f).get("next_port")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @classmethod
    def _save_port(cls, port: int) -> None:
        """保存下一个代理端口到状态文件（合并写入，不覆盖其他 key）。"""
        try:
            state = {}
            try:
                with open(cls._state_file) as f:
                    state = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            state["next_port"] = port
            with open(cls._state_file, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.warning(f"[Proxy] 保存端口状态失败: {e}")

    def _pick_file_proxy(self) -> Optional[str]:
        """从 proxy_file / API 轮换器取代理。"""
        if not self._rotator:
            return None
        proxy_config = self._rotator.pick()
        if proxy_config:
            self.thread_local.proxy_config = proxy_config
            return proxy_to_url(proxy_config)
        return None

    def _pick_proxy(self) -> Optional[str]:
        """按 proxy_mode 选源：single / file / pool / auto。"""
        mode = self._proxy_mode
        if mode == "single":
            return self.proxy or None
        if mode == "file":
            return self._pick_file_proxy() or self.proxy or None
        if mode == "pool":
            return self._pick_port_proxy() or self.proxy or None

        # auto：保留旧优先级（exclusive 交替 / file > pool > single）
        has_exclusive = self._exclusive_rotator is not None and len(self._exclusive_rotator) > 0
        has_port_pool = bool(self.proxy_pool)

        if has_exclusive and has_port_pool:
            with self._proxy_lock:
                use_exclusive = getattr(self, "_next_proxy_source", "exclusive") == "exclusive"
                self._next_proxy_source = "port" if use_exclusive else "exclusive"
            proxy_url = self._pick_exclusive_proxy() if use_exclusive else self._pick_port_proxy()
            if proxy_url:
                return proxy_url
            return (self._pick_port_proxy() if use_exclusive else self._pick_exclusive_proxy()) or self.proxy

        return (
            self._pick_exclusive_proxy()
            or self._pick_file_proxy()
            or self._pick_port_proxy()
            or self.proxy
        )

    def _pick_exclusive_proxy(self) -> Optional[str]:
        """从独享 IP 池取一个代理；批次用完后刷新。"""
        if not self._exclusive_rotator:
            return None
        if getattr(self, "_exclusive_uses", 0) >= len(self._exclusive_rotator):
            self._refresh_exclusive_proxies()
            if not self._exclusive_rotator:
                return None
        proxy_config = self._exclusive_rotator.pick()
        if proxy_config:
            self.thread_local.proxy_config = proxy_config
            self._exclusive_uses = getattr(self, "_exclusive_uses", 0) + 1
            return proxy_to_url(proxy_config)
        return None

    def _refresh_exclusive_proxies(self):
        """从独享 IP 池 API 刷新代理。"""
        cfg = self._exclusive_config
        proxies = fetch_exclusive_proxies(
            cfg["api"], cfg["password"],
            count=cfg.get("count", 5),
            pro=cfg.get("pro", 1),
            order=cfg.get("order", 1),
        )
        if proxies:
            self._exclusive_rotator = ProxyRotator(proxies)
            self._exclusive_uses = 0
        else:
            logger.warning("[Proxy] 独享代理提取失败，保留旧池")

    def _pick_port_proxy(self) -> Optional[str]:
        """从本地端口池取一个代理，端口递增，到末尾回到起始端口。"""
        pool = self.proxy_pool
        if not pool:
            return None

        with self._proxy_lock:
            port = self._load_port()
            if port is None or port < pool["start"] or port > pool["end"]:
                port = pool["start"]
            next_port = port + 1 if port < pool["end"] else pool["start"]
            self._save_port(next_port)

        logger.info(f"[Proxy] 使用端口 {port} (范围 {pool['start']}-{pool['end']})")
        proxy_url = f"http://127.0.0.1:{port}"
        self.thread_local.proxy_config = {"server": proxy_url}
        return proxy_url

    def _refresh_api_proxies(self):
        """从 API 提取新的代理并刷新代理池。"""
        if not self._api_config.get("enable"):
            return
        proxies = fetch_proxies_from_api(self._api_config, auth=self._proxy_auth)
        if proxies:
            self._rotator = ProxyRotator(proxies)
            self._api_uses = 0
            logger.info(f"[Proxy] 刷新代理池，新 {len(proxies)} 个")
        else:
            logger.warning("[Proxy] API 提取失败，代理池未刷新")

    # ------------------------------------------------------------------
    # 浏览器生命周期
    # ------------------------------------------------------------------

    def launch_browser(self) -> Tuple[Any, Any]:
        """启动 Patchright 浏览器实例，通过 CLI 参数设置 SOCKS5 代理。"""
        try:
            if not hasattr(self.thread_local, "playwright"):
                self.thread_local.playwright = sync_playwright().start()
            p = self.thread_local.playwright

            args = [
                "--lang=zh-CN",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-features=WebRtcHideLocalIpsWithMdns",
            ]
            proxy_url = getattr(self.thread_local, "current_proxy", None)
            if proxy_url:
                args.append(f"--proxy-server={proxy_url}")
                logger.info(f"[Browser] 使用 SOCKS5 代理: {proxy_url[:60]}...")

            b = p.chromium.launch(headless=self.headless, args=args)
            logger.debug("Patchright 浏览器启动成功")
            return p, b
        except Exception as e:
            logger.error(f"启动浏览器失败: {e}")
            return None, None

    def get_thread_page(self) -> Any:
        """每任务重启浏览器。HTTP 带认证走 context 级代理，SOCKS5 auth 走中继。"""
        for _retry in range(2):
            old_relay = getattr(self.thread_local, "relay", None)
            if old_relay is not None:
                old_relay.stop()
                self.thread_local.relay = None

            upstream_url = self._pick_proxy()
            if upstream_url is None:
                upstream_url = self.proxy

            proxy_config = getattr(self.thread_local, "proxy_config", None)
            # fixed proxy=http://user:pass@host needs context auth (CLI --proxy-server ignores creds)
            if (
                not proxy_config
                and upstream_url
                and "://" in upstream_url
                and "@" in upstream_url
            ):
                from proxy_pool import parse_proxy_line

                parsed = parse_proxy_line(upstream_url)
                if parsed and parsed.get("username"):
                    self.thread_local.proxy_config = parsed
                    proxy_config = parsed

            use_context_proxy = False

            if upstream_url.startswith("socks5://") and "@" in upstream_url:
                relay = HttpRelay(upstream_url)
                if not relay.start():
                    logger.warning("[HttpRelay] 启动失败")
                    continue
                self.thread_local.relay = relay
                self.thread_local.current_proxy = f"http://127.0.0.1:{relay.port}"
                logger.info(f"[HttpRelay] :{relay.port} -> {relay.upstream_ip}")
            elif proxy_config and proxy_config.get("username"):
                self.thread_local.current_proxy = None
                use_context_proxy = True
                logger.info(f"[ContextProxy] {proxy_config['server']} (auth)")
            else:
                self.thread_local.current_proxy = upstream_url

            if hasattr(self.thread_local, "browser"):
                try:
                    self.thread_local.browser.close()
                except Exception:
                    pass
                delattr(self.thread_local, "browser")

            browser = self.get_thread_browser()
            if browser is None:
                return None

            geo = get_proxy_geo(upstream_url)
            self.thread_local.geo = geo
            context_options = self._build_random_context_options(geo)

            if use_context_proxy and proxy_config:
                context_options["proxy"] = {
                    "server": proxy_config["server"],
                    "bypass": "localhost",
                }
                if proxy_config.get("username"):
                    context_options["proxy"]["username"] = proxy_config["username"]
                    context_options["proxy"]["password"] = proxy_config["password"]

            try:
                context = browser.new_context(**context_options)
                page = context.new_page()
                return page
            except Exception as e:
                logger.warning(f"[Browser] new_context 失败: {e}")
                if hasattr(self.thread_local, "browser"):
                    delattr(self.thread_local, "browser")
                continue

        return None

    # ------------------------------------------------------------------
    # 验证码处理
    # ------------------------------------------------------------------

    @staticmethod
    def _get_center_with_offset(
        box: Optional[dict], offset_range: int = 10
    ) -> Tuple[float, float]:
        """计算元素中心坐标并添加随机偏移。

        Args:
            box: bounding_box() 返回值，可能为 None
            offset_range: 随机偏移范围

        Returns:
            (x, y) 坐标

        Raises:
            ValueError: box 为 None 时
        """
        if box is None:
            raise ValueError("元素 bounding_box 为 None，可能未渲染完成")

        x = box["x"] + box["width"] / 2 + random.randint(-offset_range, offset_range)
        y = box["y"] + box["height"] / 2 + random.randint(-offset_range, offset_range)
        return x, y

    def handle_captcha(self, page: Any) -> bool:
        """处理按压式验证码。

        核心原则：看页面有什么字段就做什么操作，不预判。
        一轮 = 长按 + 可能的快速点。
        只有出现"请再试一次"才算这轮结束，才能开始下一轮长按。
        最多3轮。
        """
        frame1 = page.frame_locator('iframe[title="验证质询"]')
        frame2 = frame1.frame_locator('iframe[style*="display: block"]')

        # 先等验证码 iframe 加载
        try:
            frame2.locator('[aria-label="可访问性挑战"]').wait_for(timeout=30000)
        except Exception:
            logger.error("[Captcha] 验证码 iframe 25 秒未加载，跳过")
            return False

        long_press_count = 0  # 长按次数（轮数），最多3次
        round_failed = True  # 是否可以开始新一轮长按（初始True=第一次可以按）
        press_data = {}      # 记录按压数据

        def has_text(text: str) -> bool:
            for frame in (frame1, frame2):
                try:
                    if frame.get_by_text(text).count() > 0:
                        return True
                except Exception:
                    pass
            return False

        def check_deadline() -> None:
            deadline = getattr(self.thread_local, "captcha_deadline", None)
            if deadline:
                import time as _time
                if _time.monotonic() > deadline:
                    raise TimeoutError("验证码处理超过120秒")

        # 主循环：看情况操作
        while long_press_count < 4:
            check_deadline()
            page.wait_for_timeout(200)  # 短暂轮询间隔

            # --- 检查错误 ---
            if page.get_by_text("一些异常活动").count() > 0:
                logger.error("[Captcha] IP频率过快")
                page.wait_for_timeout(2000)  # 短暂轮询间隔
                return False
            if page.get_by_text("此站点正在维护").count() > 0:
                logger.error("[Captcha] 站点维护中")
                return False

            # --- "请稍候" → 验证执行中，等待，不按 ---
            if has_text("请稍候"):
                continue  # 还在处理，等

            # --- 检查字段，决定操作 ---

            # 字段1："可访问性挑战" → 只有上一轮失败（round_failed=True）才长按
            if round_failed:
                try:
                    loc_press = frame2.locator('[aria-label="可访问性挑战"]')
                    if loc_press.count() > 0:
                        box = loc_press.bounding_box()
                        if box:
                            x, y = self._get_center_with_offset(box, offset_range=10)
                            long_press_count += 1
                            round_failed = False  # 这轮开始了，还没失败
                            logger.info(f"[Captcha] 长按第{long_press_count}次")

                            page.mouse.move(x, y)
                            page.wait_for_timeout(random.randint(200, 500))  # 反应时间
                            page.mouse.down()
                            hold_time = random.randint(2000, 5000)  # 2-5秒
                            page.wait_for_timeout(hold_time)
                            page.mouse.up()

                            press_data[f"long_hold_{long_press_count}"] = hold_time
                            # 按压后等页面处理
                            page.wait_for_timeout(1500)
                            continue  # 回到循环，看结果
                except Exception:
                    pass

            # 字段2："再次按下" → 快速点一下（不管 round_failed，这是同一轮内的操作）
            try:
                loc_again = frame2.locator('[aria-label="再次按下"]')
                if loc_again.count() > 0:
                    box = loc_again.bounding_box()
                    if box:
                        x, y = self._get_center_with_offset(box, offset_range=20)
                        logger.info("[Captcha] 快速点一下")

                        page.mouse.move(x, y)
                        page.wait_for_timeout(random.randint(100, 300))  # 快速反应
                        page.mouse.down()
                        tap_time = random.randint(300, 800)  # 短按，不要久
                        page.wait_for_timeout(tap_time)
                        page.mouse.up()

                        press_data[f"tap_{len(press_data)+1}"] = tap_time
                        # 点完后等页面处理
                        page.wait_for_timeout(1000)
                        continue  # 回到循环，看结果
            except Exception:
                pass

            # 字段3："请再试一次" → 这轮结束，可以开始下一轮长按
            if has_text("请再试一次"):
                logger.info(f"[Captcha] 第{long_press_count}轮结束（请再试一次）")
                round_failed = True  # 标记可以开始下一轮
                # 等页面回到"可访问性挑战"状态
                page.wait_for_timeout(2000)
                continue

            # 没有以上任何字段 → 可能成功了，但必须严格验证
            # 扫描期间任何验证码相关字段出现都不算通过
            # 必须完整扫描 8 秒无任何字段才确认通过
            field_reappeared = False
            for _ in range(40):  # 8 秒，不可提前结束
                check_deadline()
                page.wait_for_timeout(200)

                # 检查错误
                if page.get_by_text("一些异常活动").count() > 0:
                    logger.error("[Captcha] IP频率过快")
                    page.wait_for_timeout(2000)
                    return False
                if page.get_by_text("此站点正在维护").count() > 0:
                    logger.error("[Captcha] 站点维护中")
                    return False

                # 以下任何字段出现 = 没通过，中断扫描回到主循环

                # "请再试一次" → 这轮结束
                if has_text("请稍候"):
                    logger.info("[Captcha] 扫描中：请稍候")
                    field_reappeared = True
                    break
                if has_text("请再试一次"):
                    logger.info("[Captcha] 扫描中：请再试一次")
                    round_failed = True
                    field_reappeared = True
                    break

                # "再次按下" → 继续点
                try:
                    if frame2.locator('[aria-label="再次按下"]').count() > 0:
                        logger.info("[Captcha] 扫描中：再次按下")
                        field_reappeared = True
                        break
                except Exception:
                    pass

                # "可访问性挑战" → 还在验证码界面
                try:
                    if frame2.locator('[aria-label="可访问性挑战"]').count() > 0:
                        logger.info("[Captcha] 扫描中：验证码仍在")
                        round_failed = True
                        field_reappeared = True
                        break
                except Exception:
                    pass

            if not field_reappeared:
                # 8 秒内无任何验证码字段 → 确认通过
                if page.get_by_text("让我们保护你的账户").count() or page.get_by_text("备用电子邮件地址").count() > 0:
                    logger.info("[Captcha] 提示输入辅助邮箱，添加备用电子邮箱，注册成功")
                    press_data["long_press_count"] = long_press_count
                    self.thread_local.captcha_data = press_data
                    return True
                logger.info("[Captcha] 验证码通过（8秒无字段）")
                press_data["long_press_count"] = long_press_count
                self.thread_local.captcha_data = press_data
                return True

            # 有字段出现，回到主循环
            continue

        # 3轮都没成功
        logger.error("[Captcha] 3轮长按未通过")
        return False

    def clean_up(self, page: Any = None, cleanup_type: str = "all_browser") -> None:
        if cleanup_type == "done_browser":
            if page:
                try:
                    page.context.close()
                except Exception:
                    pass
            if hasattr(self.thread_local, "browser"):
                try:
                    self.thread_local.browser.close()
                except Exception:
                    pass
                delattr(self.thread_local, "browser")
            if hasattr(self.thread_local, "captcha_deadline"):
                delattr(self.thread_local, "captcha_deadline")
            relay = getattr(self.thread_local, "relay", None)
            if relay is not None:
                relay.stop()
                self.thread_local.relay = None

        elif cleanup_type == "all_browser":
            if hasattr(self.thread_local, "browser"):
                try:
                    self.thread_local.browser.close()
                except Exception:
                    pass
            if hasattr(self.thread_local, "playwright"):
                try:
                    self.thread_local.playwright.stop()
                except Exception:
                    pass
            relay = getattr(self.thread_local, "relay", None)
            if relay is not None:
                relay.stop()
                self.thread_local.relay = None
