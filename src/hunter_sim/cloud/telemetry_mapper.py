"""CARLA 数字孪生状态 → HunterCore Kafka 契约载荷映射（V2.2.0）。

严格对齐 `contracts/kafka/schemas/{telemetry,health,event}.schema.json`：
所有载荷只填 required 字段并保证 `additionalProperties=false`（不引入契约外字段）。

数字孪生没有真实感知/规划/电池子系统，相应段以合法缺省值填充，可由调用方
（`TelemetryProducer`）按仿真上下文覆盖。
"""

from __future__ import annotations

import math
from typing import Any, Optional

from hunter_sim.common.models import VehicleState

# 事件等级映射（对齐 contracts/database/enums.md EVENT_LEVEL_BY_TYPE 常用子集）
_EVENT_LEVEL_BY_TYPE: dict[str, str] = {
    "harsh_acceleration": "warning",
    "harsh_braking": "warning",
    "harsh_turning": "warning",
    "over_speed": "warning",
    "collision_pre_warning": "warning",
    "collision_warning": "critical",
    "manual_takeover": "info",
    "emergency_stop": "critical",
    "sensor_fault": "critical",
    "perception_fault": "critical",
    "planning_fault": "critical",
    "control_fault": "critical",
}

_VALID_EVENT_TYPES = frozenset(_EVENT_LEVEL_BY_TYPE)


def _normalize_heading(yaw_rad: float) -> float:
    """把任意弧度航向归一到 [-π, π]（契约要求 heading ∈ [-π, π]）。"""
    return (yaw_rad + math.pi) % (2.0 * math.pi) - math.pi


def _default_system() -> dict[str, Any]:
    """system 段合法缺省（telemetry/health 复用，字段完全一致）。"""
    return {
        "cpu_usage": 0.0,
        "gpu_usage": 0.0,
        "memory_usage_mb": 0,
        "gpu_temp": 0.0,
        "cpu_temp": 0.0,
        "network_rssi": 0,
        "network_latency_ms": 0.0,
    }


def build_telemetry(
    vehicle_id: str,
    seq: int,
    state: VehicleState,
    *,
    perception: Optional[dict[str, Any]] = None,
    planning: Optional[dict[str, Any]] = None,
    control: Optional[dict[str, Any]] = None,
    system: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """把一帧 `VehicleState` 映射为 `hunter.{vehicle_id}.telemetry` 载荷。

    Args:
        vehicle_id: 数字孪生车标识（同时作为 Kafka 消息 key）。
        seq: 单调递增序号（幂等去重/丢包检测）。
        state: CARLA 车辆实时状态。
        perception/planning/control/system: 可选覆盖对应段（默认填合法缺省）。

    Returns:
        满足 telemetry.schema.json 的 dict（仅含 required 字段）。
    """
    tf = state.transform
    vx, vy, vz = state.velocity
    wx, wy, wz = state.angular_velocity

    chassis = {
        "velocity": round(state.vehicle_speed, 4),
        "steering_angle": round(state.steering, 6),
        "battery_voltage": 0.0,
        "battery_soc": 100,
        "battery_current": 0.0,
        "battery_temp": 0.0,
        "control_mode": "SIM",
        "vehicle_state": "NORMAL",
        "fault_code": 0,
        "motor_rpm": [0],
        "motor_current": [0.0],
        "motor_temp": [0],
    }

    localization = {
        "x": round(tf.x, 4),
        "y": round(tf.y, 4),
        "z": round(tf.z, 4),
        "roll": round(tf.roll, 6),
        "pitch": round(tf.pitch, 6),
        "heading": round(_normalize_heading(tf.yaw), 6),
        "linear_velocity": [round(vx, 4), round(vy, 4), round(vz, 4)],
        "angular_velocity": [round(wx, 4), round(wy, 4), round(wz, 4)],
        "position_std": 0.0,
        "heading_std": 0.0,
    }

    default_perception = {
        "detected_objects": 0,
        "fps": 0.0,
        "latency_ms": 0.0,
        "object_types": {},
    }
    default_planning = {
        "trajectory_length": 0.0,
        "trajectory_points": 0,
        "planning_latency_ms": 0.0,
        "current_behavior": "idle",
    }
    default_control = {
        "target_velocity": round(state.vehicle_speed, 4),
        "target_steer": round(state.steering, 6),
        "velocity_error": 0.0,
        "steer_error": 0.0,
        "control_latency_ms": 0.0,
    }

    return {
        "vehicle_id": vehicle_id,
        "timestamp": round(state.time_stamp, 3),
        "seq": seq,
        "chassis": chassis,
        "localization": localization,
        "perception": perception or default_perception,
        "planning": planning or default_planning,
        "control": control or default_control,
        "system": system or _default_system(),
    }


def build_health(
    vehicle_id: str,
    timestamp: float,
    *,
    status: str = "auto_driving",
    system: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构造 `hunter.{vehicle_id}.health` 心跳载荷（1Hz，acks=0）。

    Args:
        status: 车辆业务状态（8 态之一），默认 `auto_driving`。
    """
    return {
        "vehicle_id": vehicle_id,
        "timestamp": round(timestamp, 3),
        "status": status,
        "system": system or _default_system(),
    }


def build_event(
    vehicle_id: str,
    timestamp: float,
    event_type: str,
    *,
    description: str = "",
    data: Optional[dict[str, Any]] = None,
    data_file_url: Optional[str] = None,
) -> dict[str, Any]:
    """构造 `hunter.{vehicle_id}.event` 载荷（等级按 EVENT_LEVEL_BY_TYPE 自动映射）。

    Raises:
        ValueError: event_type 不在契约 19 种取值内。
    """
    if event_type not in _VALID_EVENT_TYPES:
        raise ValueError(f"unknown event_type: {event_type!r} (not in HunterCore contract)")
    payload: dict[str, Any] = {
        "vehicle_id": vehicle_id,
        "timestamp": round(timestamp, 3),
        "event_type": event_type,
        "event_level": _EVENT_LEVEL_BY_TYPE[event_type],
    }
    if description:
        payload["description"] = description[:512]
    if data is not None:
        payload["data"] = data
    if data_file_url is not None:
        payload["data_file_url"] = data_file_url
    return payload
