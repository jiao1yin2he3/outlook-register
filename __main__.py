"""统一入口：支持启动代理池服务或运行注册流程。"""
import sys
import argparse

def main():
    parser = argparse.ArgumentParser(prog="outlook-register", description="融合版 Outlook 注册系统")
    parser.add_argument("mode", choices=["register", "proxy", "email"], help="运行模式")
    args = parser.parse_args()

    if args.mode == "register":
        sys.argv = [sys.argv[0], "--mode", "register"]
        from main import run_concurrent_flows, create_controller, load_config
        config = load_config()
        controller = create_controller(config)
        run_concurrent_flows(controller, config, 1, 1)
    elif args.mode == "proxy":
        from freeproxy.server import run
        run()
    elif args.mode == "email":
        from temp_mail_inbox.demo import main as email_main
        email_main()

if __name__ == "__main__":
    main()
