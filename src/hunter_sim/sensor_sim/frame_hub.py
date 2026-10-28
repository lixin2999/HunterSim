"""传感器帧汇聚中心（V3.0.0）。

场景运行期间，CARLA 传感器回调把每帧数据推入 ``FrameHub``，控制面
（MJPEG 推流 / 截图）只取「最新一帧」，不排队、不积压——画面回传不需要
历史帧，保留队列只会放大内存与延迟。

数据流：

    CARLA 传感器回调 → 数据转换（data_converters）→ FrameHub.push
                                                    ├→ MJPEG / 截图（latest）
                                                    └→ McapRecorder（全量落盘）

线程安全：CARLA 回调线程（推）与控制面线程（取）并发，全部操作持锁；
``latest()`` 返回不可变 ``Frame``，调用方无需加锁。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from hunter_sim.common.metrics import SENSOR_FRAMES_TOTAL
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 支持的视频/点云流（与 ROS 话题、mcap topic 一一对应）
STREAM_RGB = "rgb"
STREAM_DEPTH = "depth"
STREAM_LIDAR = "lidar"
STREAM_IMU = "imu"

KNOWN_STREAMS: tuple[str, ...] = (STREAM_RGB, STREAM_DEPTH, STREAM_LIDAR, STREAM_IMU)

# fps 滑动窗口时长（秒）：太短会抖动，太长会掩盖掉帧
_FPS_WINDOW_S = 5.0
# 单流最多保留的时间戳样本（100Hz IMU × 5s = 500）
_MAX_FPS_SAMPLES = 600


@dataclass(frozen=True)
class Frame:
    """一帧传感器数据（不可变快照）。

    Attributes:
        stream: 流名（``rgb`` / ``depth`` / ``lidar`` / ``imu``）。
        ts: 数据时间戳（Unix 秒，CARLA 仿真时刻）。
        seq: 该流的单调递增帧序号（从 1 开始）。
        payload: 序列化后的原始字节（JPEG/PNG/点云/IMU 二进制）。
        meta: 附加元信息（宽高、点数、编码等）。
    """

    stream: str
    ts: float
    seq: int
    payload: bytes
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        """payload 字节数。"""
        return len(self.payload)


class FrameHub:
    """单实例/单场景的传感器帧汇聚中心。

    Args:
        fps_window_s: fps 统计滑动窗口（秒）。
        max_fps_samples: 每流保留的时间戳样本上限。
    """

    def __init__(
        self,
        fps_window_s: float = _FPS_WINDOW_S,
        max_fps_samples: int = _MAX_FPS_SAMPLES,
    ) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, Frame] = {}
        self._counts: dict[str, int] = {name: 0 for name in KNOWN_STREAMS}
        self._timestamps: dict[str, deque[float]] = {
            name: deque(maxlen=max_fps_samples) for name in KNOWN_STREAMS
        }
        self._fps_window_s = fps_window_s
        # 推流客户端计数在控制面维护，这里只做数据面统计

    # ── 写入 ──────────────────────────────────────────────────────────────

    def push(
        self,
        stream: str,
        ts: float,
        payload: bytes,
        meta: Optional[dict[str, Any]] = None,
    ) -> Optional[Frame]:
        """推入一帧数据（未知 stream 忽略）。

        Args:
            stream: 流名，须在 ``KNOWN_STREAMS`` 内。
            ts: 数据时间戳（Unix 秒）。
            payload: 原始字节。
            meta: 附加元信息。

        Returns:
            入队成功的 ``Frame``；未知 stream 或空 payload 返回 None。
        """
        name = stream.strip().lower()
        if name not in self._counts:
            logger.debug(f"FrameHub ignored unknown stream: {stream}")
            return None
        if not payload:
            logger.debug(f"FrameHub ignored empty payload: {name}")
            return None

        with self._lock:
            seq = self._counts[name] + 1
            self._counts[name] = seq
            frame = Frame(stream=name, ts=ts, seq=seq, payload=payload, meta=dict(meta or {}))
            self._latest[name] = frame
            stamps = self._timestamps[name]
            # 时钟回退保护：时间戳非单调时不入窗，避免 fps 出现负值
            if not stamps or ts >= stamps[-1]:
                stamps.append(ts)

        SENSOR_FRAMES_TOTAL.labels(stream=name).inc()
        return frame

    # ── 读取 ──────────────────────────────────────────────────────────────

    def latest(self, stream: str) -> Optional[Frame]:
        """取该流最新一帧（无帧或未知流返回 None）。"""
        with self._lock:
            return self._latest.get(stream.strip().lower())

    def count(self, stream: str) -> int:
        """该流累计帧数。"""
        with self._lock:
            return self._counts.get(stream.strip().lower(), 0)

    def fps(self, stream: str) -> float:
        """该流最近 ``fps_window_s`` 窗口内的接收帧率（Hz）。"""
        name = stream.strip().lower()
        with self._lock:
            stamps = list(self._timestamps.get(name, ()))
        if len(stamps) < 2:
            return 0.0
        cutoff = stamps[-1] - self._fps_window_s
        window = [ts for ts in stamps if ts >= cutoff]
        if len(window) < 2:
            return 0.0
        span = window[-1] - window[0]
        if span <= 0:
            return 0.0
        return (len(window) - 1) / span

    def stats(self) -> dict[str, int]:
        """各流累计帧数（含 ``total`` 汇总）。"""
        with self._lock:
            snapshot = dict(self._counts)
        snapshot["total"] = sum(snapshot.values())
        return snapshot

    def clear(self) -> None:
        """清空全部帧与统计（场景重置时调用）。"""
        with self._lock:
            self._latest.clear()
            for name in KNOWN_STREAMS:
                self._counts[name] = 0
                self._timestamps[name].clear()


def now_ts() -> float:
    """当前时间戳（便于测试替换）。"""
    return time.time()


__all__ = [
    "Frame",
    "FrameHub",
    "KNOWN_STREAMS",
    "STREAM_DEPTH",
    "STREAM_IMU",
    "STREAM_LIDAR",
    "STREAM_RGB",
    "now_ts",
]
