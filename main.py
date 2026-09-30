"""融合版 Outlook 注册系统 - 主入口

支持模式：
- register: 运行浏览器注册流程
- proxy:   启动代理池服务
- email:   测试临时邮箱功能
- test:    运行组件测试
"""

import os
import sys
import json

# 确保项目根目录在 Python 路径中
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse
from datetime import datetime

from config import load_config, get_results_dir
from logger import logger
from utils import random_email, generate_strong_password
from controllers.patchright_controller import PatchrightController
from controllers.playwright_controller import PlaywrightController


def test_temp_email():
    """测试临时邮箱模块"""
    from temp_mail_inbox import build_provider

    provider = build_provider("mailtm")
    inbox = provider.create_inbox("test-merge")
    print(f"[Email] 收件箱已创建: {inbox.address}")
    print(f"[Email] Token: {inbox.token[:20]}...")

    code = provider.poll_verification_code(inbox, timeout_seconds=5)
    print(f"[Email] 验证码轮询结果: {code}")
    return inbox, code


def test_proxy_pool():
    """测试代理池模块"""
    from freeproxy.config import load as load_settings
    from freeproxy.pool import ProxyPool
    from freeproxy.store import Store
    from freeproxy.engine import Engine

    settings = load_settings()
    pool = ProxyPool(settings)
    print(f"[Proxy] 代理池已初始化，端口: {settings.port}")

    # 检查健康状态
    import asyncio
    async def _check():
        stats = pool.snapshot_stats()
        print(f"[Proxy] 池状态: {json.dumps(stats, ensure_ascii=False)}")
        return stats

    asyncio.run(_check())
    return pool


def test_browser():
    """测试浏览器能否启动"""
    config = load_config()
    controller = PlaywrightController(config)
    page = controller.get_thread_page()
    if page:
        print(f"[Browser] 页面已创建: {page.title() if hasattr(page, 'title') else 'N/A'}")
        controller.clean_up(page, "done_browser")
        return True
    return False


def test_full_flow():
    """测试完整注册流程（单次）"""
    config = load_config()
    controller = PlaywrightController(config)

    from controllers.base_controller import BaseBrowserController
    from geo import get_proxy_geo

    # 获取代理地理位置
    proxy = config.get("proxy", "http://127.0.0.1:8300")
    geo = get_proxy_geo(proxy)
    logger.info(f"[Geo] 代理位置: {geo.get('country')} ({geo.get('countryCode')})")

    # 生成邮箱和密码
    email = random_email()
    password = generate_strong_password()
    logger.info(f"[Test] 生成邮箱: {email}{config['email_suffix']}")
    logger.info(f"[Test] 生成密码: {password}")

    # 尝试启动浏览器
    page = controller.get_thread_page()
    if page is None:
        logger.error("[Test] 浏览器启动失败")
        return False

    # 获取临时邮箱用于验证
    from temp_mail_inbox import build_provider
    te_provider = build_provider(config.get("temp_email", {}).get("provider", "mailtm"))
    te_inbox = te_provider.create_inbox(email)
    logger.info(f"[Test] 临时邮箱已创建: {te_inbox.address}")

    # 执行注册
    result = controller.outlook_register(page, email, password)

    if result:
        # 等待临时邮箱接收验证码
        code = te_provider.poll_verification_code(te_inbox, timeout_seconds=30)
        logger.info(f"[Test] 验证码: {code}")

    controller.clean_up(page, "done_browser")
    return result


def main():
    parser = argparse.ArgumentParser(prog="outlook-register", description="融合版 Outlook 注册系统")
    parser.add_argument("mode", choices=["register", "proxy", "email", "test", "browser"],
                        default="test", nargs="?", help="运行模式")
    args = parser.parse_args()

    get_results_dir()

    if args.mode == "proxy":
        from freeproxy.server import run
        logger.info("启动代理池服务...")
        run()
    elif args.mode == "email":
        logger.info("测试临时邮箱模块...")
        test_temp_email()
    elif args.mode == "browser":
        logger.info("测试浏览器启动...")
        if test_browser():
            logger.info("浏览器测试通过!")
        else:
            logger.error("浏览器测试失败!")
    elif args.mode == "test":
        logger.info("运行组件测试...")
        results = {}
        try:
            test_temp_email()
            results["email"] = "PASS"
        except Exception as e:
            logger.error(f"邮箱测试失败: {e}")
            results["email"] = "FAIL"

        try:
            test_proxy_pool()
            results["proxy"] = "PASS"
        except Exception as e:
            logger.error(f"代理池测试失败: {e}")
            results["proxy"] = "FAIL"

        try:
            if test_browser():
                results["browser"] = "PASS"
            else:
                results["browser"] = "FAIL"
        except Exception as e:
            logger.error(f"浏览器测试失败: {e}")
            results["browser"] = "FAIL"

        print(f"\n{'='*40}")
        print(f"测试结果: {json.dumps(results, ensure_ascii=False)}")
        all_pass = all(v == "PASS" for v in results.values())
        print(f"总体: {'全部通过!' if all_pass else '部分失败'}")
        print(f"{'='*40}")
    else:
        # register mode
        config = load_config()
        controller = create_controller(config)
        run_concurrent_flows(controller, config, 1, 1)


def create_controller(config: dict):
    """根据配置选择浏览器控制器"""
    browser_type = config["choose_browser"]
    if browser_type == "patchright":
        return PatchrightController(config)
    elif browser_type == "playwright":
        return PlaywrightController(config)
    else:
        raise ValueError(f"不支持的浏览器类型: '{browser_type}'")


if __name__ == "__main__":
    main()
