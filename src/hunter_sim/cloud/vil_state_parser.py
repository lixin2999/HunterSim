"""解析 `carla.{sim_id}.vil_state` 消息（方案 §5.3）。

平台将实车遥测转发为 `carla.{sim_id}.vil_state` Topic（替代旧 `telemetry_clean`），
消息格式对齐方案 §5.3 定义的扁平 `vil_state` 结构（**无 payload 包裹**）::

    {
      "schema_version": "1.0.0",
      "sim_id": "sim-20261023-0001",
      "source_seq": 42,
      "source_timestamp": 1724035200.123,   # 实车采集时刻
      "forwarded_at": 1724035200.180,       # 平台转发时刻
      "pose":        {x, y, z, roll, pitch, yaw},
      "twist":       {linear: [vx, vy, vz], angular: [wx, wy, wz]},
      "chassis":     {velocity, steering_angle, control_mode, vehicle_state, fault_code, ...},
      "perception":  {detected_objects, fps, latency_ms, objects: [...]},
      "planning":    {current_behavior, trajectory},
      "control":     {target_velocity, target_steer, target_throttle, target_brake},
      "calibration": {calibration_id, map_x, map_y, map_heading, odom_x, odom_y, odom_heading}
    }

兼容性：同时接受旧 ``telemetry_clean`` 的扁平写法（``twist.vx`` /
``chassis.speed_m_s`` / ``chassis.steer``）以及被 ``payload`` 包裹的信封写法，
便于平台侧灰度切换。
"""

from __future__ import annotations

import time
import zlib
from typing import Any, Optional

from hunter_sim.common.models import (
    DetectedObject,
    PerceptionResult,
    Transform,
    VehicleState,
)
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryFrame

# 感知目标 ID 为字符串时的稳定映射空间（CRC32 正值）
_OBJECT_ID_MASK = 0x7FFFFFFF


def _as_float(value: Any, default: float = 0.0) -> float:
    """安全转 float（None/非法字符串回退默认值）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    """安全取 dict（非 dict 回退空字典）。"""
    return value if isinstance(value, dict) else {}


def _vector3(
    source: dict[str, Any],
    key: str,
    legacy_keys: tuple[str, str, str],
) -> tuple[float, float, float]:
    """读取三分量向量。

    优先方案 §5.3 的数组写法 ``source[key] = [c0, c1, c2]``（或 ``{x,y,z}``），
    其次回退旧扁平写法（``legacy_keys`` 分别对应三个分量）。

    Args:
        source: 含该向量的字典。
        key: 方案规范字段名（``linear`` / ``angular``）。
        legacy_keys: 旧扁平字段名三元组。

    Returns:
        ``(c0, c1, c2)`` 浮点三元组。
    """
    raw = source.get(key)
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        return (_as_float(raw[0]), _as_float(raw[1]), _as_float(raw[2]))
    if isinstance(raw, dict):
        return (
            _as_float(raw.get("x", raw.get(legacy_keys[0], 0.0))),
            _as_float(raw.get("y", raw.get(legacy_keys[1], 0.0))),
            _as_float(raw.get("z", raw.get(legacy_keys[2], 0.0))),
        )
    return (
        _as_float(source.get(legacy_keys[0])),
        _as_float(source.get(legacy_keys[1])),
        _as_float(source.get(legacy_keys[2])),
    )


def _as_object_id(raw: Any, index: int) -> int:
    """把感知目标 ID 归一化为 int。

    方案 §5.3 使用字符串 ID（如 ``"obj-1"``），而 ``DetectedObject.object_id``
    为 int。数字字符串直接转换，其他字符串用 CRC32 稳定映射（跨进程一致）。

    Args:
        raw: 原始 ID（int/float/str/None）。
        index: 该目标在列表中的下标（ID 缺失时回退）。

    Returns:
        非负 int 目标 ID。
    """
    if isinstance(raw, bool) or raw is None:
        return index
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    text = str(raw).strip()
    if not text:
        return index
    if text.lstrip("-").isdigit():
        return int(text)
    return zlib.crc32(text.encode("utf-8")) & _OBJECT_ID_MASK


def _calibration_of(inner: dict[str, Any]) -> Optional[dict[str, Any]]:
    """提取方案 §5.3 ``calibration`` 段（缺失或空字典返回 None）。"""
    raw = inner.get("calibration")
    if not isinstance(raw, dict) or not raw:
        return None
    return {
        "calibration_id": str(raw.get("calibration_id", "")),
        "map_x": _as_float(raw.get("map_x")),
        "map_y": _as_float(raw.get("map_y")),
        "map_heading": _as_float(raw.get("map_heading")),
        "odom_x": _as_float(raw.get("odom_x")),
        "odom_y": _as_float(raw.get("odom_y")),
        "odom_heading": _as_float(raw.get("odom_heading")),
    }


def parse_vil_state(
    payload: dict[str, Any],
    vehicle_id: str = "",
    receive_time: Optional[float] = None,
) -> TelemetryFrame:
    """将 vil_state 消息映射为 TelemetryFrame。

    Args:
        payload: vil_state 消息顶层（扁平格式），或含 ``payload`` 的信封格式。
        vehicle_id: 关联车辆 ID（用于填充 TelemetryFrame.vehicle_id）。
        receive_time: 本地接收时间（None 则使用当前时间）。

    Returns:
        TelemetryFrame 实例。

    Raises:
        KeyError: payload 非 dict、缺少 ``pose`` 或 ``twist`` 不是字典。
        ValueError: 字段类型转换失败（整数下标等不可转换场景）。
    """
    if not isinstance(payload, dict):
        raise KeyError("vil_state message must be a dict")

    envelope_ts: Any = None
    envelope_seq: Any = None
    inner: dict[str, Any] = payload
    # 信封写法的兼容路径（扁平格式不含 "payload" 键）
    if "payload" in payload and "pose" not in payload:
        raw_inner = payload.get("payload")
        if not isinstance(raw_inner, dict):
            raise KeyError("vil_state.payload must be a dict")
        inner = raw_inner
        envelope_ts = payload.get("timestamp")
        envelope_seq = payload.get("seq")

    pose_raw = inner.get("pose")
    if not isinstance(pose_raw, dict):
        raise KeyError("vil_state missing required object field 'pose'")

    twist_raw = inner.get("twist")
    if twist_raw is None:
        twist: dict[str, Any] = {}
    elif isinstance(twist_raw, dict):
        twist = twist_raw
    else:
        raise KeyError("vil_state field 'twist' must be an object")

    chassis = _as_dict(inner.get("chassis"))
    control = _as_dict(inner.get("control"))

    # 时间戳优先级：source_timestamp（实车采集）→ timestamp → 信封 timestamp
    raw_ts = inner.get("source_timestamp")
    if raw_ts is None:
        raw_ts = inner.get("timestamp")
    if raw_ts is None:
        raw_ts = envelope_ts
    ts = _as_float(raw_ts, time.time()) if raw_ts is not None else time.time()
    rt = receive_time if receive_time is not None else time.time()

    transform = Transform(
        x=_as_float(pose_raw.get("x")),
        y=_as_float(pose_raw.get("y")),
        z=_as_float(pose_raw.get("z")),
        roll=_as_float(pose_raw.get("roll")),
        pitch=_as_float(pose_raw.get("pitch")),
        yaw=_as_float(pose_raw.get("yaw", pose_raw.get("heading"))),
    )

    # 方案 §5.3：twist.linear / twist.angular 为 3 元数组
    vx, vy, vz = _vector3(twist, "linear", ("vx", "vy", "vz"))
    wx, wy, wz = _vector3(twist, "angular", ("wx", "wy", "wz"))

    speed_from_twist = (vx * vx + vy * vy + vz * vz) ** 0.5
    if "velocity" in chassis:
        speed_m_s = _as_float(chassis.get("velocity"), speed_from_twist)
    else:
        # 向后兼容旧 telemetry_clean 的 chassis.speed_m_s
        speed_m_s = _as_float(chassis.get("speed_m_s"), speed_from_twist)

    steering = _as_float(
        chassis.get("steering_angle", chassis.get("steer", chassis.get("steering")))
    )
    # 实际执行量优先取 chassis，control 段（期望值）仅作兜底
    throttle = _as_float(
        chassis.get("throttle", control.get("throttle", control.get("target_throttle")))
    )
    brake = _as_float(
        chassis.get("brake", control.get("brake", control.get("target_brake")))
    )

    vehicle_state = VehicleState(
        time_stamp=ts,
        transform=transform,
        velocity=(vx, vy, vz),
        acceleration=(
            _as_float(chassis.get("ax")),
            _as_float(chassis.get("ay")),
            _as_float(chassis.get("az")),
        ),
        angular_velocity=(wx, wy, wz),
        steering=steering,
        throttle=throttle,
        brake=brake,
        gear=int(_as_float(chassis.get("gear"), 1.0)),
        vehicle_speed=speed_m_s,
    )

    perception: Optional[PerceptionResult] = None
    perc_data = inner.get("perception")
    if isinstance(perc_data, dict):
        objects: list[DetectedObject] = []
        raw_objects = perc_data.get("objects", perc_data.get("detected_objects", []))
        if not isinstance(raw_objects, list):
            raw_objects = []
        for idx, obj in enumerate(raw_objects):
            if not isinstance(obj, dict):
                continue
            obj_pose = obj.get("pose", obj)  # 支持嵌套 pose 或扁平字段
            if not isinstance(obj_pose, dict):
                obj_pose = obj
            objects.append(
                DetectedObject(
                    object_id=_as_object_id(obj.get("object_id", obj.get("id")), idx),
                    object_type=str(obj.get("type", obj.get("object_type", "unknown"))),
                    transform=Transform(
                        x=_as_float(obj_pose.get("x")),
                        y=_as_float(obj_pose.get("y")),
                        z=_as_float(obj_pose.get("z")),
                        yaw=_as_float(obj_pose.get("yaw", obj_pose.get("heading"))),
                    ),
                    size=(
                        _as_float(obj.get("length"), 4.0),
                        _as_float(obj.get("width"), 2.0),
                        _as_float(obj.get("height"), 1.5),
                    ),
                    velocity=(
                        _as_float(obj.get("vx")),
                        _as_float(obj.get("vy")),
                        0.0,
                    ),
                    confidence=min(1.0, max(0.0, _as_float(obj.get("confidence"), 1.0))),
                )
            )
        perception = PerceptionResult(
            time_stamp=_as_float(perc_data.get("timestamp"), ts),
            objects=objects,
        )

    # 控制指令回读（方案 §5.3 control.target_* 段）
    control_cmd: Optional[dict[str, float]] = None
    if control:
        control_cmd = {
            "throttle": _as_float(
                control.get("target_throttle", control.get("throttle"))
            ),
            "steer": _as_float(control.get("target_steer", control.get("steer"))),
            "brake": _as_float(control.get("target_brake", control.get("brake"))),
        }

    raw_source_seq = inner.get("source_seq", envelope_seq)
    source_seq = int(_as_float(raw_source_seq, 0.0))

    return TelemetryFrame(
        vehicle_id=vehicle_id,
        timestamp=ts,
        receive_time=rt,
        vehicle_state=vehicle_state,
        perception=perception,
        control_cmd=control_cmd,
        calibration=_calibration_of(inner),
        source_seq=source_seq,
        forwarded_at=(
            _as_float(inner.get("forwarded_at"), 0.0)
            if inner.get("forwarded_at") is not None
            else None
        ),
    )
