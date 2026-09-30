"""VIL 遥测数据缓冲区（PROMPT-ENG-002-A）。

基于 RingBuffer 实现只保留最新 N 帧的线程安全缓冲区。
支持数据超时检测和外推回退逻辑。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from hunter_sim.common.models import PerceptionResult, VehicleState
from hunter_sim.common.utils import RingBuffer, get_logger

logger = get_logger(__name__)


@dataclass
class TelemetryFrame:
    """一帧完整的实车遥测数据。

    Attributes:
        vehicle_id: 实车 ID。
        timestamp: 数据时间戳（Unix 秒，实车端采集时间）。
        receive_time: 接收时间戳（Unix 秒，本地接收时间）。
        vehicle_state: 车辆定位/状态数据。
        perception: 感知结果（可选）。
        control_cmd: 控制指令回读（可选）。
        is_fresh: 数据是否新鲜（未超时）。
    """

    vehicle_id: str
    timestamp: float
    receive_time: float
    vehicle_state: VehicleState
    perception: Optional[PerceptionResult] = None
    control_cmd: Optional[dict[str, float]] = None
    is_fresh: bool = True

    @property
    def latency_ms(self) -> float:
        """数据端到端延迟（毫秒）。"""
        return (self.receive_time - self.timestamp) * 1000.0


class TelemetryBuffer:
    """VIL 遥测数据缓冲区。

    使用 RingBuffer 只保留最近 max_frames 帧数据。
    支持超时检测和获取最新有效帧。

    Args:
        max_frames: 缓冲区最大帧数。
        max_latency_ms: 超过此延迟认为帧过期（毫秒）。
    """

    def __init__(
        self,
        max_frames: int = 10,
        max_latency_ms: int = 500,
    ) -> None:
        self._buffer: RingBuffer[TelemetryFrame] = RingBuffer(max_size=max_frames)
        self._max_latency_ms: float = float(max_latency_ms)
        self._total_received: int = 0
        self._total_expired: int = 0

    def push(self, frame: TelemetryFrame) -> None:
        """推入新帧数据。

        自动检测帧是否已超时（latency > max_latency_ms），超时帧仍会被存储，
        但 is_fresh 标记为 False，供同步服务决定是否使用外推回退。

        Args:
            frame: 遥测帧数据。
        """
        frame.receive_time = time.time()
        if frame.latency_ms > self._max_latency_ms:
            frame.is_fresh = False
            self._total_expired += 1
            logger.debug(
                f"Expired frame received: vehicle={frame.vehicle_id}, "
                f"latency={frame.latency_ms:.1f}ms"
            )
        self._buffer.push(frame)
        self._total_received += 1

    def get_latest(self) -> Optional[TelemetryFrame]:
        """获取最新一帧（无论是否过期）。

        Returns:
            最新 TelemetryFrame，缓冲区为空时返回 None。
        """
        return self._buffer.latest()

    def get_latest_fresh(self) -> Optional[TelemetryFrame]:
        """获取最新且未过期的帧。

        Returns:
            最新的 is_fresh=True 帧，不存在时返回 None。
        """
        frames = self._buffer.get_range(self._buffer._max_size)
        for frame in reversed(frames):
            if frame.is_fresh:
                return frame
        return None

    def get_last_n(self, n: int) -> list[TelemetryFrame]:
        """获取最近 n 帧（从旧到新）。"""
        return self._buffer.get_range(n)

    @property
    def total_received(self) -> int:
        """累计接收帧数。"""
        return self._total_received

    @property
    def total_expired(self) -> int:
        """累计超时帧数。"""
        return self._total_expired

    @property
    def expiry_rate(self) -> float:
        """超时帧比率 (0.0 ~ 1.0)。"""
        if self._total_received == 0:
            return 0.0
        return self._total_expired / self._total_received

    @property
    def is_empty(self) -> bool:
        """缓冲区是否为空。"""
        return self._buffer.is_empty

    def clear(self) -> None:
        """清空缓冲区（场景重置时调用）。"""
        self._buffer.clear()
        self._total_received = 0
        self._total_expired = 0
        logger.info("TelemetryBuffer cleared")
