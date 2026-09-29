"""基于 loguru 的结构化日志配置。

规则（§7.1）：禁止 ``print()``；日志需包含模块名、``run_id``、``frame_id`` 上下文。
这里通过 ``logger.configure(extra=...)`` 设定上下文字段默认值，再配合
:func:`run_context` 在运行时绑定 ``run_id`` / ``frame_id``。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

# 从 hunter_sim.core.logging 直接复用 loguru 全局 logger。
__all__ = ["configure_logging", "logger", "run_context"]

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "run={extra[run_id]} frame={extra[frame_id]} | "
    "<level>{message}</level>"
)


def configure_logging(
    level: str = "INFO",
    *,
    run_id: str = "-",
    log_dir: str | Path | None = None,
    rotation: str = "10 MB",
    retention: str = "14 days",
) -> None:
    """初始化全局日志：stderr sink + 可选文件 sink。

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）。
        run_id: 默认运行 ID，作为每条日志的上下文基线。
        log_dir: 若提供，则额外写入 ``huntersim_{date}.log`` 文件。
        rotation: 日志文件轮转策略。
        retention: 日志文件保留策略。
    """
    logger.remove()
    logger.configure(extra={"run_id": run_id, "frame_id": "-"})
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=_LOG_FORMAT,
        backtrace=False,
        diagnose=False,
    )

    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        logger.add(
            directory / "huntersim_{time:YYYYMMDD}.log",
            level=level.upper(),
            format=_LOG_FORMAT,
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
            backtrace=False,
            diagnose=False,
        )


@contextmanager
def run_context(*, run_id: str | None = None, frame_id: int | str | None = None) -> Iterator[None]:
    """在 ``with`` 块内为所有日志绑定 ``run_id`` / ``frame_id`` 上下文。

    Args:
        run_id: 绑定到日志的运行 ID。
        frame_id: 绑定到日志的帧号。

    Yields:
        None；块内 ``logger`` 输出的记录自动携带这些 extra 字段。
    """
    contextual: dict[str, str] = {}
    if run_id is not None:
        contextual["run_id"] = str(run_id)
    if frame_id is not None:
        contextual["frame_id"] = str(frame_id)
    with logger.contextualize(**contextual):
        yield
