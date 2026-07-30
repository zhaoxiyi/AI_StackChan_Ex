"""
日志配置模块 - loguru 会话级追踪

功能:
  - 控制台 + 文件双输出
  - 自动注入 session_id（默认 "system"）
  - get_session_logger(sid) 绑定会话 ID
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(
    level: str = "INFO",
    file_enabled: bool = True,
    file_path: str = "logs/stackchan_server.log",
    file_rotation: str = "100 MB",
    file_retention: str = "7 days",
) -> None:
    """
    初始化日志系统。

    必须在应用启动时最先调用。
    """

    # 确保每条日志记录都有默认 session_id，格式串不再 KeyError
    logger.configure(
        patcher=lambda record: record["extra"].setdefault("session_id", "system")
    )

    # 移除默认 handler
    logger.remove()

    # 控制台 handler（彩色）
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[session_id]: <12}</cyan> | "
            "<level>{message}</level>"
        ),
        level=level,
        colorize=True,
        backtrace=True,
        diagnose=True,
    )

    # 文件 handler
    if file_enabled:
        log_path = Path(file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_path),
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
                "{extra[session_id]: <12} | {name}:{function}:{line} | {message}"
            ),
            level=level,
            rotation=file_rotation,
            retention=file_retention,
            encoding="utf-8",
            backtrace=True,
            diagnose=False,
        )

    logger.info("日志系统初始化完成")


def get_session_logger(session_id: str = "system"):
    """
    获取绑定会话 ID 的 logger。

    用法:
        slog = get_session_logger("abc123")
        slog.info("会话事件")   # 自动打印 session_id=abc123

    Args:
        session_id: 会话标识符

    Returns:
        绑定了 session_id 的 logger
    """
    return logger.bind(session_id=session_id)
