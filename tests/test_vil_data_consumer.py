"""VILDataConsumer vil_state 解析集成测试（V2.10.0）。

覆盖：
- carla.{sim_id}.vil_state 格式检测与 _parse_vil_state 调用；
- sim_id 一致性校验（丢弃不匹配消息）；
- legacy_telemetry_topic 兼容订阅；
- _build_topic_list 渲染逻辑；
- 旧 telemetry_clean 格式仍可用（向后兼容）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from hunter_sim.common.models import KafkaSettings, VILSettings
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer
from hunter_sim.vil_mapper.vil_data_consumer import VILDataConsumer


# ── 工具函数 ────────────────────────────────────────────────────────────────────


def _make_consumer(
    sim_id: str = "sim-test-001",
    vehicle_id: str = "veh-1",
    **kafka_overrides: Any,
) -> VILDataConsumer:
    base: dict[str, Any] = {
        "bootstrap_servers": "localhost:9092",
        "telemetry_topic": "carla.{sim_id}.vil_state",
        "sim_id": sim_id,
    }
    base.update(kafka_overrides)
    settings = KafkaSettings(**base)
    return VILDataConsumer(
        settings=settings,
        vil_settings=VILSettings(),
        vehicle_id=vehicle_id,
        buffer=TelemetryBuffer(),
        sim_id=sim_id,
    )


def _vil_state_msg(
    sim_id: str = "sim-test-001",
    seq: int = 1,
    ts: float | None = None,
) -> bytes:
    ts = ts or time.time()
    envelope = {
        "schema_version": "1.0.0",
        "sim_id": sim_id,
        "vehicle_id": "veh-1",
        "scene_id": "scene-100",
        "timestamp": ts,
        "seq": seq,
        "type": "vil_state",
        "payload": {
            "pose": {"x": 10.5, "y": 20.3, "z": 0.0, "roll": 0.0, "pitch": 0.0, "heading": 1.57},
            "twist": {"vx": 5.0, "vy": 0.0, "vz": 0.0, "wx": 0.0, "wy": 0.0, "wz": 0.0},
            "chassis": {"throttle": 0.3, "brake": 0.0, "steer": 0.1, "gear": 2, "speed_m_s": 5.0},
            "perception": {"objects": [], "timestamp": ts},
        },
    }
    return json.dumps(envelope).encode("utf-8")


def _legacy_telemetry_msg(vehicle_id: str = "veh-1", ts: float | None = None) -> bytes:
    ts = ts or time.time()
    payload = {
        "vehicle_id": vehicle_id,
        "timestamp": ts,
        "position": {"x": 1.0, "y": 2.0, "z": 0.0, "yaw": 0.5},
        "velocity": {"vx": 3.0, "vy": 0.0, "vz": 0.0, "speed": 3.0},
        "state": {"ax": 0.0, "ay": 0.0, "az": 0.0, "steering": 0.0, "throttle": 0.2, "brake": 0.0, "gear": 1},
    }
    return json.dumps(payload).encode("utf-8")


# ── 测试：_build_topic_list 渲染 ────────────────────────────────────────────────


class TestBuildTopicList:
    def test_renders_sim_id_template(self) -> None:
        consumer = _make_consumer(sim_id="sim-abc")
        topics = consumer._build_topic_list()
        assert "carla.sim-abc.vil_state" in topics

    def test_legacy_topic_appended_when_configured(self) -> None:
        consumer = _make_consumer(legacy_telemetry_topic="telemetry_clean")
        topics = consumer._build_topic_list()
        assert "telemetry_clean" in topics
        assert "carla.sim-test-001.vil_state" in topics

    def test_no_legacy_topic_by_default(self) -> None:
        consumer = _make_consumer()
        topics = consumer._build_topic_list()
        # 只有主 topic + command topic
        assert len(topics) == 2  # vil_state + command_result
        assert not any("telemetry_clean" in t for t in topics)


# ── 测试：vil_state 格式解析 ────────────────────────────────────────────────────


class TestVilStateParsing:
    def test_handle_vil_state_message_populates_buffer(self) -> None:
        consumer = _make_consumer()
        topic = "carla.sim-test-001.vil_state"
        consumer._handle_message(topic, _vil_state_msg())
        frame = consumer._buffer.get_latest()
        assert frame is not None
        assert frame.vehicle_state.transform.x == 10.5
        assert frame.vehicle_state.transform.y == 20.3
        assert frame.vehicle_state.velocity[0] == 5.0
        assert frame.vehicle_state.steering == 0.1

    def test_sim_id_mismatch_skips_message(self) -> None:
        consumer = _make_consumer(sim_id="sim-mine")
        topic = "carla.sim-mine.vil_state"
        msg = _vil_state_msg(sim_id="sim-other")
        consumer._handle_message(topic, msg)
        assert consumer._buffer.get_latest() is None

    def test_seq_increments_accepted(self) -> None:
        consumer = _make_consumer()
        topic = "carla.sim-test-001.vil_state"
        consumer._handle_message(topic, _vil_state_msg(seq=1))
        consumer._handle_message(topic, _vil_state_msg(seq=2))
        assert consumer._buffer.total_received == 2

    def test_duplicate_seq_rejected(self) -> None:
        consumer = _make_consumer(vil=VILSettings(ordering_guard_enabled=True))
        topic = "carla.sim-test-001.vil_state"
        consumer._handle_message(topic, _vil_state_msg(seq=5))
        consumer._handle_message(topic, _vil_state_msg(seq=5))  # 重复
        assert consumer._buffer.total_received == 1

    def test_perception_objects_parsed(self) -> None:
        consumer = _make_consumer()
        topic = "carla.sim-test-001.vil_state"
        ts = time.time()
        envelope = {
            "sim_id": "sim-test-001",
            "timestamp": ts,
            "seq": 10,
            "type": "vil_state",
            "payload": {
                "pose": {"x": 0.0, "y": 0.0, "z": 0.0, "heading": 0.0},
                "twist": {"vx": 0.0, "vy": 0.0, "vz": 0.0},
                "chassis": {},
                "perception": {
                    "timestamp": ts,
                    "objects": [
                        {"id": 1, "type": "car", "pose": {"x": 5.0, "y": 3.0, "z": 0.0},
                         "length": 4.5, "width": 1.8, "height": 1.5, "vx": 2.0, "vy": 0.0, "confidence": 0.9},
                    ],
                },
            },
        }
        consumer._handle_message(topic, json.dumps(envelope).encode())
        frame = consumer._buffer.get_latest()
        assert frame is not None
        assert frame.perception is not None
        assert len(frame.perception.objects) == 1
        assert frame.perception.objects[0].object_type == "car"


# ── 测试：旧格式兼容 ────────────────────────────────────────────────────────────


class TestLegacyFormatCompat:
    def test_legacy_telemetry_clean_parsed(self) -> None:
        consumer = _make_consumer(legacy_telemetry_topic="telemetry_clean")
        consumer._handle_message("telemetry_clean", _legacy_telemetry_msg())
        frame = consumer._buffer.get_latest()
        assert frame is not None
        assert frame.vehicle_state.transform.x == 1.0
        assert frame.vehicle_state.vehicle_speed == 3.0

    def test_non_matching_vehicle_id_skipped(self) -> None:
        consumer = _make_consumer(vehicle_id="veh-A")
        # 旧格式且 vehicle_id 不匹配 → 跳过
        msg = _legacy_telemetry_msg(vehicle_id="veh-B")
        consumer._handle_message("telemetry_clean", msg)
        assert consumer._buffer.get_latest() is None


# ── 测试：sim_id 构造参数优先于 settings ────────────────────────────────────────


class TestSimIdResolution:
    def test_constructor_sim_id_overrides_settings(self) -> None:
        settings = KafkaSettings(
            bootstrap_servers="localhost:9092",
            telemetry_topic="carla.{sim_id}.vil_state",
            sim_id="from-settings",
        )
        consumer = VILDataConsumer(
            settings=settings,
            vil_settings=VILSettings(),
            vehicle_id="v1",
            buffer=TelemetryBuffer(),
            sim_id="from-ctor",
        )
        assert consumer._sim_id == "from-ctor"
        topics = consumer._build_topic_list()
        assert "carla.from-ctor.vil_state" in topics

    def test_falls_back_to_settings_sim_id(self) -> None:
        settings = KafkaSettings(
            bootstrap_servers="localhost:9092",
            telemetry_topic="carla.{sim_id}.vil_state",
            sim_id="settings-level",
        )
        consumer = VILDataConsumer(
            settings=settings,
            vil_settings=VILSettings(),
            vehicle_id="v1",
            buffer=TelemetryBuffer(),
            sim_id="",  # 空参数 → 回退到 settings
        )
        assert consumer._sim_id == "settings-level"


# ── 测试：_get_primary_telemetry_topic ─────────────────────────────────────────


class TestPrimaryTopicResolution:
    def test_renders_template(self) -> None:
        consumer = _make_consumer(sim_id="sim-xyz")
        assert consumer._get_primary_telemetry_topic() == "carla.sim-xyz.vil_state"

    def test_unrendered_when_no_sim_id(self) -> None:
        settings = KafkaSettings(
            bootstrap_servers="localhost:9092",
            telemetry_topic="carla.{sim_id}.vil_state",
            sim_id="",
        )
        consumer = VILDataConsumer(
            settings=settings,
            vil_settings=VILSettings(),
            vehicle_id="v1",
            buffer=TelemetryBuffer(),
            sim_id="",
        )
        # 无 sim_id → 保留原始模板
        assert "{sim_id}" in consumer._get_primary_telemetry_topic()
