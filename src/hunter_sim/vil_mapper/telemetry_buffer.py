"""VIL 遥测数据缓冲区（PROMPT-ENG-002-A）。

基于 RingBuffer 实现只保留最新 N 帧的线程安全缓冲区。
支持数据超时检测和外推回退逻辑。

V2.7.0 P1-9：
- 环深自动推导 ``compute_auto_buffer_depth``：按 ``extrapolation_ms × 遥测频率``
  计算外推窗口所需帧数并加余量，避免手工配置与外推参数不一致；
- 收帧即产出 VIL 端到端延迟直方图样本（``observe_vil_latency``，P50/P95/P99）。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from hunter_sim.common.metrics import observe_vil_latency
from hunter_sim.common.models import PerceptionResult, VehicleState, VILSettings
from hunter_sim.common.utils import RingBuffer, get_logger

logger = get_logger(__name__)

# 自动环深的下限/上限与余量帧数（覆盖抖动 + 至少 1 个外推周期）
_AUTO_DEPTH_MARGIN_FRAMES = 5
_AUTO_DEPTH_MIN = 10
_AUTO_DEPTH_MAX = 200


def compute_auto_buffer_depth(
    extrapolation_ms: int,
    telemetry_frequency_hz: float,
    margin_frames: int = _AUTO_DEPTH_MARGIN_FRAMES,
) -> int:
    """按外推窗口与遥测频率自动推导 RingBuffer 环深（P1-9）。

    公式：``ceil(extrapolation_ms / 1000 × frequency_hz) + margin_frames``，
    结果钳制在 [``_AUTO_DEPTH_MIN``, ``_AUTO_DEPTH_MAX``]。

    Args:
        extrapolation_ms: 位姿外推时间窗口（毫秒）。
        telemetry_frequency_hz: 遥测标称频率（Hz），必须为正。
        margin_frames: 抗抖动余量帧数。

    Returns:
        推导出的环深（帧数）。
    """
    if telemetry_frequency_hz <= 0:
        raise ValueError("telemetry_frequency_hz must be positive")
    window_frames = math.ceil(extrapolation_ms / 1000.0 * telemetry_frequency_hz)
    depth = window_frames + max(0, margin_frames)
    return max(_AUTO_DEPTH_MIN, min(_AUTO_DEPTH_MAX, depth))


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
        calibration: 方案 §5.3 calibration 段（odom↔map 对齐，可选）。
        source_seq: 方案 §5.3 source_seq（实车帧序号，0 表示未提供）。
        forwarded_at: 方案 §5.3 forwarded_at（平台转发时刻，None 表示未提供）。
    """

    vehicle_id: str
    timestamp: float
    receive_time: float
    vehicle_state: VehicleState
    perception: Optional[PerceptionResult] = None
    control_cmd: Optional[dict[str, float]] = None
    is_fresh: bool = True
    calibration: Optional[dict[str, Any]] = None
    source_seq: int = 0
    forwarded_at: Optional[float] = None

    @property
    def latency_ms(self) -> float:
        """数据端到端延迟（毫秒）。"""
        return (self.receive_time - self.timestamp) * 1000.0

    @property
    def platform_forward_lag_ms(self) -> Optional[float]:
        """平台转发耗时（毫秒，方案 §10.4 VIL_E2E_LAG 的转发段）。"""
        if self.forwarded_at is None:
            return None
        return (self.forwarded_at - self.timestamp) * 1000.0


class TelemetryBuffer:
    """VIL 遥测数据缓冲区。

    使用 RingBuffer 只保留最近 max_frames 帧数据。
    支持超时检测和获取最新有效帧。

    Args:
        max_frames: 缓冲区最大帧数（``vil_settings.auto_buffer_depth`` 为真时被覆盖）。
        max_latency_ms: 超过此延迟认为帧过期（毫秒）。
        vil_settings: VIL 配置；提供且开启自动环深时，按外推窗口 × 频率推导帧数。
    """

    def __init__(
        self,
        max_frames: int = 10,
        max_latency_ms: int = 500,
        vil_settings: Optional[VILSettings] = None,
    ) -> None:
        depth = max_frames
        if vil_settings is not None and vil_settings.auto_buffer_depth:
            depth = compute_auto_buffer_depth(
                vil_settings.extrapolation_ms, vil_settings.telemetry_frequency_hz
            )
            if depth != max_frames:
                logger.info(
                    f"Auto buffer depth {depth} frames "
                    f"(extrapolation={vil_settings.extrapolation_ms}ms, "
                    f"freq={vil_settings.telemetry_frequency_hz}Hz), "
                    f"override configured max_frames={max_frames}"
                )
        self._max_frames: int = depth
        self._buffer: RingBuffer[TelemetryFrame] = RingBuffer(max_size=depth)
        self._max_latency_ms: float = float(max_latency_ms)
        self._total_received: int = 0
        self._total_expired: int = 0

    @property
    def max_frames(self) -> int:
        """当前环深（自动推导后的实际生效值）。"""
        return self._max_frames

    def push(self, frame: TelemetryFrame) -> None:
        """推入新帧数据。

        自动检测帧是否已超时（latency > max_latency_ms），超时帧仍会被存储，
        但 is_fresh 标记为 False，供同步服务决定是否使用外推回退。

        Args:
            frame: 遥测帧数据。
        """
        frame.receive_time = time.time()
        # P1-9：收帧即记录端到端延迟直方图样本（Grafana 用 histogram_quantile 求分位数）
        observe_vil_latency(frame.latency_ms)
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
