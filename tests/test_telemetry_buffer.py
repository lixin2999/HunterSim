"""VIL 遥测缓冲区单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import time

from hunter_sim.common.models import Transform, VehicleState
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame


def _state() -> VehicleState:
    return VehicleState(
        time_stamp=0.0,
        transform=Transform(x=0.0, y=0.0, z=0.0, pitch=0.0, yaw=0.0, roll=0.0),
        velocity=(0.0, 0.0, 0.0),
        acceleration=(0.0, 0.0, 0.0),
        angular_velocity=(0.0, 0.0, 0.0),
        steering=0.0,
        throttle=0.0,
        brake=0.0,
        gear=1,
        vehicle_speed=0.0,
    )


def _frame(latency_s: float = 0.0) -> TelemetryFrame:
    now = time.time()
    return TelemetryFrame(
        vehicle_id="hunter-01",
        timestamp=now - latency_s,
        receive_time=now,
        vehicle_state=_state(),
    )


class TestTelemetryFrame:
    def test_latency_ms(self) -> None:
        f = TelemetryFrame(
            vehicle_id="v", timestamp=100.0, receive_time=100.25, vehicle_state=_state()
        )
        assert f.latency_ms == 250.0


class TestTelemetryBuffer:
    def test_empty(self) -> None:
        buf = TelemetryBuffer()
        assert buf.is_empty is True
        assert buf.get_latest() is None
        assert buf.get_latest_fresh() is None
        assert buf.expiry_rate == 0.0

    def test_push_fresh(self) -> None:
        buf = TelemetryBuffer(max_frames=5, max_latency_ms=500)
        buf.push(_frame(latency_s=0.05))  # 50ms 新鲜
        assert buf.total_received == 1
        assert buf.total_expired == 0
        assert buf.get_latest() is not None
        assert buf.get_latest_fresh() is not None

    def test_expired_frame_marked(self) -> None:
        buf = TelemetryBuffer(max_frames=5, max_latency_ms=100)
        f = _frame(latency_s=0.5)  # 500ms 过期
        buf.push(f)
        assert buf.total_expired == 1
        latest = buf.get_latest()
        assert latest is not None
        assert latest.is_fresh is False
        # 无新鲜帧
        assert buf.get_latest_fresh() is None

    def test_capacity_eviction(self) -> None:
        buf = TelemetryBuffer(max_frames=3, max_latency_ms=1000)
        for _ in range(5):
            buf.push(_frame(latency_s=0.01))
        assert len(buf.get_last_n(10)) == 3
        assert buf.total_received == 5

    def test_clear(self) -> None:
        buf = TelemetryBuffer(max_frames=3)
        buf.push(_frame())
        buf.clear()
        assert buf.is_empty is True
        assert buf.total_received == 0

    def test_expiry_rate(self) -> None:
        buf = TelemetryBuffer(max_frames=10, max_latency_ms=100)
        buf.push(_frame(latency_s=0.01))  # fresh
        buf.push(_frame(latency_s=0.5))   # expired
        assert buf.expiry_rate == 0.5
