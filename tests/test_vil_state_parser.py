"""vil_state_parser 单测（P0：解析 carla.{sim_id}.vil_state 格式）。

验证：
- 完整信封（含 payload 嵌套）解析为 TelemetryFrame；
- 直接传入 payload 字典解析（扁平格式）；
- pose/twist/chassis/perception/control 字段映射正确；
- 缺少可选字段时不抛出异常；
- 与 telemetry_clean 旧格式互不干扰（格式检测）。
"""

from __future__ import annotations

from typing import Any

import pytest

from hunter_sim.cloud.vil_state_parser import parse_vil_state
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryFrame


# ─── 测试辅助 ──────────────────────────────────────────────────────────────────


def _full_envelope(**overrides: Any) -> dict[str, Any]:
    """返回一个完整的 vil_state 信封消息（方案 §5.3）。"""
    envelope: dict[str, Any] = {
        "schema_version": "1.0.0",
        "sim_id": "sim-test-001",
        "timestamp": 1724035200.0,
        "seq": 42,
        "type": "vil_state",
        "payload": {
            "pose": {"x": 1.0, "y": 2.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "heading": 1.57},
            "twist": {"vx": 5.0, "vy": 0.0, "vz": 0.0, "wx": 0.0, "wy": 0.0, "wz": 0.1},
            "chassis": {"throttle": 0.5, "brake": 0.0, "steer": 0.1, "gear": 3, "speed_m_s": 5.0},
            "perception": {
                "timestamp": 1724035200.0,
                "objects": [
                    {"id": 1, "type": "car", "pose": {"x": 10.0, "y": 5.0, "z": 0.0, "heading": 0.0},
                     "length": 4.5, "width": 1.8, "height": 1.4, "vx": 2.0, "vy": 0.0, "confidence": 0.9},
                ],
            },
            "control": {"throttle": 0.3, "steer": 0.05, "brake": 0.0},
        },
    }
    envelope.update(overrides)
    return envelope


# ─── 完整信封解析 ──────────────────────────────────────────────────────────────


class TestParseVilStateFullEnvelope:
    def test_returns_telemetry_frame(self) -> None:
        frame = parse_vil_state(_full_envelope(), vehicle_id="HUNTER-001")
        assert isinstance(frame, TelemetryFrame)

    def test_pose_to_transform(self) -> None:
        frame = parse_vil_state(_full_envelope())
        t = frame.vehicle_state.transform
        assert t.x == pytest.approx(1.0)
        assert t.y == pytest.approx(2.0)
        assert t.z == pytest.approx(0.0)
        assert t.yaw == pytest.approx(1.57)

    def test_twist_to_velocity(self) -> None:
        frame = parse_vil_state(_full_envelope())
        vx, vy, vz = frame.vehicle_state.velocity
        assert vx == pytest.approx(5.0)
        assert vy == pytest.approx(0.0)
        assert vz == pytest.approx(0.0)

    def test_chassis_to_controls(self) -> None:
        frame = parse_vil_state(_full_envelope())
        vs = frame.vehicle_state
        assert vs.throttle == pytest.approx(0.5)
        assert vs.brake == pytest.approx(0.0)
        assert vs.steering == pytest.approx(0.1)
        assert vs.gear == 3
        assert vs.vehicle_speed == pytest.approx(5.0)

    def test_timestamp_from_envelope(self) -> None:
        frame = parse_vil_state(_full_envelope())
        assert frame.timestamp == pytest.approx(1724035200.0)

    def test_vehicle_id(self) -> None:
        frame = parse_vil_state(_full_envelope(), vehicle_id="CAR-99")
        assert frame.vehicle_id == "CAR-99"

    def test_perception_objects(self) -> None:
        frame = parse_vil_state(_full_envelope())
        assert frame.perception is not None
        assert len(frame.perception.objects) == 1
        obj = frame.perception.objects[0]
        assert obj.object_id == 1
        assert obj.object_type == "car"
        assert obj.transform.x == pytest.approx(10.0)

    def test_control_cmd(self) -> None:
        frame = parse_vil_state(_full_envelope())
        assert frame.control_cmd is not None
        assert frame.control_cmd["throttle"] == pytest.approx(0.3)
        assert frame.control_cmd["steer"] == pytest.approx(0.05)
        assert frame.control_cmd["brake"] == pytest.approx(0.0)


# ─── 扁平 payload 解析 ─────────────────────────────────────────────────────────


class TestParseVilStateFlatPayload:
    def test_flat_pose(self) -> None:
        """直接传入含 pose 的 payload 字典（无外层信封）。"""
        payload = {
            "timestamp": 200.0,
            "pose": {"x": 3.0, "y": 4.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "heading": 0.5},
            "twist": {"vx": 10.0, "vy": 1.0, "vz": 0.0, "wx": 0.0, "wy": 0.0, "wz": 0.0},
            "chassis": {"steer": 0.0, "throttle": 1.0, "brake": 0.0, "gear": 2},
        }
        frame = parse_vil_state(payload)
        assert frame.vehicle_state.transform.x == pytest.approx(3.0)
        assert frame.vehicle_state.velocity[0] == pytest.approx(10.0)
        assert frame.timestamp == pytest.approx(200.0)


# ─── 缺少可选字段 ──────────────────────────────────────────────────────────────


class TestParseVilStateMissingOptionals:
    def test_no_perception(self) -> None:
        payload = {
            "timestamp": 100.0,
            "pose": {"x": 0.0},
            "twist": {},
            "chassis": {},
        }
        frame = parse_vil_state(payload)
        assert frame.perception is None
        assert frame.control_cmd is None

    def test_no_control(self) -> None:
        payload = {
            "timestamp": 100.0,
            "pose": {"x": 0.0},
            "twist": {},
            "chassis": {},
            "perception": {"objects": []},
        }
        frame = parse_vil_state(payload)
        assert frame.control_cmd is None
        assert frame.perception is not None
        assert len(frame.perception.objects) == 0


# ─── receive_time 注入 ─────────────────────────────────────────────────────────


class TestReceiveTime:
    def test_custom_receive_time(self) -> None:
        frame = parse_vil_state(_full_envelope(), receive_time=999.0)
        assert frame.receive_time == pytest.approx(999.0)

    def test_auto_receive_time(self) -> None:
        frame = parse_vil_state(_full_envelope())
        # 默认取当前时间，应大于 envelope 时间戳
        assert frame.receive_time > 1724035200.0


# ─── 格式检测辅助（供 VILDataConsumer 使用）────────────────────────────────────


class TestFormatDetection:
    def test_is_vil_state_format(self) -> None:
        """含 pose 字段的 payload 字典应被识别为 vil_state 格式。"""
        msg = _full_envelope()
        inner = msg.get("payload", msg)
        assert "pose" in inner  # VILDataConsumer 的检测逻辑

    def test_is_old_format(self) -> None:
        """旧 telemetry_clean 含 position 而非 pose。"""
        old_msg = {
            "vehicle_id": "SIM_001",
            "timestamp": 100.0,
            "position": {"x": 1.0, "y": 2.0, "z": 0.0},
            "velocity": {"x": 5.0, "y": 0.0, "z": 0.0},
        }
        assert "pose" not in old_msg


# ─── V2.12.0：方案 §5.3 标准扁平契约（无 payload 包裹）─────────────────────────


def _scheme_v53_message(**overrides: Any) -> dict[str, Any]:
    """按方案 §5.3 构造一条真实平台转发格式的 vil_state 消息。"""
    msg: dict[str, Any] = {
        "schema_version": "1.0.0",
        "sim_id": "sim-20261023-0001",
        "source_seq": 128,
        "source_timestamp": 1724035200.5,
        "forwarded_at": 1724035200.58,
        "pose": {"frame": "map", "x": 12.5, "y": -3.0, "z": 0.0,
                 "roll": 0.0, "pitch": 0.0, "yaw": 1.2},
        "twist": {"linear": [8.0, 0.5, 0.0], "angular": [0.0, 0.0, 0.15]},
        "chassis": {"velocity": 8.02, "steering_angle": 0.08,
                    "control_mode": "auto", "vehicle_state": "driving",
                    "fault_code": 0, "gear": 4},
        "perception": {
            "detected_objects": 1,
            "fps": 12.5,
            "latency_ms": 30.0,
            "objects": [{
                "object_id": "obj-1", "type": "car",
                "x": 30.0, "y": 2.0, "z": 0.0, "yaw": 1.1,
                "length": 4.6, "width": 1.9, "height": 1.5,
                "vx": 3.0, "vy": 0.0, "confidence": 0.88,
            }],
        },
        "planning": {"current_behavior": "lane_keep", "trajectory": []},
        "control": {"target_velocity": 8.0, "target_steer": 0.06,
                    "target_throttle": 0.4, "target_brake": 0.0},
        "calibration": {
            "calibration_id": "cal-2026-10", "map_x": 100.0, "map_y": 200.0,
            "map_heading": 0.0, "odom_x": 0.0, "odom_y": 0.0, "odom_heading": 0.0,
        },
    }
    msg.update(overrides)
    return msg


class TestSchemeV53Contract:
    """脱离旧 telemetry_clean 写法，验证真实平台契约的字段映射。"""

    def test_twist_linear_angular_arrays(self) -> None:
        frame = parse_vil_state(_scheme_v53_message())
        vs = frame.vehicle_state
        assert vs.velocity == pytest.approx((8.0, 0.5, 0.0))
        assert vs.angular_velocity == pytest.approx((0.0, 0.0, 0.15))

    def test_chassis_velocity_and_steering_angle(self) -> None:
        frame = parse_vil_state(_scheme_v53_message())
        vs = frame.vehicle_state
        assert vs.vehicle_speed == pytest.approx(8.02)
        assert vs.steering == pytest.approx(0.08)
        assert vs.gear == 4

    def test_pose_yaw_field(self) -> None:
        frame = parse_vil_state(_scheme_v53_message())
        assert frame.vehicle_state.transform.yaw == pytest.approx(1.2)
        assert frame.vehicle_state.transform.x == pytest.approx(12.5)

    def test_source_timestamp_preferred(self) -> None:
        frame = parse_vil_state(_scheme_v53_message())
        assert frame.timestamp == pytest.approx(1724035200.5)
        assert frame.source_seq == 128
        assert frame.forwarded_at == pytest.approx(1724035200.58)
        assert frame.platform_forward_lag_ms == pytest.approx(80.0, abs=0.1)

    def test_string_object_id_does_not_raise(self) -> None:
        frame = parse_vil_state(_scheme_v53_message())
        assert frame.perception is not None
        obj = frame.perception.objects[0]
        assert isinstance(obj.object_id, int)
        # CRC32 稳定映射：同一字符串多次解析结果一致
        again = parse_vil_state(_scheme_v53_message())
        assert again.perception.objects[0].object_id == obj.object_id
        assert obj.object_type == "car"
        assert obj.confidence == pytest.approx(0.88)

    def test_calibration_preserved(self) -> None:
        frame = parse_vil_state(_scheme_v53_message())
        assert frame.calibration is not None
        assert frame.calibration["calibration_id"] == "cal-2026-10"
        assert frame.calibration["map_x"] == pytest.approx(100.0)

    def test_control_target_fields(self) -> None:
        frame = parse_vil_state(_scheme_v53_message())
        assert frame.control_cmd is not None
        assert frame.control_cmd["throttle"] == pytest.approx(0.4)
        assert frame.control_cmd["steer"] == pytest.approx(0.06)
        # chassis.steering_angle 是实际值，不应被 target 覆盖
        assert frame.vehicle_state.steering == pytest.approx(0.08)

    def test_missing_pose_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            parse_vil_state({"twist": {}, "chassis": {}})

    def test_non_dict_twist_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            parse_vil_state({"pose": {"x": 0.0}, "twist": [1, 2, 3]})

    def test_confidence_clamped(self) -> None:
        msg = _scheme_v53_message()
        msg["perception"]["objects"][0]["confidence"] = 1.7
        frame = parse_vil_state(msg)
        assert frame.perception.objects[0].confidence == pytest.approx(1.0)
