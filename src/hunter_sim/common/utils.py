"""HunterSim 公共工具函数。

提供 RingBuffer、日志初始化、数学辅助工具等跨模块公用组件。
"""

from __future__ import annotations

import logging
import math
import sys
import threading
from collections import deque
from typing import Generic, Optional, TypeVar

import structlog

T = TypeVar("T")


# ─── 日志工厂 ─────────────────────────────────────────────────────────────────


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """获取统一格式的 logging.Logger 实例。

    日志格式：[时间][级别][模块名] 消息

    Args:
        name: 模块/类名称，作为日志记录器名称。
        level: 最低日志级别字符串。

    Returns:
        配置好的 Logger 实例。
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            fmt="[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


def get_struct_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取 structlog JSON 结构化日志记录器（生产环境使用）。

    Args:
        name: 记录器名称。

    Returns:
        BoundLogger 实例。
    """
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
