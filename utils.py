from __future__ import annotations

"""工具函数模块

提供随机邮箱名、强密码生成，鼠标贝塞尔曲线轨迹模拟，
以及 sec-ch-ua 头部生成等反检测辅助功能。
"""

import re
import random
import string
import secrets
import math
from typing import List, Tuple
from faker import Faker


# 全局 Faker 实例
_faker = Faker()

def random_email(length: int | None = None) -> str:
    """生成邮箱用户名。

    趋势不变（简单多、复杂少），但每次比例随机波动。
    没有什么是固定的，只有趋势是稳定的。
    """
    first = _faker.first_name().lower()
    last = _faker.last_name().lower()

    # 每次调用，权重都随机波动——趋势不变，比例变化
    patterns = ["name_num", "name_year", "flast_num", "first_last_num", "complex"]
    weights = [
        random.uniform(40, 70),   # name_num: 总是占多数，但多少不定
        random.uniform(8, 25),    # name_year
        random.uniform(5, 18),    # flast_num
        random.uniform(5, 18),    # first_last_num
        random.uniform(1, 10),     # complex: 小众总是存在，多少的问题
    ]
    pattern = random.choices(patterns, weights=weights, k=1)[0]

    if pattern == "name_num":
        num = str(random.randint(1, 9999))
        return first + num

    elif pattern == "name_year":
        year = str(random.randint(1985, 2005))
        return first + year

    elif pattern == "flast_num":
        num = str(random.randint(1, 999))
        return first[0] + last + num

    elif pattern == "first_last_num":
        sep = random.choice(["", ".", "_"])
        num = str(random.randint(1, 999))
        return f"{first}{sep}{last}{num}"

    else:  # complex
        sep = random.choice([".", "_", "-"])
        num = str(random.randint(10000, 999999))
        return f"{first}{sep}{last}{num}"


def generate_strong_password(length: int | None = None) -> str:
    """生成密码，保证满足 Outlook 要求：8+位，含大小写、数字、特殊字符。

    趋势不变（常见模式多），但比例每次波动。
    """
    specials = "!@#$%"
    # 每次权重随机波动
    patterns = ["word_sym", "name_sym", "random"]
    weights = [
        random.uniform(30, 60),  # word_sym: 多数但不固定
        random.uniform(20, 45),  # name_sym
        random.uniform(3, 15),   # random: 少数，但多少不定
    ]
    pattern = random.choices(patterns, weights=weights, k=1)[0]

    while True:
        if pattern == "word_sym":
            # Summer23! — 符号在末尾，不 shuffle
            word = _faker.word().capitalize()
            num = str(random.randint(1, 9999))
            sym = random.choice(specials)
            result = word + num + sym

        elif pattern == "name_sym":
            # Michael534!
            name = _faker.first_name()
            num = str(random.randint(10, 9999))
            sym = random.choice(specials)
            result = name + num + sym

        else:  # random
            if length is None:
                length = random.randint(9, 13)
            chars = string.ascii_letters + string.digits + specials
            result = "".join(secrets.choice(chars) for _ in range(length))

        if (len(result) >= 8
                and any(c.islower() for c in result)
                and any(c.isupper() for c in result)
                and any(c.isdigit() for c in result)
                and any(c in specials for c in result)):
            return result


# ---------------------------------------------------------------------------
# 鼠标贝塞尔曲线轨迹模拟
# ---------------------------------------------------------------------------

def _bezier_curve(points: List[Tuple[float, float]], num_steps: int) -> List[Tuple[float, float]]:
    """计算 n 阶贝塞尔曲线上的离散点。

    Args:
        points: 控制点列表 [(x0,y0), (x1,y1), ...]
        num_steps: 离散化步数

    Returns:
        曲线上的点列表
    """
    n = len(points) - 1
    result: List[Tuple[float, float]] = []
    for i in range(num_steps + 1):
        t = i / num_steps
        x = y = 0.0
        for j, (px, py) in enumerate(points):
            # 伯恩斯坦基函数
            coeff = math.comb(n, j) * (t ** j) * ((1 - t) ** (n - j))
            x += coeff * px
            y += coeff * py
        result.append((x, y))
    return result


def generate_mouse_path(
    start: Tuple[float, float],
    end: Tuple[float, float],
    num_steps: int | None = None,
) -> List[Tuple[float, float]]:
    """生成从 start 到 end 的拟人鼠标移动轨迹。

    使用三阶贝塞尔曲线，在直线两侧随机偏移两个控制点，
    模拟人手移动时的自然弧度和抖动。

    Args:
        start: 起点坐标 (x, y)
        end:   终点坐标 (x, y)
        num_steps: 轨迹离散步数，默认按距离自适应

    Returns:
        轨迹点列表，第一个点近似 start，最后一个点近似 end
    """
    sx, sy = start
    ex, ey = end
    distance = math.hypot(ex - sx, ey - sy)

    if num_steps is None:
        num_steps = max(10, int(distance / 20))

    # 在直线两侧生成两个随机控制点，制造弧度
    mid_x = (sx + ex) / 2
    mid_y = (sy + ey) / 2
    offset = distance * 0.15

    ctrl1 = (
        mid_x + random.uniform(-offset, offset),
        mid_y + random.uniform(-offset, offset),
    )
    ctrl2 = (
        mid_x + random.uniform(-offset, offset),
        mid_y + random.uniform(-offset, offset),
    )

    points = [start, ctrl1, ctrl2, end]
    path = _bezier_curve(points, num_steps)

    # 加入微小随机抖动，模拟手部不稳
    # 抖动范围随距离增大，远距离移动手会更不稳
    jitter_range = max(1.0, distance * 0.008)
    jittered: List[Tuple[float, float]] = []
    for px, py in path:
        jx = px + random.uniform(-jitter_range, jitter_range)
        jy = py + random.uniform(-jitter_range, jitter_range)
        jittered.append((jx, jy))

    # 确保起点和终点精确（抖动只影响中间路径）
    jittered[0] = start
    jittered[-1] = end
    return jittered


def human_mouse_move(page, x: float, y: float, steps: int | None = None) -> None:
    """模拟人类鼠标移动到目标坐标。

    从上次结束位置出发（保持轨迹连续性），生成贝塞尔曲线轨迹，
    逐步移动到目标。移动速度有随机变化，模拟真人加速减速。

    Args:
        page: Playwright Page 对象
        x: 目标 x 坐标
        y: 目标 y 坐标
        steps: 轨迹步数
    """
    # 尝试从上次结束位置出发，保持鼠标轨迹连续性
    prev = getattr(page, "_last_mouse_pos", None)
    if prev is None:
        prev = (random.uniform(50, 800), random.uniform(50, 400))
    start = prev

    path = generate_mouse_path(start, (x, y), steps)

    for px, py in path:
        page.mouse.move(px, py)
        # 速度变化：开头慢、中间快、结尾慢（加速→匀速→减速）
        # 但加入随机性，不是每次都完美遵循
        page.wait_for_timeout(random.randint(2, 20))

    # 偶尔在目标附近停顿一下再精确到位（模拟最后的微调）
    if random.random() < 0.3:
        page.wait_for_timeout(random.randint(50, 200))
        page.mouse.move(x, y)
    else:
        page.mouse.move(x, y)

    # 记录本次结束位置，供下次使用
    page._last_mouse_pos = (x, y)


def human_type(page, locator, text: str, base_delay: float = 50, timeout: int = 10000) -> None:
    """模拟人类键盘打字节奏。

    每个字符的输入间隔服从正态分布 N(base_delay, base_delay*0.4)，
    偶尔出现"思考停顿"（300~1000ms 的长暂停）。
    比 Playwright 的 type(delay=fixed) 更拟人化，因为真人打字
    每个字符的间隔是不同的。

    Args:
        page: Playwright Page 对象
        locator: 目标输入框 locator
        text: 要输入的文本
        base_delay: 基础延迟（毫秒），约 50ms 对应快速打字，100ms 对应普通
        timeout: 定位超时（毫秒）
    """
    locator.click(timeout=timeout)
    for i, char in enumerate(text):
        page.keyboard.type(char)
        # 延迟随打字位置变化：开头稍慢（找键），中间快（熟练），结尾稍慢（检查）
        progress = i / max(len(text), 1)
        if progress < 0.2 or progress > 0.8:
            delay = max(10, int(random.gauss(base_delay * 1.3, base_delay * 0.5)))
        else:
            delay = max(10, int(random.gauss(base_delay, base_delay * 0.4)))
        page.wait_for_timeout(delay)
        # 3~8% 概率出现停顿（范围随机，不是固定值）
        if random.random() < random.uniform(0.03, 0.08):
            page.wait_for_timeout(random.randint(200, 1500))


def human_scroll(page, min_distance: int = 100, max_distance: int = 400) -> None:
    """模拟人类滚动页面行为。

    分多次滚动，每次距离和间隔随机，模拟不均匀的滚动速度。

    Args:
        page: Playwright Page 对象
        min_distance: 单次最小滚动距离
        max_distance: 单次最大滚动距离
    """
    num_scrolls = random.randint(1, 3)
    for _ in range(num_scrolls):
        page.mouse.wheel(0, random.randint(min_distance, max_distance))
        page.wait_for_timeout(random.randint(500, 2000))


# ---------------------------------------------------------------------------
# sec-ch-ua 头部生成
# ---------------------------------------------------------------------------

def extract_chrome_version(user_agent: str) -> str:
    """从 User-Agent 字符串中提取 Chrome 主版本号。

    Args:
        user_agent: User-Agent 字符串

    Returns:
        主版本号字符串（如 "125"），提取失败返回 ""
    """
    match = re.search(r'Chrome/(\d+)', user_agent)
    return match.group(1) if match else ""


def build_sec_ch_ua_headers(user_agent: str) -> dict:
    """根据 User-Agent 生成匹配的 sec-ch-ua 头部。

    Chrome 浏览器会自动发送 Client Hints 头部 (sec-ch-ua)，
    其中包含浏览器版本号。如果 UA 中写 Chrome/125 但
    sec-ch-ua 写 v="124"，检测系统会发现矛盾。

    Args:
        user_agent: User-Agent 字符串

    Returns:
        包含 sec-ch-ua 相关头部的字典
    """
    version = extract_chrome_version(user_agent)
    if not version:
        return {}

    return {
        "sec-ch-ua": f'"Google Chrome";v="{version}", '
                     f'"Chromium";v="{version}", '
                     f'"Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
