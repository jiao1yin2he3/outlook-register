"""日志模块

用标准库 logging 替代散落各处的 print()，支持分级输出和文件持久化。
"""

import logging
import os
from datetime import datetime

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR = os.path.join(_BASE_DIR, "Results", "logs")


def setup_logger(name: str = "outlook_register") -> logging.Logger:
    """创建并返回项目全局 logger。

    输出同时写入控制台和文件，文件按日期命名，存放在 Results/logs/ 下。

    Args:
        name: logger 名称

    Returns:
        配置好的 Logger 实例
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # 控制台输出：INFO 级别
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    )
    logger.addHandler(console_handler)

    # 文件输出：DEBUG 级别（包含更详细信息）
    os.makedirs(_LOG_DIR, exist_ok=True)
    log_filename = datetime.now().strftime("%Y%m%d.log")
    file_handler = logging.FileHandler(
        os.path.join(_LOG_DIR, log_filename), encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    )
    logger.addHandler(file_handler)

    return logger


# 全局 logger 实例，其他模块直接 import 使用
logger = setup_logger()
