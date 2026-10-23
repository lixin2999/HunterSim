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

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
        message: 可选错误/提示信息。
        updated_at: ISO8601 UTC 时间戳。
    """

    status: str
    progress_percent: float
    elapsed_time: float
    total_time: float
    actors_count: int
    collisions: int
    lane_invasions: int
    ego_state: dict[str, Any]
    message: Optional[str] = None
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.updated_at:
            self.updated_at = datetime.now(timezone.utc).isoformat()


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
) -> dict[str, Any]:
    """构建 sim_status 消息（SIL 模式 vehicle_id=None）。

    Args:
        sim_id: 仿真实例 ID。
        scene_id: 当前场景 ID。
        payload: SimStatusPayload 实例。
        seq: 消息序号（由 SimDataProducer 管理）。
        timestamp: Unix 时间戳（None 则使用当前时间）。

    Returns:
        符合方案 §5.4 格式的 dict 消息。
    """
    import time as _time  # noqa: PLC0415

    ts = timestamp if timestamp is not None else _time.time()
    return _envelope(
        sim_id=sim_id,
        vehicle_id=None,
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
    velocity: dict[str, Any],
    steering: float,
    acceleration: dict[str, Any],
) -> dict[str, Any]:
    """构建 sim_telemetry 消息（方案 §5.4）。

    Args:
        sim_id: 仿真实例 ID。
        vehicle_id: 数字孪生车 ID（SIL 模式可为 None）。
        scene_id: 当前场景 ID。
        seq: 单调递增序号。
        ts: Unix 时间戳（秒）。
        pose: 位姿 {x, y, z, roll, pitch, heading}。
        twist: 速度 {vx, vy, vz, wx, wy, wz}。
        velocity: 速度标量 {m_s, km_h}。
        steering: 方向盘角度（弧度）。
        acceleration: 加速度 {ax, ay, az}。

    Returns:
        符合方案 §5.4 sim_telemetry 格式的 dict 消息。
    """
    payload: dict[str, Any] = {
        "pose": pose,
        "twist": twist,
        "velocity": velocity,
        "steering": round(steering, 6),
        "acceleration": acceleration,
    }
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
) -> dict[str, Any]:
    """构建 sim_event 消息（方案 §5.4）。

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

    Returns:
        符合方案 §5.4 sim_event 格式的 dict 消息。
    """
    payload: dict[str, Any] = {
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

    Returns:
        符合方案 §5.4 sensor_file 格式的 dict 消息。
    """
    payload: dict[str, Any] = {
        "bucket": bucket,
        "object_key": object_key,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "sensor_type": sensor_type,
        "start_time": start_time,
        "end_time": end_time,
        "frame_count": frame_count,
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
