"""TelemetryProducer 单测（V2.2.0，注入 fake producer，无真实 kafka）。

校验 topic 名解析（{vehicle_id} 模板）、key=vehicle_id、JSON 序列化、异常入缓冲、
flush 重投、未启用时 no-op。
"""

from __future__ import annotations

import json

from hunter_sim.cloud.telemetry_producer import TelemetryProducer
from hunter_sim.common.models import CloudSettings, Transform, VehicleState


class FakeProducer:
    """记录 send 调用的桩生产者。"""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []
        self._fail = fail

    def send(self, topic: str, key: bytes = None, value: bytes = None) -> None:  # noqa: ANN001, D102
        if self._fail:
            raise RuntimeError("broker down")
        self.sent.append((topic, key, value))

    def flush(self, timeout: float = 3.0) -> None:  # noqa: ANN001, D102
        return None

    def close(self) -> None:  # noqa: D102
        return None


def _settings(**overrides: object) -> CloudSettings:
    base = {"enabled": True, "vehicle_id": "SIM_001"}
    base.update(overrides)
    return CloudSettings(**base)  # type: ignore[arg-type]


def _state() -> VehicleState:
    return VehicleState(
        time_stamp=1700000000.0,
        transform=Transform(x=1.0, y=2.0, z=0.0, pitch=0.0, yaw=0.0, roll=0.0),
        vehicle_speed=2.0,
    )


def test_topic_template_and_key() -> None:
    """topic 替换 {vehicle_id}，key 为 vehicle_id 编码，value 为 JSON。"""
    fake = FakeProducer()
    producer = TelemetryProducer(_settings(), producer_factory=lambda s: fake)
    assert producer.connect() is True
    ok = producer.publish_telemetry(_state(), seq=5)
    assert ok is True
    topic, key, value = fake.sent[0]
    assert topic == "hunter.SIM_001.telemetry"
    assert key == b"SIM_001"
    payload = json.loads(value)
    assert payload["vehicle_id"] == "SIM_001"
    assert payload["seq"] == 5


def test_event_and_health_topics() -> None:
    """event/health 走各自 topic。"""
    fake = FakeProducer()
    producer = TelemetryProducer(_settings(), producer_factory=lambda s: fake)
    producer.connect()
    producer.publish_event(1700000000.0, "harsh_braking")
    producer.publish_health(1700000000.0)
    topics = [t for t, _, _ in fake.sent]
    assert topics == ["hunter.SIM_001.event", "hunter.SIM_001.health"]


def test_send_failure_buffers() -> None:
    """send 抛异常时消息进缓冲、返回 False。"""
    fake = FakeProducer(fail=True)
    producer = TelemetryProducer(_settings(), producer_factory=lambda s: fake)
    producer.connect()
    assert producer.publish_telemetry(_state(), seq=1) is False
    assert producer.buffered_count == 1


def test_flush_reattempts_buffered() -> None:
    """恢复可用后 flush 重投缓冲消息。"""
    fake = FakeProducer(fail=True)
    producer = TelemetryProducer(_settings(), producer_factory=lambda s: fake)
    producer.connect()
    producer.publish_telemetry(_state(), seq=1)  # 入缓冲
    assert producer.buffered_count == 1
    fake._fail = False  # 恢复
    resent = producer.flush()
    assert resent == 1
    assert producer.buffered_count == 0
    assert len(fake.sent) == 1


def test_disabled_is_noop() -> None:
    """未启用时 connect 返回 False，发布不抛。"""
    producer = TelemetryProducer(_settings(enabled=False), producer_factory=lambda s: FakeProducer())
    assert producer.connect() is False
    assert producer.available is False
    assert producer.publish_telemetry(_state(), seq=1) is False


class _FakeFuture:
    """模拟 kafka-python FutureRecordMetadata，收集 errback。"""

    def __init__(self) -> None:
        self.errbacks: list = []

    def add_errback(self, callback) -> None:  # noqa: ANN001
        self.errbacks.append(callback)


class AsyncFailProducer:
    """send 返回 Future（不同步抛异常），用于验证异步失败回捞。"""

    def __init__(self) -> None:
        self.futures: list = []

    def send(self, topic: str, key: bytes = None, value: bytes = None):  # noqa: ANN001, ANN201, D102
        fut = _FakeFuture()
        self.futures.append(fut)
        return fut

    def flush(self, timeout: float = 3.0) -> None:  # noqa: ANN001, D102
        return None

    def close(self) -> None:  # noqa: D102
        return None


def test_async_send_error_rebuffers() -> None:
    """send 同步不报错但异步投递失败时，errback 应将消息重入缓冲（C-3）。"""
    fake = AsyncFailProducer()
    producer = TelemetryProducer(_settings(), producer_factory=lambda s: fake)
    producer.connect()
    assert producer.publish_telemetry(_state(), seq=1) is True
    assert producer.buffered_count == 0
    # 模拟 broker 不可达：触发已注册的 errback
    future = fake.futures[0]
    assert future.errbacks, "应为发送 Future 注册 errback"
    future.errbacks[0](RuntimeError("broker down"))
    assert producer.buffered_count == 1
