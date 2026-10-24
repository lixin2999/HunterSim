"""sim_data_mapper 单测（P0：6 类消息构造函数，字段完整性）。

验证统一信封结构（schema_version/sim_id/vehicle_id/scene_id/timestamp/seq/type/payload），
以及各类型 payload 字段的完整性和值正确性。无外部依赖。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from hunter_sim.cloud.sim_data_mapper import (
    SCHEMA_VERSION,
    SimStatusPayload,
    build_artifact,
    build_sensor_file,
    build_sim_event,
    build_sim_metrics,
    build_sim_status,
    build_sim_telemetry,
)

# ─── 信封公共字段验证辅助 ──────────────────────────────────────────────────────


def _assert_envelope(msg: dict[str, Any], expected_type: str, sim_id: str = "sim-test-001") -> None:
    """验证所有消息共有的信封字段。"""
    assert msg["schema_version"] == SCHEMA_VERSION
    assert msg["sim_id"] == sim_id
    assert msg["type"] == expected_type
    assert isinstance(msg["timestamp"], float)
    assert isinstance(msg["seq"], int)
    assert "payload" in msg
    assert "scene_id" in msg


# ─── sim_status ────────────────────────────────────────────────────────────────


class TestBuildSimStatus:
    def _make_payload(self) -> SimStatusPayload:
        return SimStatusPayload(
            status="RUNNING",
            progress_percent=42.5,
            elapsed_time=10.0,
            total_time=60.0,
            actors_count=5,
            collisions=1,
            lane_invasions=0,
            ego_state={"x": 1.0, "y": 2.0, "heading": 0.5, "velocity": 3.0, "steering": 0.1},
            message=None,
        )

    def test_envelope_fields(self) -> None:
        payload = self._make_payload()
        msg = build_sim_status("sim-test-001", "scene-abc", payload, seq=7, timestamp=100.0)
        _assert_envelope(msg, "sim_status")
        assert msg["seq"] == 7
        assert msg["timestamp"] == 100.0
        assert msg["vehicle_id"] is None  # SIL 模式

    def test_payload_fields(self) -> None:
        payload = self._make_payload()
        msg = build_sim_status("sim-test-001", "scene-abc", payload, seq=0, timestamp=100.0)
        p = msg["payload"]
        assert p["status"] == "RUNNING"
        assert p["progress_percent"] == pytest.approx(42.5)
        assert p["elapsed_time"] == pytest.approx(10.0)
        assert p["total_time"] == pytest.approx(60.0)
        assert p["actors_count"] == 5
        assert p["collisions"] == 1
        assert p["lane_invasions"] == 0
        assert p["ego_state"]["x"] == pytest.approx(1.0)
        assert "updated_at" in p

    def test_timestamp_auto(self) -> None:
        payload = self._make_payload()
        msg = build_sim_status("sim-test-001", "scene-abc", payload)
        assert msg["timestamp"] > 0


# ─── sim_telemetry ─────────────────────────────────────────────────────────────


class TestBuildSimTelemetry:
    def test_envelope_and_payload(self) -> None:
        msg = build_sim_telemetry(
            sim_id="sim-test-001",
            vehicle_id="HUNTER-001",
            scene_id="scene-xyz",
            seq=42,
            ts=1724035200.123,
            pose={"x": 10.0, "y": 20.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "heading": 1.57},
            twist={"vx": 5.0, "vy": 0.0, "vz": 0.0, "wx": 0.0, "wy": 0.0, "wz": 0.1},
            velocity={"m_s": 5.0, "km_h": 18.0},
            steering=0.05,
            acceleration={"ax": 0.5, "ay": 0.0, "az": -9.8},
        )
        _assert_envelope(msg, "sim_telemetry")
        assert msg["vehicle_id"] == "HUNTER-001"
        p = msg["payload"]
        assert p["pose"]["x"] == pytest.approx(10.0)
        assert p["twist"]["vx"] == pytest.approx(5.0)
        assert p["velocity"]["km_h"] == pytest.approx(18.0)
        assert p["steering"] == pytest.approx(0.05, abs=1e-6)
        assert p["acceleration"]["az"] == pytest.approx(-9.8)

    def test_sil_mode_vehicle_id_none(self) -> None:
        msg = build_sim_telemetry("sim-test-001", None, "", 0, time.time(),
                                  {}, {}, {}, 0.0, {})
        assert msg["vehicle_id"] is None


# ─── sim_event ─────────────────────────────────────────────────────────────────


class TestBuildSimEvent:
    def test_basic_event(self) -> None:
        msg = build_sim_event(
            sim_id="sim-test-001",
            vehicle_id="HUNTER-001",
            scene_id="scene-1",
            seq=10,
            ts=200.0,
            event_type="collision",
            event_level="critical",
            sim_time=5.5,
            description="Car hit static prop",
            data={"other_actor_id": "33"},
        )
        _assert_envelope(msg, "sim_event")
        p = msg["payload"]
        assert p["event_type"] == "collision"
        assert p["event_level"] == "critical"
        assert p["sim_time"] == pytest.approx(5.5)
        assert p["description"] == "Car hit static prop"
        assert p["data"]["other_actor_id"] == "33"

    def test_no_data_defaults_empty(self) -> None:
        msg = build_sim_event(
            "sim-test-001", None, "", 0, 100.0,
            "lane_invasion", "warning", 3.0, "left lane",
        )
        assert msg["payload"]["data"] == {}


# ─── sim_metrics ───────────────────────────────────────────────────────────────


class TestBuildSimMetrics:
    def test_full_metrics(self) -> None:
        msg = build_sim_metrics(
            sim_id="sim-test-001",
            vehicle_id="HUNTER-001",
            scene_id="scene-1",
            seq=20,
            ts=300.0,
            grade="A",
            passed=True,
            safety={"collision_count": 0, "min_ttc_s": 5.2},
            efficiency={"avg_speed_ms": 8.0, "total_distance_m": 120.0},
            comfort={"avg_acceleration_ms2": 0.8, "max_acceleration_ms2": 1.5},
            task={"completion_rate": 1.0, "scene_duration_s": 30.0},
            success_criteria_result={"collision_free": True},
            takeover_required=False,
        )
        _assert_envelope(msg, "sim_metrics")
        p = msg["payload"]
        assert p["grade"] == "A"
        assert p["passed"] is True
        assert p["safety"]["collision_count"] == 0
        assert p["task"]["takeover_required"] is False
        assert p["success_criteria_result"]["collision_free"] is True


# ─── sensor_file ───────────────────────────────────────────────────────────────


class TestBuildSensorFile:
    def test_sensor_file(self) -> None:
        msg = build_sensor_file(
            sim_id="sim-test-001",
            seq=30,
            ts=400.0,
            bucket="hunter-rosbag",
            object_key="SIM-sim-test-001/rosbag_20261023_001.mbag",
            size_bytes=1024 * 1024,
            sha256="a1b2c3d4",
            sensor_type="rosbag",
            start_time=0.0,
            end_time=30.0,
            frame_count=1500,
            vehicle_id="HUNTER-001",
            scene_id="scene-1",
        )
        _assert_envelope(msg, "sensor_file")
        p = msg["payload"]
        assert p["bucket"] == "hunter-rosbag"
        assert p["size_bytes"] == 1024 * 1024
        assert p["sha256"] == "a1b2c3d4"
        assert p["frame_count"] == 1500
        assert p["sensor_type"] == "rosbag"


# ─── artifact ──────────────────────────────────────────────────────────────────


class TestBuildArtifact:
    def test_artifact(self) -> None:
        msg = build_artifact(
            sim_id="sim-test-001",
            seq=40,
            ts=500.0,
            name="eval.json",
            bucket="hunter-scene-assets",
            object_key="scenarios/simulations/sim-test-001/eval.json",
            size_bytes=2048,
            sha256="deadbeef",
            media_type="application/json",
            vehicle_id=None,
            scene_id="scene-1",
        )
        _assert_envelope(msg, "artifact")
        p = msg["payload"]
        assert p["name"] == "eval.json"
        assert p["media_type"] == "application/json"
        assert p["size_bytes"] == 2048


# ─── SimStatusPayload __post_init__ ───────────────────────────────────────────


class TestSimStatusPayload:
    def test_updated_at_auto(self) -> None:
        p = SimStatusPayload(
            status="PAUSED", progress_percent=50.0, elapsed_time=30.0,
            total_time=60.0, actors_count=3, collisions=0, lane_invasions=0,
            ego_state={},
        )
        assert p.updated_at != ""
        assert "T" in p.updated_at  # ISO8601 格式

    def test_updated_at_explicit(self) -> None:
        p = SimStatusPayload(
            status="COMPLETED", progress_percent=100.0, elapsed_time=60.0,
            total_time=60.0, actors_count=0, collisions=0, lane_invasions=0,
            ego_state={}, updated_at="2026-10-23T00:00:00+00:00",
        )
        assert p.updated_at == "2026-10-23T00:00:00+00:00"


# ─── V2.12.0：对照方案 §5.4 / §7.3.1 的契约回归 ────────────────────────────


def _status_payload() -> SimStatusPayload:
    return SimStatusPayload(
        status="RUNNING", progress_percent=10.0, elapsed_time=5.0,
        total_time=50.0, actors_count=2, collisions=0, lane_invasions=0,
        ego_state={},
    )


class TestV212Contract:
    """方案 §5.4 字段表与 §7.3.1 幂等键要求的回归用例。"""

    def test_sim_status_carries_vehicle_id_in_vil(self) -> None:
        msg = build_sim_status(
            "sim-1", "scene-1", _status_payload(), seq=1, vehicle_id="HUNTER-001"
        )
        assert msg["vehicle_id"] == "HUNTER-001"

    def test_sim_status_sil_keeps_null_vehicle_id(self) -> None:
        msg = build_sim_status("sim-1", "scene-1", _status_payload(), seq=1)
        assert msg["vehicle_id"] is None

    def test_sim_status_events_triggered(self) -> None:
        payload = SimStatusPayload(
            status="RUNNING", progress_percent=0.0, elapsed_time=0.0,
            total_time=10.0, actors_count=1, collisions=0, lane_invasions=0,
            ego_state={}, events_triggered=7,
        )
        msg = build_sim_status("sim-1", "scene-1", payload, seq=1)
        assert msg["payload"]["events_triggered"] == 7

    def test_telemetry_uses_steering_angle(self) -> None:
        msg = build_sim_telemetry(
            "sim-1", None, "scene-1", 1, 100.0, {}, {}, {}, 0.25, {}
        )
        assert msg["payload"]["steering_angle"] == pytest.approx(0.25, abs=1e-6)
        # 兼容别名仍保留
        assert msg["payload"]["steering"] == pytest.approx(0.25, abs=1e-6)

    def test_sim_event_has_event_id(self) -> None:
        msg = build_sim_event(
            "sim-1", None, "scene-1", 2, 100.0, "collision", "critical", 1.0, "hit"
        )
        event_id = msg["payload"]["event_id"]
        assert isinstance(event_id, str) and len(event_id) >= 8

    def test_sim_event_event_id_is_stable_when_given(self) -> None:
        msg = build_sim_event(
            "sim-1", None, "scene-1", 2, 100.0, "collision", "critical", 1.0, "hit",
            event_id="evt-fixed-1",
        )
        assert msg["payload"]["event_id"] == "evt-fixed-1"

    def test_sensor_file_has_md5_format_topics(self) -> None:
        msg = build_sensor_file(
            sim_id="sim-1", seq=3, ts=100.0, bucket="b", object_key="k",
            size_bytes=10, sha256="abc", sensor_type="rosbag",
            start_time=0.0, end_time=1.0, frame_count=5,
            md5="def", file_format="rosbag2", topics=["/carla/ego/odom"],
        )
        p = msg["payload"]
        assert p["md5"] == "def"
        assert p["format"] == "rosbag2"
        assert p["topics"] == ["/carla/ego/odom"]

    def test_sensor_file_optional_fields_default_empty(self) -> None:
        msg = build_sensor_file(
            sim_id="sim-1", seq=3, ts=100.0, bucket="b", object_key="k",
            size_bytes=10, sha256="abc", sensor_type="rosbag",
            start_time=0.0, end_time=1.0, frame_count=5,
        )
        p = msg["payload"]
        assert p["md5"] == ""
        assert p["format"] == ""
        assert p["topics"] == []
