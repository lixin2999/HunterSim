"""HunterSim 公共工具函数。

提供 RingBuffer、日志初始化、数学辅助工具等跨模块公用组件。

V2.7.0 P1-11：``configure_logging`` 统一日志出口——级别/JSON 结构化/文件轮转
全部由配置驱动，request_id 通过 structlog contextvars 贯穿全部模块日志。
"""

from __future__ import annotations

import logging
import logging.handlers
import math
import os
import sys
import threading
from collections import deque
from typing import Any, Generic, Optional, TypeVar

import structlog

T = TypeVar("T")


# ─── 日志工厂 ─────────────────────────────────────────────────────────────────

# 全局默认日志级别（由应用入口按 HUNTER_SIM_LOG_LEVEL 注入）
_DEFAULT_LEVEL: int = logging.INFO

# configure_logging 已执行标记：模块 logger 不再自行挂 handler（P1-11）
_LOG_CONFIGURED: bool = False


class _RequestIdFilter(logging.Filter):
    """把 structlog contextvars 里的 request_id 注入 stdlib LogRecord（P1-11）。

    中间件通过 ``bind_request_id`` 绑定；无请求上下文时填 ``-``，保证
    文本格式的 ``%(request_id)s`` 字段永不存在缺字段异常。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            rid = structlog.contextvars.get_contextvars().get("request_id")
            record.request_id = rid if isinstance(rid, str) and rid else "-"
        return True


def _make_text_formatter() -> logging.Formatter:
    """可读文本格式：[时间][级别][模块名][rid] 消息。"""
    return logging.Formatter(
        fmt="[%(asctime)s][%(levelname)s][%(name)s][%(request_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def bind_request_id(request_id: str) -> None:
    """绑定当前请求的 request_id（structlog contextvars，P1-11）。"""
    structlog.contextvars.bind_contextvars(request_id=request_id)


def clear_request_id() -> None:
    """清除当前上下文的 request_id 绑定（请求结束时调用）。"""
    structlog.contextvars.unbind_contextvars("request_id")


def configure_logging(
    log_level: str = "INFO",
    json_logs: bool = False,
    log_file: str = "",
    log_max_bytes: int = 10_485_760,
    log_backup_count: int = 5,
) -> int:
    """统一日志装配（P1-11，应用入口调用，幂等）。

    - 级别：``HUNTER_SIM_LOG_LEVEL`` 同步全部模块 logger；
    - 出口：摘除各模块 logger 自有 handler，统一挂到 ``hunter_sim`` 父 logger，
      避免多次调用时重复输出；
    - ``json_logs``：为真时 stdlib 与 structlog 两套日志均经
      ``ProcessorFormatter`` 渲染为 JSON（含 request_id/时间戳/级别）；
    - ``log_file``：非空时额外接入 ``RotatingFileHandler``（按字节轮转，
      容器部署可保持 stdout 另选日志驱动接管）。

    Args:
        log_level: DEBUG/INFO/WARNING/ERROR/CRITICAL。
        json_logs: 是否以 JSON 渲染。
        log_file: 轮转日志文件路径（空表示仅 stdout）。
        log_max_bytes: 单文件最大字节数。
        log_backup_count: 保留历史文件数。

    Returns:
        实际生效的 logging 整型级别。
    """
    global _LOG_CONFIGURED  # noqa: PLW0603  进程级配置，全局唯一
    level = set_default_log_level(log_level)
    parent = logging.getLogger("hunter_sim")

    # 幂等：先清空父 logger 已有出口，再摘除模块级 handler
    for handler in list(parent.handlers):
        parent.removeHandler(handler)
    for name in list(logging.root.manager.loggerDict):
        if name == "hunter_sim" or name.startswith("hunter_sim."):
            module_logger = logging.getLogger(name)
            for handler in list(module_logger.handlers):
                module_logger.removeHandler(handler)

    if json_logs:
        timestamper = structlog.processors.TimeStamper(fmt="iso")
        pre_chain: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.ExtraAdder(),
            timestamper,
        ]
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.add_log_level,
                timestamper,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.make_filtering_bound_logger(level),
            cache_logger_on_first_use=True,
        )
        formatter: logging.Formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=pre_chain,
        )
    else:
        formatter = _make_text_formatter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(_RequestIdFilter())
    parent.addHandler(stdout_handler)

    if log_file:
        directory = os.path.dirname(log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=log_max_bytes,
            backupCount=log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_RequestIdFilter())
        parent.addHandler(file_handler)

    parent.setLevel(level)
    _LOG_CONFIGURED = True
    return level


def set_default_log_level(level: str | int) -> int:
    """设置全局默认日志级别，并同步已创建的 hunter_sim 模块 logger。

    ``get_logger`` 默认取此级别，避免“配置了 HUNTER_SIM_LOG_LEVEL 但不生效”。

    Args:
        level: 级别名（DEBUG/INFO/WARNING/ERROR/CRITICAL）或 logging 整型级别。

    Returns:
        实际生效的 logging 整型级别。
    """
    global _DEFAULT_LEVEL  # noqa: PLW0603  进程级配置，全局唯一
    resolved = getattr(logging, level.upper(), logging.INFO) if isinstance(level, str) else int(level)
    _DEFAULT_LEVEL = resolved
    logging.getLogger().setLevel(resolved)
    # 已创建的模块 logger 都显式设过级别，需一并刷新
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("hunter_sim"):
            logging.getLogger(name).setLevel(resolved)
    return resolved


def get_logger(name: str, level: Optional[str] = None) -> logging.Logger:
    """获取统一格式的 logging.Logger 实例。

    日志格式：[时间][级别][模块名][rid] 消息；``configure_logging`` 已执行时
    不再挂模块级 handler，统一由 ``hunter_sim`` 父 logger 出口（P1-11）。

    Args:
        name: 模块/类名称，作为日志记录器名称。
        level: 最低日志级别字符串；None 表示使用全局默认级别。

    Returns:
        配置好的 Logger 实例。
    """
    logger = logging.getLogger(name)
    if not logger.handlers and not _LOG_CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_make_text_formatter())
        handler.addFilter(_RequestIdFilter())
        logger.addHandler(handler)
    if level is not None:
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    else:
        logger.setLevel(_DEFAULT_LEVEL)
    return logger


def get_struct_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取 structlog JSON 结构化日志记录器（生产环境使用）。

    ``configure_logging`` 已全局装配时直接复用全局配置（P1-11），
    否则按保守 JSON 管线局部配置（保持旧行为）。

    Args:
        name: 记录器名称。

    Returns:
        BoundLogger 实例。
    """
    if not _LOG_CONFIGURED:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        )
    bound_logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return bound_logger


# ─── RingBuffer ───────────────────────────────────────────────────────────────


class RingBuffer(Generic[T]):
    """线程安全的固定大小环形缓冲区。

    只保留最新 N 帧数据，超出时自动覆盖最旧数据。
    用于 VIL 遥测数据缓冲和传感器数据处理。

    Args:
        max_size: 缓冲区最大容量（帧数）。
    """

    def __init__(self, max_size: int = 10) -> None:
        if max_size < 1:
            raise ValueError("RingBuffer max_size must be >= 1")
        self._max_size: int = max_size
        self._buffer: deque[T] = deque(maxlen=max_size)
        self._lock: threading.Lock = threading.Lock()

    def push(self, item: T) -> None:
        """压入新数据，超容量时覆盖最旧项。"""
        with self._lock:
            self._buffer.append(item)

    def latest(self) -> Optional[T]:
        """返回最新一帧数据，缓冲区为空时返回 None。"""
        with self._lock:
            return self._buffer[-1] if self._buffer else None

    def get_range(self, last_n: int) -> list[T]:
        """返回最近 last_n 帧数据（从旧到新）。

        Args:
            last_n: 取回帧数，超出缓冲区大小时返回全部。

        Returns:
            数据列表，缓冲区为空时返回空列表。
        """
        with self._lock:
            items = list(self._buffer)
            return items[-last_n:] if last_n < len(items) else items

    @property
    def size(self) -> int:
        """当前缓冲区数据量。"""
        with self._lock:
            return len(self._buffer)

    @property
    def is_empty(self) -> bool:
        """缓冲区是否为空。"""
        with self._lock:
            return len(self._buffer) == 0

    def clear(self) -> None:
        """清空缓冲区。"""
        with self._lock:
            self._buffer.clear()


# ─── 数学/几何辅助 ────────────────────────────────────────────────────────────


def deg_to_rad(degrees: float) -> float:
    """角度转弧度。"""
    return math.radians(degrees)


def rad_to_deg(radians: float) -> float:
    """弧度转角度。"""
    return math.degrees(radians)


def normalize_angle_rad(angle: float) -> float:
    """将弧度归一化到 [-pi, pi] 范围内。

    Args:
        angle: 任意弧度值。

    Returns:
        归一化后的弧度值。
    """
    return (angle + math.pi) % (2 * math.pi) - math.pi


def lerp(a: float, b: float, t: float) -> float:
    """线性插值。

    Args:
        a: 起始值。
        b: 终止值。
        t: 插值因子，范围 [0, 1]。

    Returns:
        插值结果。
    """
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t


def smoothstep(t: float) -> float:
    """Hermite 平滑插值（S-curve）。

    Args:
        t: 输入值，范围 [0, 1]。

    Returns:
        平滑输出值。
    """
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def euclidean_distance_2d(x1: float, y1: float, x2: float, y2: float) -> float:
    """2D 欧几里得距离。"""
    return math.hypot(x2 - x1, y2 - y1)


def rmse(values: list[float]) -> float:
    """计算均方根误差（RMSE）。

    Args:
        values: 误差值列表。

    Returns:
        RMSE 值，空列表时返回 0.0。
    """
    if not values:
        return 0.0
    return math.sqrt(sum(v * v for v in values) / len(values))
