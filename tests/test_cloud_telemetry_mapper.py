"""telemetry_mapper 契约映射单测（V2.2.0，无 Qt/无引擎）。

校验 `build_telemetry / build_health / build_event` 产出的载荷满足 HunterCore
Kafka schema 的 required 字段集合，且不引入契约外字段（additionalProperties=false）。
"""

from __future__ import annotations

import math

from hunter_sim.cloud.telemetry_mapper import (
    build_event,
    build_health,
    build_telemetry,
)
from hunter_sim.common.models import Transform, VehicleState

_TELEMETRY_REQUIRED = {
    "vehicle_id",
    "timestamp",
    "seq",
    "chassis",
    "localization",
    "perception",
    "planning",
    "control",
    "system",
}
_HEALTH_REQUIRED = {"vehicle_id", "timestamp", "status", "system"}
_EVENT_REQUIRED = {"vehicle_id", "timestamp", "event_type", "event_level"}


def _state(yaw: float = 3.0, speed: float = 4.2) -> VehicleState:
    return VehicleState(
        time_stamp=1700000000.123,
        transform=Transform(x=10.0, y=-5.0, z=0.3, pitch=0.0, yaw=yaw, roll=0.0),
        velocity=(speed, 0.0, 0.0),
        angular_velocity=(0.0, 0.0, 0.1),
        steering=0.05,
        vehicle_speed=speed,
    )


def test_telemetry_top_level_required_and_no_extras() -> None:
    """telemetry 顶层字段集恰为契约 required（无多余键）。"""
    payload = build_telemetry("SIM_HUNTER_SE_001", 7, _state())
    assert set(payload) == _TELEMETRY_REQUIRED
    assert payload["vehicle_id"] == "SIM_HUNTER_SE_001"
    assert payload["seq"] == 7
    assert payload["timestamp"] == 1700000000.123


def test_telemetry_subsections_have_required_fields() -> None:
    """chassis/localization 段包含关键子字段且取值来自 VehicleState。"""
    payload = build_telemetry("V", 1, _state(speed=3.0))
    chassis = payload["chassis"]
    localization = payload["localization"]
    assert chassis["control_mode"] == "SIM"
    assert chassis["battery_soc"] == 100
    assert chassis["velocity"] == 3.0
    assert localization["x"] == 10.0
    assert localization["y"] == -5.0
    assert localization["linear_velocity"][0] == 3.0


def test_heading_normalized_to_pm_pi() -> None:
    """航向输出落在 [-π, π]（Transform 已限幅，映射保持等值）。"""
    payload = build_telemetry("V", 1, _state(yaw=3.0))
    heading = payload["localization"]["heading"]
    assert -math.pi <= heading <= math.pi
    assert heading == round(3.0, 6)


def test_telemetry_overrides_respected() -> None:
    """调用方传入的 perception/planning/control/system 覆盖缺省。"""
    perception = {
        "detected_objects": 3,
        "fps": 20.0,
        "latency_ms": 5.0,
        "object_types": {"car": 3},
    }
    system = {
        "cpu_usage": 42.0,
        "gpu_usage": 55.0,
        "memory_usage_mb": 1024,
        "gpu_temp": 60.0,
        "cpu_temp": 50.0,
        "network_rssi": -50,
        "network_latency_ms": 10.0,
    }
    payload = build_telemetry("V", 1, _state(), perception=perception, system=system)
    assert payload["perception"]["detected_objects"] == 3
    assert payload["system"]["cpu_usage"] == 42.0


def test_health_payload() -> None:
    """health 字段集为 required 四键，status 可指定。"""
    payload = build_health("V", 1700000000.5, status="remote_driving")
    assert set(payload) == _HEALTH_REQUIRED
    assert payload["status"] == "remote_driving"


def test_event_level_auto_mapping() -> None:
    """event 等级按 EVENT_LEVEL_BY_TYPE 自动映射。"""
    payload = build_event("V", 1700000000.0, "collision_warning", description="sim crash")
    assert set(payload) >= _EVENT_REQUIRED
    assert payload["event_level"] == "critical"
    assert payload["description"] == "sim crash"


def test_event_unknown_type_raises() -> None:
    """未知 event_type 抛 ValueError（契约 19 种之外）。"""
    try:
        build_event("V", 1.0, "not_a_real_event")
    except ValueError:
        return
    raise AssertionError("未知 event_type 应抛 ValueError")
