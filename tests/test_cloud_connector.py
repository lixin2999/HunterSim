"""CloudConnector 聚合入口单测（V2.2.0，注入 fake producer/client）。

校验未启用 no-op、启用后 start 聚合两条链路、publish_state 按 telemetry_interval_s
节流、upload_bag 透传。不依赖真实 Kafka/httpx。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hunter_sim.cloud.connector import CloudConnector
from hunter_sim.common.models import CloudSettings, Transform, VehicleState


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []

    def send(self, topic: str, key: bytes = None, value: bytes = None) -> None:  # noqa: ANN001, D102
        self.sent.append((topic, key, value))

    def flush(self, timeout: float = 3.0) -> None:  # noqa: ANN001, D102
        return None

    def close(self) -> None:  # noqa: D102
        return None


def _settings(**overrides: object) -> CloudSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "vehicle_id": "SIM_001",
        "jwt_token": "t0k",
        "telemetry_interval_s": 0.1,
    }
    base.update(overrides)
    return CloudSettings(**base)  # type: ignore[arg-type]


def _state() -> VehicleState:
    return VehicleState(
        time_stamp=1700000000.0,
        transform=Transform(x=0.0, y=0.0, z=0.0, pitch=0.0, yaw=0.0, roll=0.0),
        vehicle_speed=1.0,
    )


def test_disabled_noop() -> None:
    """未启用时 start 返回 False，发布/上传安全返回。"""
    conn = CloudConnector(_settings(enabled=False))
    assert conn.enabled is False
    assert conn.start() is False
    assert conn.publish_state(_state()) is False
    assert conn.upload_bag(Path("x.mcap")) is None


def test_start_and_publish() -> None:
    """启用后 start 聚合链路，publish_state/event/health 落到 fake producer。"""
    fake = FakeProducer()
    conn = CloudConnector(_settings(), producer_factory=lambda s: fake)
    assert conn.start() is True
    assert conn.started is True
    assert conn.publish_state(_state()) is True
    assert conn.publish_event("harsh_braking") is True
    assert conn.publish_health() is True
    topics = [t for t, _, _ in fake.sent]
    assert topics == ["hunter.SIM_001.telemetry", "hunter.SIM_001.event", "hunter.SIM_001.health"]
    conn.stop()
    assert conn.started is False


def test_publish_state_throttles() -> None:
    """telemetry_interval_s 上限时第二次发布被节流（返回 True 但不落 broker）。"""
    fake = FakeProducer()
    conn = CloudConnector(_settings(telemetry_interval_s=1.0), producer_factory=lambda s: fake)
    conn.start()
    first = conn.publish_state(_state())
    second = conn.publish_state(_state())
    assert first is True
    assert second is True  # 节流视为"已按节拍处理"但未发送
    assert len(fake.sent) == 1
