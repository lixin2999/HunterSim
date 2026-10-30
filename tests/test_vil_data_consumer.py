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
from pathlib import Path
from typing import Any

import pytest

from hunter_sim.common.metrics import VIL_MESSAGE_DROPPED_TOTAL
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
        # V3.0.0：command_topic 已移除，默认只有主 vil_state topic
        assert topics == ["carla.sim-test-001.vil_state"]
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

    def test_reset_ordering_guard_accepts_new_session_seq(self) -> None:
        consumer = _make_consumer(vil=VILSettings(ordering_guard_enabled=True))
        topic = "carla.sim-test-001.vil_state"
        consumer._handle_message(topic, _vil_state_msg(seq=5))
        consumer.reset_ordering_guard()
        consumer._handle_message(topic, _vil_state_msg(seq=1))  # 新会话从头计数
        assert consumer._buffer.total_received == 2

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


# ── 测试：V2.12.0 审查加固 ──────────────────────────────────────────────────────


class TestV212Hardening:
    def test_non_telemetry_topic_is_ignored(self) -> None:
        """V3.0.0：command_result 回读 Topic 已移除，非遥测 Topic 一律忽略。"""
        consumer = _make_consumer()
        consumer._handle_message("carla.sim-test-001.vil_state", _vil_state_msg(seq=1))
        assert consumer._buffer.total_received == 1

        cmd = json.dumps({"throttle": 0.4, "steer": 0.1, "brake": 0.0}).encode()
        consumer._handle_message("hunter.veh-1.command_result", cmd)

        assert consumer._buffer.total_received == 1
        latest = consumer._buffer.get_latest()
        assert latest is not None
        assert latest.control_cmd is None
        assert consumer.stats["errors"] == 0

    def test_non_telemetry_topic_without_frame_is_noop(self) -> None:
        consumer = _make_consumer()
        consumer._handle_message("hunter.veh-1.command_result", b'{"throttle": 0.2}')
        assert consumer._buffer.total_received == 0
        assert consumer._buffer.get_latest() is None

    def test_empty_sim_id_does_not_subscribe_template(self) -> None:
        """sim_id 为空时不订阅字面量模板（否则永远收不到数据）。"""
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
        topics = consumer._build_topic_list()
        assert not any("{sim_id}" in t for t in topics)
        assert topics == []

    def test_sim_id_mismatch_counts_dropped_metric(self) -> None:
        consumer = _make_consumer(sim_id="sim-metric")
        before = VIL_MESSAGE_DROPPED_TOTAL.labels(reason="sim_id_mismatch")._value.get()
        consumer._handle_message(
            "carla.sim-metric.vil_state", _vil_state_msg(sim_id="sim-other")
        )
        after = VIL_MESSAGE_DROPPED_TOTAL.labels(reason="sim_id_mismatch")._value.get()
        assert after - before == 1
        assert consumer._buffer.get_latest() is None


# ── 测试：V3.1.2 消费侧接入安全（SASL / TLS / mTLS）─────────────────────────────


def _cert_files(tmp_path: Path) -> tuple[str, str, str]:
    """生成临时 CA / 客户端证书 / 客户端私钥文件，返回三者路径。"""
    paths: list[str] = []
    for name in ("ca-cert.pem", "client-cert.pem", "client-key.pem"):
        path = tmp_path / name
        path.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
        paths.append(str(path))
    return paths[0], paths[1], paths[2]


class TestSecurityConfig:
    """消费端 security_protocol / SASL 凭据 / TLS 证书的构建与接线。"""

    def test_default_is_plaintext(self) -> None:
        """不配安全相关变量时保持旧行为（零改动）。"""
        assert _make_consumer()._security_kwargs == {"security_protocol": "PLAINTEXT"}

    def test_protocol_is_case_insensitive(self) -> None:
        consumer = _make_consumer(
            security_protocol=" sasl_plaintext ", username="u", password="p"
        )
        assert consumer._security_kwargs["security_protocol"] == "SASL_PLAINTEXT"

    def test_sasl_plaintext_without_certs(self) -> None:
        """SASL_PLAINTEXT 只带机制与凭据，不产生 ssl_* 参数。"""
        consumer = _make_consumer(
            security_protocol="SASL_PLAINTEXT",
            sasl_mechanism="SCRAM-SHA-256",
            username="u",
            password="p",
        )
        assert consumer._security_kwargs == {
            "security_protocol": "SASL_PLAINTEXT",
            "sasl_mechanism": "SCRAM-SHA-256",
            "sasl_plain_username": "u",
            "sasl_plain_password": "p",
        }

    def test_sasl_ssl_with_ca(self, tmp_path: Path) -> None:
        ca, _, _ = _cert_files(tmp_path)
        consumer = _make_consumer(
            security_protocol="SASL_SSL", username="u", password="p", ca_cert_path=ca
        )
        kwargs = consumer._security_kwargs
        assert kwargs["security_protocol"] == "SASL_SSL"
        assert kwargs["sasl_mechanism"] == "SCRAM-SHA-512"
        assert kwargs["ssl_cafile"] == ca
        assert "ssl_certfile" not in kwargs

    def test_ssl_mtls_pair(self, tmp_path: Path) -> None:
        """纯 mTLS（无 SASL）：只带证书三项。"""
        ca, cert, key = _cert_files(tmp_path)
        consumer = _make_consumer(
            security_protocol="SSL",
            ca_cert_path=ca,
            client_cert_path=cert,
            client_key_path=key,
        )
        assert consumer._security_kwargs == {
            "security_protocol": "SSL",
            "ssl_cafile": ca,
            "ssl_certfile": cert,
            "ssl_keyfile": key,
        }

    def test_partial_mtls_raises(self, tmp_path: Path) -> None:
        _, cert, _ = _cert_files(tmp_path)
        with pytest.raises(ValueError, match="KAFKA_CLIENT_KEY_PATH"):
            _make_consumer(
                security_protocol="SASL_SSL",
                username="u",
                password="p",
                client_cert_path=cert,
            )

    def test_missing_ca_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="CA 证书"):
            _make_consumer(
                security_protocol="SASL_SSL",
                username="u",
                password="p",
                ca_cert_path=str(tmp_path / "missing.pem"),
            )

    def test_invalid_protocol_raises(self) -> None:
        with pytest.raises(ValueError, match="不支持的 Kafka 安全协议"):
            _make_consumer(security_protocol="SASL_TLS")

    def test_sasl_without_username_raises(self) -> None:
        """密码类机制缺账号是必然失败，构造期就报错而不是空转。"""
        with pytest.raises(ValueError, match="KAFKA_USERNAME"):
            _make_consumer(security_protocol="SASL_SSL")

    def test_credentials_ignored_when_plaintext(self) -> None:
        """PLAINTEXT 下账号不会被打包（kafka-python 不启用 SASL），但不报错。"""
        kwargs = _make_consumer(username="u", password="p")._security_kwargs
        assert kwargs == {"security_protocol": "PLAINTEXT"}

    def test_empty_username_treated_as_unset(self) -> None:
        """`KAFKA_USERNAME=` 空串视为未配置（.env 模板默认值不应触发告警）。"""
        kwargs = _make_consumer(username="", password="")._security_kwargs
        assert kwargs == {"security_protocol": "PLAINTEXT"}

    def test_consumer_kwargs_carry_security(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """真实 KafkaConsumer 参数里不能出现旧键名 sasl_username（会直接抛错）。"""
        kafka = pytest.importorskip("kafka")
        captured: dict[str, Any] = {}

        class _FakeConsumer:
            def __init__(self, *topics: str, **kwargs: Any) -> None:
                captured["topics"] = topics
                captured.update(kwargs)

        monkeypatch.setattr(kafka, "KafkaConsumer", _FakeConsumer)
        consumer = _make_consumer(security_protocol="SASL_SSL", username="u", password="p")
        assert consumer._create_consumer(["carla.sim-test-001.vil_state"]) is not None
        assert captured["security_protocol"] == "SASL_SSL"
        assert captured["sasl_plain_username"] == "u"
        assert captured["sasl_plain_password"] == "p"
        assert "sasl_username" not in captured

    def test_dlq_producer_reuses_security(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """坏消息转发同样要带接入参数，否则 ACL 集群上 DLQ 写不进去。"""
        kafka = pytest.importorskip("kafka")
        captured: dict[str, Any] = {}

        class _FakeProducer:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        monkeypatch.setattr(kafka, "KafkaProducer", _FakeProducer)
        consumer = _make_consumer(
            security_protocol="SASL_SSL",
            username="u",
            password="p",
            dlq_topic="carla.vil_state.dlq",
        )
        assert consumer._get_dlq_producer() is not None
        assert captured["security_protocol"] == "SASL_SSL"
        assert captured["sasl_plain_username"] == "u"
