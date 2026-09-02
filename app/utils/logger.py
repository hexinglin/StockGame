"""
模块名称: logger.py
说明:    日志配置，控制台 + 滚动文件，保留 30 天（参照 AutoTrade）
"""
import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(app=None, level=logging.INFO):
    """配置日志输出：控制台 + 滚动文件

    Args:
        app: Flask 应用实例（可选，同步配置 app.logger）
        level: 日志级别，默认 INFO
    """
    logger = logging.getLogger()
    logger.setLevel(level)

    # 避免重复添加 handler
    if logger.handlers:
        return

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # 控制台输出
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)
    logger.addHandler(console)

    # 滚动文件输出
    log_dir = os.environ.get("STOCKGAME_LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "stockgame.log")
    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    # 抑制第三方库的 INFO 日志
    for noisy in ['urllib3', 'apscheduler', 'socketio', 'engineio']:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if app:
        app.logger.handlers = logger.handlers
        app.logger.setLevel(level)
        app.logger.propagate = False
