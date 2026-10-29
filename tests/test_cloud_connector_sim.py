"""CloudConnector 新路径端到端测试（V2.10.0：桩 producer）。

聚焦 6 类消息通过 CloudConnector 的集成发布：
- sim_status / sim_telemetry / sim_event / sim_metrics / sensor_file / artifact
- upload_bag 完成后自动触发 sensor_file 发布
- 未启用/未启动时各方法安全 no-op

与 test_cloud_connector.py 互补：本文件更关注完整消息流与 artifact 发布路径。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from hunter_sim.cloud.connector import CloudConnector
from hunter_sim.cloud.sim_data_producer import SimDataProducer
from hunter_sim.common.models import CloudSettings


# ── 桩基础设施 ──────────────────────────────────────────────────────────────────


class _RecordingProducer:
    """Kafka Producer 桩：记录所有 send 调用（topic + value 解析为 dict）。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def send(self, topic: str, key: bytes = None, value: bytes = None) -> Any:  # noqa: ANN001
        parsed = json.loads(value) if value else {}
        self.records.append({"topic": topic, "key": key, "value": parsed})
        return _Future()

    def flush(self, timeout: float = 3.0) -> None: ...
    def close(self, timeout: float = None) -> None: ...  # noqa: ANN001

    def messages_of_type(self, msg_type: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["value"].get("type") == msg_type]


class _Future:
    def get(self, timeout: float = None) -> None: ...  # noqa: ANN001


def _settings(**overrides: object) -> CloudSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "sim_id": "sim-e2e-001",
        "sensor_data_topic_template": "carla.{sim_id}.sensor_data",
        "sensor_data_dlq_template": "carla.{sim_id}.sensor_data.dlq",
        "telemetry_proxy_interval_s": 0.0,  # 禁用节流方便测试
    }
    base.update(overrides)
    return CloudSettings(**base)  # type: ignore[arg-type]


def _factory(recorder: _RecordingProducer) -> Any:
    def factory(settings: CloudSettings, sim_id: str) -> _RecordingProducer:
        return recorder

    return factory


# ── 测试：publish_artifact ─────────────────────────────────────────────────────


def test_publish_artifact_end_to_end() -> None:
    """publish_artifact 通过 SimDataProducer 发出 artifact 类型消息。"""
    rec = _RecordingProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(rec))
    conn.start("sim-e2e-001")

    ok = conn.publish_artifact(
        name="eval.json",
        bucket="hunter-scene-assets",
        object_key="scenarios/simulations/sim-e2e-001/eval.json",
        size_bytes=2048,
        sha256="abc123",
        media_type="application/json",
        scene_id="scene-789",
    )
    assert ok is True
    artifacts = rec.messages_of_type("artifact")
    assert len(artifacts) == 1
    payload = artifacts[0]["value"]["payload"]
    assert payload["name"] == "eval.json"
    assert payload["bucket"] == "hunter-scene-assets"
    assert payload["sha256"] == "abc123"
    conn.stop()


def test_publish_artifact_when_not_started() -> None:
    """未启动时 publish_artifact 安全返回 False。"""
    conn = CloudConnector(_settings())
    assert conn.publish_artifact(
        name="x", bucket="b", object_key="k",
        size_bytes=0, sha256="", media_type="text/plain"
    ) is False


# ── 测试：upload_bag → sensor_file 自动发布 ─────────────────────────────────────


def test_upload_bag_auto_publishes_sensor_file() -> None:
    """upload_bag 成功上传后自动发布 sensor_file 类型消息。"""
    rec = _RecordingProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(rec))
    conn.start("sim-e2e-001")

    class _FakeResult:
        bucket = "sensor-bucket"
        object_key = "bags/sim-e2e-001/run1.mcap"
        size_bytes = 5120
        sha256 = "deadbeef"

    conn.uploader.upload = lambda path, data_type="rosbag": _FakeResult()  # type: ignore[method-assign]
    conn._perform_upload(Path("run1.mcap"), "mcap")

    sensor_files = rec.messages_of_type("sensor_file")
    assert len(sensor_files) == 1
    sf_payload = sensor_files[0]["value"]["payload"]
    assert sf_payload["bucket"] == "sensor-bucket"
    assert sf_payload["object_key"] == "bags/sim-e2e-001/run1.mcap"
    conn.stop()


def test_upload_failure_no_sensor_file_publish() -> None:
    """上传失败时不发布 sensor_file。"""
    rec = _RecordingProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(rec))
    conn.start("sim-e2e-001")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("MinIO unreachable")

    conn.uploader.upload = _boom  # type: ignore[method-assign]
    conn._perform_upload(Path("bad.mcap"), "mcap")

    sensor_files = rec.messages_of_type("sensor_file")
    assert len(sensor_files) == 0
    assert conn.upload_stats["failed"] == 1
    conn.stop()


# ── 测试：6 类消息全部走通 ──────────────────────────────────────────────────────


def test_all_six_message_types() -> None:
    """一次连接中依次发出 6 种 type 消息，全部落到正确 topic。"""
    rec = _RecordingProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(rec))
    conn.start("sim-e2e-001")
    topic = "carla.sim-e2e-001.sensor_data"

    # 1. sim_status
    conn.publish_status({
        "schema_version": "1.0.0", "sim_id": "sim-e2e-001",
        "type": "sim_status", "timestamp": time.time(), "seq": 1, "payload": {"status": "RUNNING"},
    })
    # 2. sim_telemetry
    conn.publish_telemetry({
        "schema_version": "1.0.0", "sim_id": "sim-e2e-001",
        "type": "sim_telemetry", "timestamp": time.time(), "seq": 2,
        "payload": {"pose": {"x": 1.0}},
    })
    # 3. sim_event
    conn.publish_event("collision", "critical", sim_time=2.5, description="hit wall")
    # 4. sim_metrics
    conn.publish_metrics({
        "schema_version": "1.0.0", "sim_id": "sim-e2e-001",
        "type": "sim_metrics", "timestamp": time.time(), "seq": 4, "payload": {"score": 85},
    })
    # 5. sensor_file (via _perform_upload)

    class _UploadResult:
        bucket = "b"
        object_key = "k"
        size_bytes = 100
        sha256 = "h"

    conn.uploader.upload = lambda path, data_type="rosbag": _UploadResult()  # type: ignore[method-assign]
    conn._perform_upload(Path("test.mcap"), "mcap")
    # 6. artifact
    conn.publish_artifact(
        name="log.txt", bucket="b", object_key="logs/sim-e2e-001.log",
        size_bytes=50, sha256="x", media_type="text/plain"
    )

    types_seen = {r["value"].get("type") for r in rec.records}
    assert "sim_status" in types_seen
    assert "sim_telemetry" in types_seen
    assert "sim_event" in types_seen
    assert "sim_metrics" in types_seen
    assert "sensor_file" in types_seen
    assert "artifact" in types_seen

    # 所有消息 topic 一致
    for r in rec.records:
        assert r["topic"] == topic

    conn.stop()


# ── 测试：publish_metrics ───────────────────────────────────────────────────────


def test_publish_metrics_via_connector() -> None:
    """publish_metrics 委托至 SimDataProducer 并发出。"""
    rec = _RecordingProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(rec))
    conn.start("sim-e2e-001")

    msg = {
        "schema_version": "1.0.0", "sim_id": "sim-e2e-001",
        "type": "sim_metrics", "timestamp": time.time(), "seq": 10,
        "payload": {"driving_score": 92.5, "infraction_score": 88},
    }
    assert conn.publish_metrics(msg) is True
    metrics = rec.messages_of_type("sim_metrics")
    assert len(metrics) == 1
    assert metrics[0]["value"]["payload"]["driving_score"] == 92.5
    conn.stop()


# ── 测试：上传任务携带投递时刻的 producer（M5 防串台） ──────────────────────


def test_perform_upload_uses_given_producer() -> None:
    """显式传入 producer 时优先于当前 producer（实例切换后不回退）。"""
    current = _RecordingProducer()
    captured = _RecordingProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(current))
    conn.start("sim-B")
    captured_producer = SimDataProducer(
        _settings(), sim_id="sim-A", producer_factory=_factory(captured)
    )
    assert captured_producer.connect() is True

    class _Result:
        bucket = "b"
        object_key = "k"
        size_bytes = 3
        sha256 = "h"

    conn.uploader.upload = lambda path, data_type="rosbag": _Result()  # type: ignore[method-assign]
    conn._perform_upload(Path("A.mcap"), "mcap", producer=captured_producer)  # noqa: SLF001

    assert [m["value"]["sim_id"] for m in captured.messages_of_type("sensor_file")] == [
        "sim-A"
    ]
    assert current.messages_of_type("sensor_file") == []
    conn.stop()


def test_upload_job_captures_producer_at_enqueue() -> None:
    """上传任务在投递时就固定 producer：实例切换后 sensor_file 仍发往原 sim 的 Topic。"""
    first = _RecordingProducer()
    second = _RecordingProducer()
    conn = CloudConnector(_settings(), producer_factory=_factory(first))
    conn.start("sim-A")
    gate = threading.Event()
    running = threading.Event()

    class _Result:
        bucket = "b"
        object_key = "k"
        size_bytes = 3
        sha256 = "h"

    def _upload(path: Path, data_type: str = "rosbag") -> _Result:
        running.set()
        gate.wait(5.0)
        return _Result()

    conn.uploader.upload = _upload  # type: ignore[method-assign]
    conn.upload_bag(Path("A.mcap"), "mcap", scene_id="scene-A")
    assert running.wait(5.0) is True  # 任务已投递并在上传线程执行中

    # 模拟 A 结束、B 接管（单世界串行下下一个实例会重建 producer）
    next_producer = SimDataProducer(
        _settings(), sim_id="sim-B", producer_factory=_factory(second)
    )
    assert next_producer.connect() is True
    conn._producer = next_producer  # noqa: SLF001
    gate.set()

    assert conn.wait_upload_idle(5.0) is True
    assert [m["value"]["sim_id"] for m in first.messages_of_type("sensor_file")] == ["sim-A"]
    assert second.messages_of_type("sensor_file") == []
    conn.stop()
