"""VIL 数据健康监控单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import time

import pytest

from hunter_sim.common.models import Transform, VehicleState, VILSettings
from hunter_sim.vil_mapper.data_health_monitor import (
    DataHealthMonitor,
    DataHealthStatus,
)
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame


def _make_frame(latency_ms: float) -> TelemetryFrame:
    """构造一帧指定端到端延迟的遥测数据。"""
    now = time.time()
    return TelemetryFrame(
        vehicle_id="hunter-se-001",
        timestamp=now - latency_ms / 1000.0,
        receive_time=now,
        vehicle_state=VehicleState(
            time_stamp=now,
            transform=Transform(x=1.0, y=2.0, yaw=0.1),
            vehicle_speed=3.0,
        ),
    )


@pytest.fixture()
def settings() -> VILSettings:
    return VILSettings(max_data_latency_ms=500, warning_latency_ms=50)


class TestDataHealthMonitor:
    def test_empty_buffer_is_interrupted(self, settings: VILSettings) -> None:
        buffer = TelemetryBuffer()
        monitor = DataHealthMonitor(settings, buffer)
        report = monitor.check()
        assert report.status == DataHealthStatus.INTERRUPTED
        assert report.last_frame_age_ms == 99999.0

    def test_low_latency_is_ok(self, settings: VILSettings) -> None:
        buffer = TelemetryBuffer(max_latency_ms=500)
        monitor = DataHealthMonitor(settings, buffer)
        buffer.push(_make_frame(latency_ms=10.0))
        report = monitor.check()
        assert report.status == DataHealthStatus.OK
        assert report.current_latency_ms == pytest.approx(10.0, abs=2.0)

    def test_mid_latency_is_degraded(self, settings: VILSettings) -> None:
        buffer = TelemetryBuffer(max_latency_ms=500)
        monitor = DataHealthMonitor(settings, buffer)
        buffer.push(_make_frame(latency_ms=120.0))
        report = monitor.check()
        assert report.status == DataHealthStatus.DEGRADED

    def test_status_change_triggers_callbacks(self, settings: VILSettings) -> None:
        buffer = TelemetryBuffer(max_latency_ms=500)
        degraded_calls: list = []
        recovered_calls: list = []
        monitor = DataHealthMonitor(
            settings,
            buffer,
            on_degraded=lambda r: degraded_calls.append(r),
            on_recovered=lambda r: recovered_calls.append(r),
        )
        # 初始 OK -> push 高延迟 -> DEGRADED（触发 on_degraded）
        buffer.push(_make_frame(latency_ms=200.0))
        monitor.check()
        assert monitor.current_status == DataHealthStatus.DEGRADED
        assert len(degraded_calls) == 1

        # 恢复低延迟 -> OK（触发 on_recovered）
        buffer.push(_make_frame(latency_ms=5.0))
        monitor.check()
        assert monitor.current_status == DataHealthStatus.OK
        assert len(recovered_calls) == 1

    def test_no_repeat_callback_when_stable(self, settings: VILSettings) -> None:
        buffer = TelemetryBuffer(max_latency_ms=500)
        degraded_calls: list = []
        monitor = DataHealthMonitor(
            settings, buffer, on_degraded=lambda r: degraded_calls.append(r)
        )
        for _ in range(3):
            buffer.push(_make_frame(latency_ms=150.0))
            monitor.check()
        # 状态一直为 DEGRADED，仅首次变化触发一次
        assert len(degraded_calls) == 1

    def test_interrupted_callback(self, settings: VILSettings) -> None:
        buffer = TelemetryBuffer(max_latency_ms=500)
        interrupted_calls: list = []
        monitor = DataHealthMonitor(
            settings, buffer, on_interrupted=lambda r: interrupted_calls.append(r)
        )
        # 先正常
        buffer.push(_make_frame(latency_ms=10.0))
        monitor.check()
        # 清空后无新帧 -> 帧龄超限 -> INTERRUPTED
        buffer.clear()
        monitor._last_frame_time = time.time() - 10.0  # 回拨内部时间，避免 sleep
        report = monitor.check()
        assert report.status == DataHealthStatus.INTERRUPTED
        assert len(interrupted_calls) == 1

    def test_fps_estimated_after_multiple_frames(self, settings: VILSettings) -> None:
        buffer = TelemetryBuffer(max_latency_ms=500)
        monitor = DataHealthMonitor(settings, buffer)
        monitor._frame_count_window = [time.time() - 1.0, time.time()]  # 2 帧跨 1s
        buffer.push(_make_frame(latency_ms=10.0))
        report = monitor.check()
        assert report.fps > 0.0
