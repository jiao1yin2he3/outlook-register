#!/usr/bin/env python3
"""批量注册 Outlook 邮箱 - 子进程隔离模式"""
import sys, os, time, random, json, signal, subprocess, urllib.request, urllib.parse

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Results")
STATUS_FILE = os.path.join(RESULTS_DIR, "batch_status.json")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

TARGET_COUNT = 50

PROXY_NODES = [
    "🇩🇪 德国 A 高级",
    "🇳🇱 荷兰 A 高级",
    "🇬🇧 英国 A 高级",
    "🇷🇺 俄罗斯 A 高级",
    "🇷🇺 俄罗斯 B 高级",
    "🇹🇷 土耳其 A 高级",
    "🇮🇳 印度 A 高级",
    "🇦🇺 澳大利亚 A 高级",
    "🇺🇸 美国 A 高级",
    "🇺🇸 美国 C 高级",
    "🇯🇵 日本 A 高级",
    "🇯🇵 日本 B 高级",
    "🇰🇷 韩国 A 高级",
    "🇭🇰 香港 A 高级",
    "🇸🇬 新加坡 A 高级",
]

running = True

def signal_handler(sig, frame):
    global running
    print("[Batch] 收到中断信号，完成当前任务后停止...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def switch_proxy(node_name: str) -> bool:
    try:
        group_name = "🚀 节点选择"
        encoded_group = urllib.parse.quote(group_name, safe="")
        url = f"http://127.0.0.1:19099/proxies/{encoded_group}"
        data = json.dumps({"name": node_name}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        print(f"[Batch] 切换节点失败: {e}")
        return False


def load_status() -> dict:
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    return {"success": 0, "fail": 0, "accounts": [], "current_node": 0}


def save_status(status: dict):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)


def register_one(node_name: str) -> dict:
    """在子进程中注册一个账号"""
    result = {"email": "", "password": "", "status": "fail", "node": node_name, "bind_emails": []}

    # 用子进程隔离浏览器
    child_script = os.path.join(SCRIPT_DIR, "register_single.py")
    try:
        proc = subprocess.run(
            [sys.executable, child_script],
            capture_output=True, text=True, timeout=180,
            cwd=SCRIPT_DIR,
        )
        output = proc.stdout + proc.stderr
        # 解析输出
        for line in output.split("\n"):
            if line.startswith("RESULT:"):
                try:
                    data = json.loads(line[7:])
                    result.update(data)
                except Exception:
                    pass
        if result["status"] != "success":
            # 检查是否有错误信息
            for line in output.split("\n"):
                if "ERROR" in line or "FAIL" in line:
                    print(f"  子进程: {line.strip()}")
    except subprocess.TimeoutExpired:
        print(f"  子进程超时 (180s)")
    except Exception as e:
        print(f"  子进程异常: {e}")

    return result


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    status = load_status()
    start_idx = status["success"] + status["fail"]

    print(f"[Batch] 目标: {TARGET_COUNT} 个账号")
    print(f"[Batch] 已完成: {status['success']} 成功, {status['fail']} 失败")
    print(f"[Batch] 从第 {start_idx + 1} 个开始")
    print(f"[Batch] 可用节点: {len(PROXY_NODES)} 个")

    node_idx = status.get("current_node", 0)

    for i in range(start_idx, TARGET_COUNT):
        if not running:
            print("[Batch] 用户中断，停止")
            break

        node_name = PROXY_NODES[node_idx % len(PROXY_NODES)]
        node_idx += 1

        print(f"\n[Batch] === 第 {i+1}/{TARGET_COUNT} 个 (节点: {node_name}) ===")

        # 切换节点
        if not switch_proxy(node_name):
            print(f"  节点切换失败，跳过")
            status["fail"] += 1
            save_status(status)
            continue

        time.sleep(2)

        # 注册
        result = register_one(node_name)

        if result["status"] == "success":
            status["success"] += 1
            print(f"  ✓ {result['email']}")
        else:
            status["fail"] += 1
            print(f"  ✗ 注册失败")

        status["accounts"].append(result)
        status["current_node"] = node_idx
        save_status(status)

        total = status["success"] + status["fail"]
        rate = status["success"] * 100 // max(total, 1)
        print(f"[Batch] 进度: {status['success']}/{total} ({rate}%)")

        wait = random.randint(3, 8)
        time.sleep(wait)

    print("\n" + "=" * 50)
    print(f"[Batch] 完成! 成功: {status['success']}, 失败: {status['fail']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
