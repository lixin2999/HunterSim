"""VIL 数据健康监控（PROMPT-ENG-002-A）。

监控遥测数据的延迟、丢帧率、中断状态，在异常时触发告警和降级处理。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from hunter_sim.common.models import VILSettings
from hunter_sim.common.utils import get_logger
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame

logger = get_logger(__name__)


class DataHealthStatus(str, Enum):
    """数据健康状态枚举。"""

    OK = "ok"              # 正常
    DEGRADED = "degraded"  # 延迟升高，使用外推
    INTERRUPTED = "interrupted"  # 数据中断，仿真暂停


@dataclass
class DataHealthReport:
    """数据健康状态报告快照。

    Attributes:
        status: 当前健康状态。
        current_latency_ms: 最新帧延迟（毫秒）。
        avg_latency_ms: 平均延迟（毫秒）。
        expiry_rate: 超时帧比率 (0~1)。
        fps: 当前有效数据接收帧率。
        frames_received: 累计接收帧数。
        frames_expired: 累计超时帧数。
        last_frame_age_ms: 距最后一帧接收的时间间隔（毫秒）。
        timestamp: 报告生成时间。
    """

    status: DataHealthStatus
    current_latency_ms: float
    avg_latency_ms: float
    expiry_rate: float
    fps: float
    frames_received: int
    frames_expired: int
    last_frame_age_ms: float
    timestamp: float = field(default_factory=time.time)


class DataHealthMonitor:
    """VIL 数据健康监控器。

    定期评估遥测数据流质量，当延迟超过 warning_latency_ms 时降级，
    超过 max_data_latency_ms 时报告中断（触发仿真暂停）。

    Args:
        settings: VIL 参数配置。
        buffer: 遥测数据缓冲区。
        on_degraded: 状态变为降级时的回调。
        on_interrupted: 状态变为中断时的回调。
        on_recovered: 状态恢复正常时的回调。
    """

    def __init__(
        self,
        settings: VILSettings,
        buffer: TelemetryBuffer,
        on_degraded: Optional[Callable[[DataHealthReport], None]] = None,
        on_interrupted: Optional[Callable[[DataHealthReport], None]] = None,
        on_recovered: Optional[Callable[[DataHealthReport], None]] = None,
    ) -> None:
        self._settings = settings
        self._buffer = buffer
        self._on_degraded = on_degraded
        self._on_interrupted = on_interrupted
        self._on_recovered = on_recovered

        self._current_status: DataHealthStatus = DataHealthStatus.OK
        self._last_frame_time: float = 0.0
        self._latency_window: list[float] = []
        self._max_window: int = 100
        self._frame_count_window: list[float] = []  # 时间戳列表，用于 FPS 计算

    def check(self) -> DataHealthReport:
        """执行一次健康检查并返回报告。

        应在每次仿真 tick 前调用。

        Returns:
            DataHealthReport 快照。
        """
        now = time.time()
        latest: Optional[TelemetryFrame] = self._buffer.get_latest()

        # 延迟计算
        current_latency_ms: float = 0.0
        if latest is not None:
            current_latency_ms = latest.latency_ms
            self._last_frame_time = latest.receive_time
            self._latency_window.append(current_latency_ms)
            if len(self._latency_window) > self._max_window:
                self._latency_window.pop(0)
            self._frame_count_window.append(now)
            # 保留最近 5 秒时间戳用于 FPS 估算
            cutoff = now - 5.0
            while self._frame_count_window and self._frame_count_window[0] < cutoff:
                self._frame_count_window.pop(0)

        avg_latency_ms: float = (
            sum(self._latency_window) / len(self._latency_window)
            if self._latency_window
            else 0.0
        )

        # FPS 估算
        fps: float = 0.0
        if len(self._frame_count_window) >= 2:
            span = self._frame_count_window[-1] - self._frame_count_window[0]
            if span > 0:
                fps = len(self._frame_count_window) / span

        last_frame_age_ms: float = (now - self._last_frame_time) * 1000.0 if self._last_frame_time else 99999.0

        # 状态判断
        new_status = self._determine_status(current_latency_ms, last_frame_age_ms)
        report = DataHealthReport(
            status=new_status,
            current_latency_ms=current_latency_ms,
            avg_latency_ms=avg_latency_ms,
            expiry_rate=self._buffer.expiry_rate,
            fps=fps,
            frames_received=self._buffer.total_received,
            frames_expired=self._buffer.total_expired,
            last_frame_age_ms=last_frame_age_ms,
        )

        # 触发状态变化回调
        if new_status != self._current_status:
            self._handle_status_change(new_status, report)
            self._current_status = new_status

        return report

    @property
    def current_status(self) -> DataHealthStatus:
        """当前健康状态（只读）。"""
        return self._current_status

    def _determine_status(self, latency_ms: float, frame_age_ms: float) -> DataHealthStatus:
        """根据延迟判断健康状态。"""
        # 数据中断：超过 max_data_latency_ms 没有新帧
        if frame_age_ms > self._settings.max_data_latency_ms:
            return DataHealthStatus.INTERRUPTED

        # 降级：当前帧延迟超过 warning_latency_ms
        if latency_ms > self._settings.warning_latency_ms:
            return DataHealthStatus.DEGRADED

        return DataHealthStatus.OK

    def _handle_status_change(
        self, new_status: DataHealthStatus, report: DataHealthReport
    ) -> None:
        """状态变化时记录日志并触发回调。"""
        if new_status == DataHealthStatus.INTERRUPTED:
            logger.error(
                f"VIL data INTERRUPTED: frame_age={report.last_frame_age_ms:.0f}ms. "
                "Simulation will pause."
            )
            if self._on_interrupted:
                self._on_interrupted(report)
        elif new_status == DataHealthStatus.DEGRADED:
            logger.warning(
                f"VIL data DEGRADED: latency={report.current_latency_ms:.1f}ms. "
                "Using pose extrapolation."
            )
            if self._on_degraded:
                self._on_degraded(report)
        elif new_status == DataHealthStatus.OK:
            logger.info("VIL data recovered to OK status")
            if self._on_recovered:
                self._on_recovered(report)
