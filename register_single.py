#!/usr/bin/env python3
"""单个账号注册（子进程调用）"""
import sys, os, time, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from controllers.patchright_controller import PatchrightController
from utils import random_email, generate_strong_password
from logger import logger


def main():
    config = load_config()
    controller = PatchrightController(config)
    email = random_email()
    password = generate_strong_password()
    full_email = f"{email}{config['email_suffix']}"

    result = {"email": full_email, "password": password, "status": "fail", "bind_emails": []}

    try:
        page = controller.get_thread_page()
        if page is None:
            logger.error(f"[{full_email}] Browser failed")
            print(f"RESULT:{json.dumps(result)}")
            return

        logger.info(f"[{full_email}] 注册中...")
        success = controller.outlook_register(page, email, password)

        if success:
            result["status"] = "success"
            logger.info(f"[{full_email}] 注册成功 ✓")
        else:
            logger.warning(f"[{full_email}] 注册失败 ✗")

        controller.clean_up(page, "done_browser")
    except Exception as e:
        logger.error(f"[{full_email}] 异常: {e}")
        try:
            controller.clean_up(None, "all_browser")
        except Exception:
            pass

    print(f"RESULT:{json.dumps(result)}")


if __name__ == "__main__":
    main()
