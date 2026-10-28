"""CARLA → 平台消息构造函数（P0，方案 §5.4）。

6 类消息共享统一信封格式，所有 payload 字段对齐方案文档：

- ``sim_status``     — 仿真实例运行状态（1Hz + 状态跃迁）
- ``sim_telemetry``  — 数字孪生车辆遥测（10Hz）
- ``sim_event``      — 场景事件（碰撞/急刹等）
- ``sim_metrics``    — 评估指标（场景终止后）
- ``sensor_file``    — bag 文件上传完成通知
- ``artifact``       — 产物文件（eval.json/events.jsonl 等）

信封格式::

    {
      "schema_version": "1.0.0",
      "sim_id": "sim-20261023-0001",
      "vehicle_id": "HUNTER-001",   # SIL 模式为 None
      "scene_id": "uuid",
      "timestamp": 1724035200.123,
      "seq": 42,
      "type": "sim_status",
      "payload": { ... }
    }
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Optional


SCHEMA_VERSION = "1.0.0"


# ─── sim_status payload ────────────────────────────────────────────────────────


@dataclass
class SimStatusPayload:
    """方案 §5.4 sim_status.payload 字段定义。

    Attributes:
        status: CARLA 原始状态（大写，如 RUNNING/PAUSED/COMPLETED）。
        progress_percent: 场景进度 (0.0 ~ 100.0)。
        elapsed_time: 已运行时间（秒）。
        total_time: 场景设定总时长（秒）。
        actors_count: 世界中 Actor 数量。
        collisions: 碰撞计数。
        lane_invasions: 车道偏离计数。
        ego_state: 自车状态字典 {x, y, heading, velocity, steering}。
        events_triggered: 已触发事件总数（方案 §5.4 sim_status 字段）。
        message: 可选错误/提示信息。
        updated_at: Unix epoch 秒（契约要求 number）。
    """

    status: str
    progress_percent: float
    elapsed_time: float
    total_time: float
    actors_count: int
    collisions: int
    lane_invasions: int
    ego_state: dict[str, Any]
    events_triggered: int = 0
    message: Optional[str] = None
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.updated_at:
            self.updated_at = time.time()


# ─── 信封辅助 ──────────────────────────────────────────────────────────────────


def _envelope(
    sim_id: str,
    vehicle_id: Optional[str],
    scene_id: str,
    timestamp: float,
    seq: int,
    msg_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """构建统一消息信封（方案 §5.4 公共字段）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "sim_id": sim_id,
        "vehicle_id": vehicle_id,
        "scene_id": scene_id,
        "timestamp": timestamp,
        "seq": seq,
        "type": msg_type,
        "payload": payload,
    }


# ─── 公开构造函数 ──────────────────────────────────────────────────────────────


def build_sim_status(
    sim_id: str,
    scene_id: str,
    payload: SimStatusPayload,
    *,
    seq: int = 0,
    timestamp: Optional[float] = None,
    vehicle_id: Optional[str] = None,
) -> dict[str, Any]:
    """构建 sim_status 消息。

    方案 §5.4 要求 SIL 模式 ``vehicle_id`` 可为 null，VIL 模式必须携带
    孪生车 ID（平台据此建立车辆↔仿真会话绑定）。

    Args:
        sim_id: 仿真实例 ID。
        scene_id: 当前场景 ID。
        payload: SimStatusPayload 实例。
        seq: 消息序号（由 SimDataProducer 管理）。
        timestamp: Unix 时间戳（None 则使用当前时间）。
        vehicle_id: 孪生车 ID（SIL 模式保持 None）。

    Returns:
        符合方案 §5.4 格式的 dict 消息。
    """
    import time as _time  # noqa: PLC0415

    ts = timestamp if timestamp is not None else _time.time()
    return _envelope(
        sim_id=sim_id,
        vehicle_id=vehicle_id or None,
        scene_id=scene_id,
        timestamp=ts,
        seq=seq,
        msg_type="sim_status",
        payload=asdict(payload),
    )


def build_sim_telemetry(
    sim_id: str,
    vehicle_id: Optional[str],
    scene_id: str,
    seq: int,
    ts: float,
    pose: dict[str, Any],
    twist: dict[str, Any],
    velocity: float,
    steering: float,
    acceleration: float,
    sim_time: Optional[float] = None,
) -> dict[str, Any]:
    """构建 sim_telemetry 消息（方案 §5.4）。

    Args:
        sim_id: 仿真实例 ID。
        vehicle_id: 数字孪生车 ID（SIL 模式可为 None）。
        scene_id: 当前场景 ID。
        seq: 单调递增序号。
        ts: Unix 时间戳（秒）。
        pose: 位姿 {x, y, z, roll, pitch, yaw}。
        twist: 速度向量 {vx, vy, vz, wx, wy, wz}。
        velocity: 速度标量（m/s，契约要求 number）。
        steering: 方向盘角度（弧度）。
        acceleration: 加速度标量（m/s²，契约要求 number）。
        sim_time: 仿真时间轴时刻（秒，可选）。

    Returns:
        符合方案 §5.4 sim_telemetry 格式的 dict 消息。
    """
    payload: dict[str, Any] = {
        "pose": pose,
        "twist": twist,
        "velocity": round(float(velocity), 6),
        # 方案 §5.4 规范字段名；steering 保留为向后兼容别名
        "steering_angle": round(steering, 6),
        "steering": round(steering, 6),
        "acceleration": round(float(acceleration), 6),
    }
    if sim_time is not None:
        payload["sim_time"] = max(0.0, float(sim_time))
    return _envelope(
        sim_id=sim_id,
        vehicle_id=vehicle_id,
        scene_id=scene_id,
        timestamp=ts,
        seq=seq,
        msg_type="sim_telemetry",
        payload=payload,
    )


def build_sim_event(
    sim_id: str,
    vehicle_id: Optional[str],
    scene_id: str,
    seq: int,
    ts: float,
    event_type: str,
    event_level: str,
    sim_time: float,
    description: str,
    data: Optional[dict[str, Any]] = None,
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    """构建 sim_event 消息（方案 §5.4）。

    方案 §7.3.1 规定 ``sim_event`` 以 ``event_id`` 做幂等键（其余类型用
    ``(sim_id, seq)``），故 payload 必须携带稳定的 ``event_id``。

    Args:
        sim_id: 仿真实例 ID。
        vehicle_id: 车辆 ID。
        scene_id: 场景 ID。
        seq: 消息序号。
        ts: Unix 时间戳（秒）。
        event_type: 事件类型（collision/lane_invasion 等）。
        event_level: 事件级别（info/warning/critical）。
        sim_time: 仿真时间轴时刻（秒）。
        description: 事件描述。
        data: 附加数据字典。
        event_id: 事件幂等键（None 则生成 UUID4 hex）。

    Returns:
        符合方案 §5.4 sim_event 格式的 dict 消息。
    """
    payload: dict[str, Any] = {
        "event_id": event_id or uuid.uuid4().hex,
        "event_type": event_type,
        "event_level": event_level,
        "sim_time": sim_time,
        "description": description,
        "data": data or {},
    }
    return _envelope(
        sim_id=sim_id,
        vehicle_id=vehicle_id,
        scene_id=scene_id,
        timestamp=ts,
        seq=seq,
        msg_type="sim_event",
        payload=payload,
    )


def build_sim_metrics(
    sim_id: str,
    vehicle_id: Optional[str],
    scene_id: str,
    seq: int,
    ts: float,
    grade: str,
    passed: bool,
    safety: dict[str, Any],
    efficiency: dict[str, Any],
    comfort: dict[str, Any],
    task: dict[str, Any],
    success_criteria_result: dict[str, Any],
    takeover_required: bool = False,
) -> dict[str, Any]:
    """构建 sim_metrics 消息（方案 §5.4）。

    Args:
        sim_id: 仿真实例 ID。
        vehicle_id: 车辆 ID。
        scene_id: 场景 ID。
        seq: 消息序号。
        ts: Unix 时间戳（秒）。
        grade: 评估等级（S/A/B/C/D）。
        passed: 是否通过。
        safety: 安全指标字典。
        efficiency: 效率指标字典。
        comfort: 舒适性指标字典。
        task: 任务完成度字典（含 takeover_required）。
        success_criteria_result: 成功准则明细。
        takeover_required: 是否发生人工接管。

    Returns:
        符合方案 §5.4 sim_metrics 格式的 dict 消息。
    """
    payload: dict[str, Any] = {
        "grade": grade,
        "passed": passed,
        "safety": safety,
        "efficiency": efficiency,
        "comfort": comfort,
        "task": {**task, "takeover_required": takeover_required},
        "success_criteria_result": success_criteria_result,
    }
    return _envelope(
        sim_id=sim_id,
        vehicle_id=vehicle_id,
        scene_id=scene_id,
        timestamp=ts,
        seq=seq,
        msg_type="sim_metrics",
        payload=payload,
    )


def build_sensor_file(
    sim_id: str,
    seq: int,
    ts: float,
    bucket: str,
    object_key: str,
    size_bytes: int,
    sha256: str,
    sensor_type: str,
    start_time: float,
    end_time: float,
    frame_count: int,
    vehicle_id: Optional[str] = None,
    scene_id: str = "",
    md5: str = "",
    file_format: str = "",
    topics: Optional[list[str]] = None,
) -> dict[str, Any]:
    """构建 sensor_file 消息（方案 §5.4）。

    Args:
        sim_id: 仿真实例 ID。
        seq: 消息序号。
        ts: Unix 时间戳（秒）。
        bucket: MinIO Bucket 名称。
        object_key: 对象存储 Key。
        size_bytes: 文件大小（字节）。
        sha256: 文件 SHA-256 十六进制字符串。
        sensor_type: 传感器类型（rosbag/camera_raw 等）。
        start_time: bag 起始仿真时间（秒）。
        end_time: bag 结束仿真时间（秒）。
        frame_count: bag 内帧数。
        vehicle_id: 车辆 ID（可选）。
        scene_id: 场景 ID（可选）。
        md5: 文件 MD5（方案 §5.4 要求；未知时为空串）。
        file_format: 文件格式（rosbag2/mcap 等；未知时为空串）。
        topics: bag 内包含的 Topic 列表（未知时为 None）。

    Returns:
        符合方案 §5.4 sensor_file 格式的 dict 消息。
    """
    payload: dict[str, Any] = {
        "bucket": bucket,
        "object_key": object_key,
        "size_bytes": size_bytes,
        "md5": md5,
        "sha256": sha256,
        "sensor_type": sensor_type,
        "format": file_format,
        "start_time": start_time,
        "end_time": end_time,
        "frame_count": frame_count,
        "topics": list(topics or []),
    }
    return _envelope(
        sim_id=sim_id,
        vehicle_id=vehicle_id,
        scene_id=scene_id,
        timestamp=ts,
        seq=seq,
        msg_type="sensor_file",
        payload=payload,
    )


def build_artifact(
    sim_id: str,
    seq: int,
    ts: float,
    name: str,
    bucket: str,
    object_key: str,
    size_bytes: int,
    sha256: str,
    media_type: str,
    vehicle_id: Optional[str] = None,
    scene_id: str = "",
) -> dict[str, Any]:
    """构建 artifact 消息（方案 §5.4）。

    Args:
        sim_id: 仿真实例 ID。
        seq: 消息序号。
        ts: Unix 时间戳（秒）。
        name: 产物名称（如 eval.json / events.jsonl / run.log）。
        bucket: MinIO Bucket。
        object_key: 对象 Key。
        size_bytes: 字节大小。
        sha256: SHA-256 字符串。
        media_type: MIME 类型（application/json / text/plain 等）。
        vehicle_id: 车辆 ID（可选）。
        scene_id: 场景 ID（可选）。

    Returns:
        符合方案 §5.4 artifact 格式的 dict 消息。
    """
    payload: dict[str, Any] = {
        "name": name,
        "bucket": bucket,
        "object_key": object_key,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "media_type": media_type,
    }
    return _envelope(
        sim_id=sim_id,
        vehicle_id=vehicle_id,
        scene_id=scene_id,
        timestamp=ts,
        seq=seq,
        msg_type="artifact",
        payload=payload,
    )
