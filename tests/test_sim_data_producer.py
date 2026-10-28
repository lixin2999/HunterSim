"""SimDataProducer 单测（P1/P6：各发布路径 + 缓冲降级 + DLQ，无 Kafka 环境）。

覆盖：
- connect() 未启用时降级 no-op；
- producer_factory 注入桩；
- publish_status / publish_telemetry / publish_event / publish_metrics /
  publish_sensor_file / publish_artifact 正常发送；
- broker 不可达时消息进缓冲；
- flush() 尽力重投缓冲；
- DLQ topic 渲染；
- next_seq() 单调递增；
- sensor_data_topic 属性。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from hunter_sim.cloud.sim_data_producer import SimDataProducer
from hunter_sim.common.models import CloudSettings


# ─── 桩工厂 ────────────────────────────────────────────────────────────────────


class FakeKafkaProducer:
    """记录 send 调用并可配置抛出异常。"""

    def __init__(self, fail_on_send: bool = False) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []
        self._fail = fail_on_send
        self.closed = False

    def send(self, topic: str, key: bytes = None, value: bytes = None) -> Any:  # noqa: ANN001, D102
        if self._fail:
            raise RuntimeError("broker unreachable")
        self.sent.append((topic, key, value))
        # 返回 FutureRecordMetadata-like
        return _FakeFuture()

    def flush(self, timeout: float = 3.0) -> None:  # noqa: D102
        return None

    def close(self, timeout: float = None) -> None:  # noqa: D102
        self.closed = True


class _FakeFuture:
    def get(self, timeout: float = None) -> None:  # noqa: ANN001, D102
        return None


def _settings(**overrides: Any) -> CloudSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "kafka_bootstrap": "fake:9092",
        "sensor_data_topic_template": "carla.{sim_id}.sensor_data",
        "sensor_data_dlq_template": "carla.{sim_id}.sensor_data.dlq",
    }
    base.update(overrides)
    return CloudSettings(**base)  # type: ignore[arg-type]


def _producer(
    sim_id: str = "sim-test-001",
    fail: bool = False,
    **setting_overrides: Any,
) -> tuple[SimDataProducer, FakeKafkaProducer]:
    fake = FakeKafkaProducer(fail_on_send=fail)
    settings = _settings(**setting_overrides)

    def factory(s: CloudSettings, sid: str) -> FakeKafkaProducer:
        return fake

    p = SimDataProducer(settings, sim_id=sim_id, producer_factory=factory)
    p.connect()
    return p, fake


# ─── 生命周期 ──────────────────────────────────────────────────────────────────


class TestConnect:
    def test_disabled_noop(self) -> None:
        settings = _settings(enabled=False)

        def factory(s: CloudSettings, sid: str) -> Any:
            raise AssertionError("factory should not be called")

        p = SimDataProducer(settings, sim_id="sim-x", producer_factory=factory)
        ok = p.connect()
        assert ok is False
        assert p.available is False

    def test_connect_success(self) -> None:
        p, fake = _producer()
        assert p.available is True

    def test_close_sets_unavailable(self) -> None:
        p, fake = _producer()
        p.close()
        assert p.available is False
        assert fake.closed is True


# ─── Topic 属性 ────────────────────────────────────────────────────────────────


class TestTopicProperties:
    def test_sensor_data_topic(self) -> None:
        p, _ = _producer(sim_id="sim-abc")
        assert p.sensor_data_topic == "carla.sim-abc.sensor_data"

    def test_dlq_topic(self) -> None:
        p, _ = _producer(sim_id="sim-abc")
        assert p.dlq_topic == "carla.sim-abc.sensor_data.dlq"


# ─── next_seq ──────────────────────────────────────────────────────────────────


class TestSeq:
    def test_monotonic(self) -> None:
        p, _ = _producer()
        seqs = [p.next_seq() for _ in range(5)]
        assert seqs == [1, 2, 3, 4, 5]

    def test_thread_safe(self) -> None:
        p, _ = _producer()
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(100):
                val = p.next_seq()
                with lock:
                    results.append(val)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 400
        assert len(set(results)) == 400  # 无重复


# ─── 发布路径 ──────────────────────────────────────────────────────────────────


class TestPublishStatus:
    def test_sends_correct_topic(self) -> None:
        p, fake = _producer(sim_id="sim-status")
        msg = {"type": "sim_status", "payload": {"status": "RUNNING"}}
        ok = p.publish_status(msg)
        assert ok is True
        assert len(fake.sent) == 1
        topic, key, value = fake.sent[0]
        assert topic == "carla.sim-status.sensor_data"
        assert key == b"sim-status"
        assert json.loads(value)["type"] == "sim_status"


class TestPublishTelemetry:
    def test_sends_message(self) -> None:
        p, fake = _producer(sim_id="sim-tel")
        msg = {"type": "sim_telemetry", "payload": {"pose": {"x": 1.0}}}
        ok = p.publish_telemetry(msg)
        assert ok is True
        assert fake.sent[0][0] == "carla.sim-tel.sensor_data"


class TestPublishEvent:
    def test_builds_and_sends(self) -> None:
        p, fake = _producer(sim_id="sim-ev")
        ok = p.publish_event(
            vehicle_id="HUNTER-001",
            scene_id="scene-1",
            event_type="collision",
            event_level="critical",
            sim_time=3.5,
            description="Hit barrier",
            data={"speed_at_impact": 10.0},
        )
        assert ok is True
        topic, _, value = fake.sent[0]
        parsed = json.loads(value)
        assert parsed["type"] == "sim_event"
        assert parsed["payload"]["event_type"] == "collision"
        assert parsed["payload"]["event_level"] == "critical"


class TestPublishMetrics:
    def test_sends(self) -> None:
        p, fake = _producer(sim_id="sim-met")
        msg = {"type": "sim_metrics", "payload": {"grade": "A"}}
        ok = p.publish_metrics(msg)
        assert ok is True
        assert fake.sent[0][0] == "carla.sim-met.sensor_data"


class TestPublishSensorFile:
    def test_builds_and_sends(self) -> None:
        p, fake = _producer(sim_id="sim-bag")
        ok = p.publish_sensor_file(
            bucket="hunter-rosbag",
            object_key="SIM-sim-bag/rosbag_001.mbag",
            size_bytes=512,
            sha256="abcdef",
            sensor_type="rosbag",
            start_time=0.0,
            end_time=30.0,
            frame_count=100,
        )
        assert ok is True
        _, _, value = fake.sent[0]
        parsed = json.loads(value)
        assert parsed["type"] == "sensor_file"
        assert parsed["payload"]["bucket"] == "hunter-rosbag"


class TestPublishArtifact:
    def test_builds_and_sends(self) -> None:
        p, fake = _producer(sim_id="sim-art")
        ok = p.publish_artifact(
            name="eval.json",
            bucket="hunter-scene-assets",
            object_key="scenarios/simulations/sim-art/eval.json",
            size_bytes=1024,
            sha256="beef1234",
            media_type="application/json",
        )
        assert ok is True
        _, _, value = fake.sent[0]
        parsed = json.loads(value)
        assert parsed["type"] == "artifact"
        assert parsed["payload"]["name"] == "eval.json"


# ─── 缓冲降级 ──────────────────────────────────────────────────────────────────


class TestBufferFallback:
    def test_send_failure_buffers(self) -> None:
        p, fake = _producer(sim_id="sim-buf", fail=True)
        msg = {"type": "sim_status", "payload": {}}
        ok = p.publish_status(msg)
        # 发送失败，缓冲，返回 False（未达 broker）
        assert ok is False
        assert p.buffered_count >= 1

    def test_flush_retries_buffer(self) -> None:
        # 先制造缓冲，再切换到成功路径
        fake_fail = FakeKafkaProducer(fail_on_send=True)
        fake_ok = FakeKafkaProducer(fail_on_send=False)
        settings = _settings()

        def factory(s: CloudSettings, sid: str) -> FakeKafkaProducer:
            return fake_fail

        p = SimDataProducer(settings, sim_id="sim-flush", producer_factory=factory)
        p.connect()
        # 发送失败进缓冲
        p.publish_status({"type": "sim_status", "payload": {}})
        assert p.buffered_count >= 1
        # 手动替换内部 producer 为成功桩
        p._producer = fake_ok
        p._available = True
        p.flush()
        # flush 后缓冲应清空
        assert p.buffered_count == 0


# ─── 未启用时 no-op ─────────────────────────────────────────────────────────────


class TestDisabledNoop:
    def test_publish_disabled(self) -> None:
        settings = _settings(enabled=False)

        def factory(s: CloudSettings, sid: str) -> Any:
            raise AssertionError("should not be called")

        p = SimDataProducer(settings, sim_id="sim-off", producer_factory=factory)
        p.connect()
        # 所有发布均返回 False（未发往 broker）但不抛异常
        assert p.publish_status({"type": "sim_status", "payload": {}}) is False
        assert p.publish_telemetry({"type": "sim_telemetry", "payload": {}}) is False


# ─── publish_status_from_payload ────────────────────────────────────────────────


class TestPublishStatusFromPayload:
    def test_convenience(self) -> None:
        from hunter_sim.cloud.sim_data_mapper import SimStatusPayload

        p, fake = _producer(sim_id="sim-cvp")
        payload = SimStatusPayload(
            status="COMPLETED",
            progress_percent=100.0,
            elapsed_time=60.0,
            total_time=60.0,
            actors_count=2,
            collisions=0,
            lane_invasions=0,
            ego_state={"x": 0.0, "y": 0.0, "heading": 0.0, "velocity": 0.0, "steering": 0.0},
        )
        ok = p.publish_status_from_payload("scene-1", payload)
        assert ok is True
        _, _, value = fake.sent[0]
        parsed = json.loads(value)
        assert parsed["type"] == "sim_status"
        assert parsed["payload"]["status"] == "COMPLETED"


# ─── V2.12.0：缓冲溢出可观测 + 重投异步失败不静默 ──────────────────────────


class _ErrbackFuture:
    """可记录 errback 注册的 Future 桩。"""

    def __init__(self) -> None:
        self.errbacks: list[Any] = []

    def add_errback(self, fn: Any) -> None:
        self.errbacks.append(fn)


class _ErrbackProducer(FakeKafkaProducer):
    """send 返回可记录 errback 的 Future。"""

    def __init__(self) -> None:
        super().__init__()
        self.futures: list[_ErrbackFuture] = []

    def send(self, topic: str, key: bytes = None, value: bytes = None) -> Any:  # noqa: ANN001
        self.sent.append((topic, key, value))
        future = _ErrbackFuture()
        self.futures.append(future)
        return future


class TestV212Observability:
    def test_buffer_overflow_increments_dropped_metric(self) -> None:
        """满缓冲丢最旧时必须计数（不得静默丢数据）。"""
        import hunter_sim.cloud.sim_data_producer as mod
        from hunter_sim.common.metrics import SIM_DATA_DROPPED_TOTAL

        fake = FakeKafkaProducer(fail_on_send=True)
        p = SimDataProducer(
            _settings(), sim_id="sim-drop", producer_factory=lambda s, sid: fake
        )
        p.connect()
        before = SIM_DATA_DROPPED_TOTAL.labels(
            sim_id="sim-drop", reason="buffer_overflow"
        )._value.get()

        # 直接塞满（避免构造 2000 次网络失败日志）
        for _ in range(mod._MAX_BUFFER + 3):
            p._buffer_message("t", b"k", b"v")

        after = SIM_DATA_DROPPED_TOTAL.labels(
            sim_id="sim-drop", reason="buffer_overflow"
        )._value.get()
        assert after - before == 3
        assert p.buffered_count == mod._MAX_BUFFER

    def test_flush_registers_errback(self) -> None:
        """flush 重投的异步失败必须注册 errback（否则静默丢失）。"""
        fake = _ErrbackProducer()
        p = SimDataProducer(
            _settings(), sim_id="sim-eb", producer_factory=lambda s, sid: fake
        )
        p.connect()
        p._buffer_message("t", b"k", b"v")

        assert p.flush() == 1
        assert len(fake.futures) == 1
        assert len(fake.futures[0].errbacks) == 1

        # errback 被触发时消息应重入缓冲
        fake.futures[0].errbacks[0](RuntimeError("async boom"))
        assert p.buffered_count == 1

    def test_send_registers_errback(self) -> None:
        fake = _ErrbackProducer()
        p = SimDataProducer(
            _settings(), sim_id="sim-eb2", producer_factory=lambda s, sid: fake
        )
        p.connect()
        assert p.publish_status({"type": "sim_status", "payload": {}}) is True
        assert len(fake.futures[0].errbacks) == 1

    def test_sensor_file_forwards_optional_fields(self) -> None:
        p, fake = _producer(sim_id="sim-sf")
        p.publish_sensor_file(
            bucket="b", object_key="k", size_bytes=1, sha256="s",
            md5="m", file_format="rosbag2", topics=["/tf"],
        )
        _, _, value = fake.sent[0]
        payload = json.loads(value)["payload"]
        assert payload["md5"] == "m"
        assert payload["format"] == "rosbag2"
        assert payload["topics"] == ["/tf"]
