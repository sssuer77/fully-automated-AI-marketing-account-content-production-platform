"""结构化日志（落控制台 + 文件；DB 留痕由 ``log_service`` 负责）。

约定
----
- 全站禁用 ``print``（ruff ``T20`` 已强制）。
- 日志字段名用 ``snake_case``；关键实体统一 ``task_id`` / ``job_id`` / ``pool``。
- ``request_id`` 由 HTTP 中间件注入，贯穿一次请求的所有日志。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog

__all__ = ["configure_logging", "get_logger"]

_CONFIGURED: bool = False


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = False,
    log_file: Path | None = None,
) -> None:
    """初始化 structlog（幂等；重复调用只更新级别）。"""
    global _CONFIGURED  # noqa: PLW0603 — 进程级单例，符合 structlog 惯例

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(format="%(message)s", handlers=handlers, level=numeric_level, force=True)

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str, **initial: Any) -> structlog.stdlib.BoundLogger:
    """取得带默认字段的 logger（未初始化时按默认参数自动初始化）。"""
    if not _CONFIGURED:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger.bind(**initial) if initial else logger
