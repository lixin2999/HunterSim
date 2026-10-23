"""解析 `carla.{sim_id}.vil_state` 消息（P0，方案 §5.3）。

平台将实车遥测转发为 `carla.{sim_id}.vil_state` Topic（替代旧 `telemetry_clean`），
消息格式对齐方案 §5.3 定义的 `vil_state` 结构。

本模块提供：
- ``parse_vil_state()`` — 将 vil_state payload 映射为 HunterSim 内部 ``TelemetryFrame``；
- 兼容旧 ``telemetry_clean`` 格式（由调用方通过格式检测区分）。

``vil_state`` 消息结构（方案 §5.3）::

    {
      "schema_version": "1.0.0",
      "sim_id": "sim-20261023-0001",
      "timestamp": 1724035200.123,
      "seq": 42,
      "type": "vil_state",
      "payload": {
        "pose":           {x, y, z, roll, pitch, heading},
        "twist":          {vx, vy, vz, wx, wy, wz},
        "chassis":        {throttle, brake, steer, gear, speed_m_s},
        "perception":     {objects: [...], timestamp},
        "planning":       {...},         # 可选
        "control":        {...},         # 可选
        "calibration":    {map_x, map_y, map_heading}  # 可选
      }
    }
"""

from __future__ import annotations

import time
from typing import Any, Optional

from hunter_sim.common.models import (
    DetectedObject,
    PerceptionResult,
    Transform,
    VehicleState,
)
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryFrame


def parse_vil_state(
    payload: dict[str, Any],
    vehicle_id: str = "",
    receive_time: Optional[float] = None,
) -> TelemetryFrame:
    """将 vil_state 消息的 payload 映射为 TelemetryFrame。

    Args:
        payload: vil_state 消息顶层或 payload 字段（含 pose/twist/chassis 等）。
        vehicle_id: 关联车辆 ID（用于填充 TelemetryFrame.vehicle_id）。
        receive_time: 本地接收时间（None 则使用当前时间）。

    Returns:
        TelemetryFrame 实例。

    Raises:
        KeyError: payload 缺少必要字段（pose 或 twist）。
        ValueError: 字段类型转换失败。
    """
    # 支持完整信封（含 payload 字段）或直接传入 payload 字典
    if "payload" in payload and "pose" not in payload:
        inner = payload["payload"]
        ts = float(payload.get("timestamp", time.time()))
    else:
        inner = payload
        ts = float(payload.get("timestamp", time.time()))

    rt = receive_time if receive_time is not None else time.time()

    pose: dict[str, Any] = inner.get("pose", {})
    twist: dict[str, Any] = inner.get("twist", {})
    chassis: dict[str, Any] = inner.get("chassis", {})

    transform = Transform(
        x=float(pose.get("x", 0.0)),
        y=float(pose.get("y", 0.0)),
        z=float(pose.get("z", 0.0)),
        roll=float(pose.get("roll", 0.0)),
        pitch=float(pose.get("pitch", 0.0)),
        yaw=float(pose.get("heading", pose.get("yaw", 0.0))),
    )

    vx = float(twist.get("vx", 0.0))
    vy = float(twist.get("vy", 0.0))
    vz = float(twist.get("vz", 0.0))
    wx = float(twist.get("wx", 0.0))
    wy = float(twist.get("wy", 0.0))
    wz = float(twist.get("wz", 0.0))

    speed_m_s = float(chassis.get("speed_m_s", (vx * vx + vy * vy) ** 0.5))

    vehicle_state = VehicleState(
        time_stamp=ts,
        transform=transform,
        velocity=(vx, vy, vz),
        acceleration=(
            float(chassis.get("ax", 0.0)),
            float(chassis.get("ay", 0.0)),
            float(chassis.get("az", 0.0)),
        ),
        angular_velocity=(wx, wy, wz),
        steering=float(chassis.get("steer", chassis.get("steering", 0.0))),
        throttle=float(chassis.get("throttle", 0.0)),
        brake=float(chassis.get("brake", 0.0)),
        gear=int(chassis.get("gear", 1)),
        vehicle_speed=speed_m_s,
    )

    # 感知结果（可选）
    perception: Optional[PerceptionResult] = None
    perc_data = inner.get("perception")
    if isinstance(perc_data, dict):
        objects: list[DetectedObject] = []
        for idx, obj in enumerate(perc_data.get("objects", [])):
            if not isinstance(obj, dict):
                continue
            obj_pose = obj.get("pose", obj)  # 支持嵌套 pose 或扁平字段
            objects.append(
                DetectedObject(
                    object_id=int(obj.get("id", idx)),
                    object_type=str(obj.get("type", "unknown")),
                    transform=Transform(
                        x=float(obj_pose.get("x", 0.0)),
                        y=float(obj_pose.get("y", 0.0)),
                        z=float(obj_pose.get("z", 0.0)),
                        yaw=float(obj_pose.get("heading", obj_pose.get("yaw", 0.0))),
                    ),
                    size=(
                        float(obj.get("length", 4.0)),
                        float(obj.get("width", 2.0)),
                        float(obj.get("height", 1.5)),
                    ),
                    velocity=(
                        float(obj.get("vx", 0.0)),
                        float(obj.get("vy", 0.0)),
                        0.0,
                    ),
                    confidence=float(obj.get("confidence", 1.0)),
                )
            )
        perception = PerceptionResult(
            time_stamp=float(perc_data.get("timestamp", ts)),
            objects=objects,
        )

    # 控制指令回读（可选）
    control_cmd: Optional[dict[str, float]] = None
    ctrl_data = inner.get("control")
    if isinstance(ctrl_data, dict):
        control_cmd = {
            "throttle": float(ctrl_data.get("throttle", 0.0)),
            "steer": float(ctrl_data.get("steer", 0.0)),
            "brake": float(ctrl_data.get("brake", 0.0)),
        }

    return TelemetryFrame(
        vehicle_id=vehicle_id,
        timestamp=ts,
        receive_time=rt,
        vehicle_state=vehicle_state,
        perception=perception,
        control_cmd=control_cmd,
    )
