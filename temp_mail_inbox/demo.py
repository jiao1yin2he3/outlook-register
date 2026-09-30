"""临时邮箱获取与收件 CLI 演示。

用法:
  python demo.py                      # 默认用 mail.tm (零配置)
  python demo.py --provider duckmail
  python demo.py --name mymail123    # 自定义邮箱前缀

流程: create_inbox -> 生成一个验证码/链接并等待收件箱轮询 -> 打印提取结果。
注意: 演示默认不会真的等待外部邮件 (没发信人), 只演示获取收件箱 + 轮询逻辑;
实际在 NVIDIA 注册流程中, create_inbox 之后是页面操作发邮件, 再调 poll_* 收码。
"""
from __future__ import annotations

import argparse
import json
import sys

from temp_mail_inbox import build_provider


def main() -> int:
    parser = argparse.ArgumentParser(description="临时邮箱获取与收件演示")
    parser.add_argument("--provider", default="mailtm",
                        choices=["mailtm", "duckmail", "cloudflare_temp_email"])
    parser.add_argument("--name", default="temail-demo1")
    parser.add_argument("--poll-seconds", type=int, default=10,
                        help="poll 轮询超时(秒), 演示用短超时")
    args = parser.parse_args()

    provider = build_provider(args.provider)
    inbox = provider.create_inbox(args.name)
    print(f"收件箱已创建: {inbox.address}")

    code = provider.poll_verification_code(inbox, timeout_seconds=args.poll_seconds)
    print(json.dumps({"verification_code": code}, ensure_ascii=False))

    links_fn = getattr(provider, "fetch_verification_links", None)
    if links_fn:
        links = links_fn(inbox, timeout_seconds=args.poll_seconds)
        print(json.dumps({"links": links}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
