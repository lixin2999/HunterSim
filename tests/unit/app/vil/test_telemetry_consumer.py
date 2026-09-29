"""VIL Kafka 遥测消费者单元测试（对应 §4.2）。

Kafka 客户端通过 monkeypatch 完全 mock，不依赖真实 broker。
测试模式：``asyncio_mode=auto``（见 pyproject.toml），异步测试函数自动识别。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from typing import Any

import pytest

from hunter_sim.app.vil.models import VehicleTelemetry, VILCalibration, VILConfig
from hunter_sim.app.vil.telemetry_consumer import KafkaTelemetryConsumerImpl
from hunter_sim.core.exceptions import KafkaConnectionError


def _make_config(target: str = "hunter-001") -> VILConfig:
    return VILConfig(
        target_vehicle_id=target,
        calibration=VILCalibration(x0=0.0, y0=0.0, yaw0=0.0),
    )


def _payload(vehicle_id: str = "hunter-001", ts: float | None = None) -> dict[str, Any]:
    return {
        "vehicle_id": vehicle_id,
        "timestamp": ts if ts is not None else time.time(),
        "localization": {"x": 1.0, "y": 2.0, "heading": 0.1},
        "chassis": {"velocity": 5.0, "steering": 0.0, "throttle": 0.3, "brake": 0.0},
        "perception": {"objects": [], "planning_trajectory": []},
        "behavior_state": "cruise",
    }


async def test_poll_latest_returns_none_before_data() -> None:
    consumer = KafkaTelemetryConsumerImpl(_make_config())
    assert consumer._latest is None
    assert await consumer.poll_latest() is None


async def test_handle_message_accepts_matching_vehicle_id() -> None:
    consumer = KafkaTelemetryConsumerImpl(_make_config())
    msg = types.SimpleNamespace(value=json.dumps(_payload()).encode("utf-8"))
    consumer._handle_message(msg)
    latest = await consumer.poll_latest()
    assert latest is not None
    assert latest.vehicle_id == "hunter-001"
    assert latest.localization.x == pytest.approx(1.0)


def test_handle_message_filters_other_vehicle() -> None:
    consumer = KafkaTelemetryConsumerImpl(_make_config("hunter-001"))
    msg = types.SimpleNamespace(value=json.dumps(_payload(vehicle_id="hunter-999")).encode())
    consumer._handle_message(msg)
    assert consumer._latest is None


def test_handle_message_ignores_invalid_json() -> None:
    consumer = KafkaTelemetryConsumerImpl(_make_config())
    consumer._handle_message(types.SimpleNamespace(value=b"not-json"))
    assert consumer._latest is None


def test_handle_message_ignores_schema_violation() -> None:
    consumer = KafkaTelemetryConsumerImpl(_make_config())
    bad = _payload()
    del bad["localization"]
    consumer._handle_message(types.SimpleNamespace(value=json.dumps(bad).encode()))
    assert consumer._latest is None


async def test_start_raises_when_kafka_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "kafka", None)  # type: ignore[index]
    consumer = KafkaTelemetryConsumerImpl(_make_config())
    with pytest.raises(KafkaConnectionError):
        await consumer.start()


class _FakeConsumer:
    """行为可控的 Kafka 代用：首次 poll 返回预置 payload，之后空转；close 标记。"""

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        self.closed = False
        self._sent = False

    def poll(self, timeout_ms: int = 0) -> dict[str, list[Any]]:
        if self.closed:
            return {}
        if self._sent:
            time.sleep(0.005)
            return {}
        self._sent = True
        payload = json.dumps(_payload()).encode("utf-8")
        return {"telemetry_clean": [types.SimpleNamespace(value=payload)]}

    def close(self) -> None:
        self.closed = True


def _install_fake_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("kafka")
    module.KafkaConsumer = _FakeConsumer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kafka", module)


async def test_start_and_stop_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_kafka(monkeypatch)
    consumer = KafkaTelemetryConsumerImpl(_make_config())
    await consumer.start()
    assert consumer._thread is not None and consumer._thread.is_alive()
    inner = consumer._consumer
    assert inner is not None
    await consumer.stop()
    assert consumer._thread is None
    assert inner.closed is True
    # 幂等
    await consumer.stop()


async def test_background_thread_updates_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_kafka(monkeypatch)
    consumer = KafkaTelemetryConsumerImpl(_make_config())
    await consumer.start()
    latest: VehicleTelemetry | None = None
    for _ in range(100):
        latest = await consumer.poll_latest()
        if latest is not None:
            break
        await asyncio.sleep(0.01)
    await consumer.stop()
    assert latest is not None
    assert latest.localization.x == pytest.approx(1.0)
