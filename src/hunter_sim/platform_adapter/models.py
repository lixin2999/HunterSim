"""适配器数据模型（请求体、实例记录、状态映射）。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

# 平台状态枚举（HunterCore SimulationStatus）
PLATFORM_PENDING = "pending"
PLATFORM_RUNNING = "running"
PLATFORM_SUCCEEDED = "succeeded"
PLATFORM_FAILED = "failed"
PLATFORM_CANCELED = "canceled"

PLATFORM_STATUSES = frozenset(
    {PLATFORM_PENDING, PLATFORM_RUNNING, PLATFORM_SUCCEEDED, PLATFORM_FAILED, PLATFORM_CANCELED}
)
TERMINAL_STATUSES = frozenset({PLATFORM_SUCCEEDED, PLATFORM_FAILED, PLATFORM_CANCELED})
ACTIVE_STATUSES = frozenset({PLATFORM_PENDING, PLATFORM_RUNNING})

# CARLA 原始状态 → 平台状态（未知值按 failed 处理）
_CARLA_TO_PLATFORM: dict[str, str] = {
    "CREATED": PLATFORM_PENDING,
    "LOADING": PLATFORM_PENDING,
    "READY": PLATFORM_PENDING,
    "PENDING": PLATFORM_PENDING,
    "RUNNING": PLATFORM_RUNNING,
    "PAUSED": PLATFORM_RUNNING,
    "COMPLETED": PLATFORM_SUCCEEDED,
    "SUCCEEDED": PLATFORM_SUCCEEDED,
    "FAILED": PLATFORM_FAILED,
    "CANCELED": PLATFORM_CANCELED,
    "CANCELLED": PLATFORM_CANCELED,
}


def to_platform_status(raw: Optional[str]) -> str:
    """把 CARLA/内部状态映射为平台状态；未知值返回 ``failed``。"""
    if not raw:
        return PLATFORM_PENDING
    return _CARLA_TO_PLATFORM.get(raw.strip().upper(), PLATFORM_FAILED)


def is_terminal(status: str) -> bool:
    """平台状态是否为终态。"""
    return status in TERMINAL_STATUSES


class CreateInstanceRequest(BaseModel):
    """``POST /api/v1/instances`` 请求体（平台已合并 param_overrides）。"""

    model_config = ConfigDict(extra="ignore")

    scene_id: str = Field(min_length=1)
    scene_name: str = ""
    scene_config: dict[str, Any] = Field(default_factory=dict)
    sim_id: Optional[str] = None
    simulation_id: Optional[str] = Field(
        default=None, description="sim_id 的兼容别名（平台旧字段）"
    )
    mode: Optional[str] = None
    vehicle_id: Optional[str] = None
    vil: Optional[dict[str, Any]] = None
    replay_config: Optional[dict[str, Any]] = None
    kafka: Optional[dict[str, Any]] = None
    quality: Optional[str] = None

    @property
    def resolved_sim_id(self) -> str:
        """平台生成的 sim_id（两字段任一）。"""
        return (self.sim_id or self.simulation_id or "").strip()


class SubmitScenarioRequest(BaseModel):
    """``POST /api/v1/instances/{id}/scenario`` 请求体。

    ``scene_config`` 已由平台合并 ``param_overrides``，适配器不得再次应用
    ``param_overrides``（尤其不能改 map）。
    """

    model_config = ConfigDict(extra="ignore")

    sim_id: Optional[str] = None
    scene_id: Optional[str] = None
    scene_config: dict[str, Any] = Field(default_factory=dict)
    param_overrides: dict[str, Any] = Field(default_factory=dict)
    mode: Optional[str] = None
    vehicle_id: Optional[str] = None
    kafka: Optional[dict[str, Any]] = None


class ControlResponse(BaseModel):
    """pause/resume/stop 统一响应。"""

    sim_instance_id: str
    instance_id: str
    status: str
    carla_status: str
    message: Optional[str] = None


@dataclass
class InstanceRecord:
    """适配器实例记录（SQLite 行对象）。

    Attributes:
        instance_id: 适配器生成的实例 ID（= 平台 sim_instance_id）。
        sim_id: 平台生成的仿真会话 ID（必须原样回显）。
        scene_id: 场景 ID。
        scene_name: 场景名称。
        mode: sil/vil/replay。
        vehicle_id: 孪生车 ID（SIL 为空）。
        status: 平台状态（pending/running/succeeded/failed/canceled）。
        carla_status: CARLA 原始状态（大写）。
        message: 错误/提示信息。
        progress_percent: 进度百分比。
        current_time_s: 已运行时长（秒）。
        total_time_s: 场景总时长（秒）。
        actors_count: Actor 数量。
        collisions: 碰撞次数。
        lane_invasions: 车道偏离次数。
        ego_state: 自车状态。
        scene_config: 平台下发的场景配置（已合并）。
        vil_config: VIL 配置（含 calibration）。
        result: 终态结果（evaluation/artifacts 等）。
        recording_path: 传感器录制文件本地路径（未录制为空串）。
        recording_bytes: 传感器录制文件大小（字节）。
        revision: 场景下发版本号（用于队列合并去重）。
        created_at/updated_at/started_at/finished_at: Unix 时间戳。
        watchdog_deadline: 看门狗绝对截止时间戳（0 表示未启动）。
    """

    instance_id: str
    sim_id: str
    scene_id: str
    scene_name: str = ""
    mode: str = "sil"
    vehicle_id: str = ""
    status: str = PLATFORM_PENDING
    carla_status: str = "CREATED"
    message: Optional[str] = None
    progress_percent: float = 0.0
    current_time_s: float = 0.0
    total_time_s: float = 0.0
    actors_count: int = 0
    collisions: int = 0
    lane_invasions: int = 0
    ego_state: dict[str, Any] = field(default_factory=dict)
    scene_config: dict[str, Any] = field(default_factory=dict)
    vil_config: Optional[dict[str, Any]] = None
    result: Optional[dict[str, Any]] = None
    recording_path: str = ""
    recording_bytes: int = 0
    revision: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    watchdog_deadline: float = 0.0

    # ── 序列化 ─────────────────────────────────────────────────────────────

    def to_view(self) -> dict[str, Any]:
        """实例查询视图（同时给出三种实例 ID 键名）。"""
        return {
            "sim_instance_id": self.instance_id,
            "instance_id": self.instance_id,
            "id": self.instance_id,
            "sim_id": self.sim_id,
            "scene_id": self.scene_id,
            "scene_name": self.scene_name,
            "mode": self.mode,
            "vehicle_id": self.vehicle_id or None,
            "status": self.status,
            "carla_status": self.carla_status,
            "message": self.message,
            "progress_percent": round(self.progress_percent, 3),
            "progress": round(self.progress_percent / 100.0, 4),
            "current_time_s": round(self.current_time_s, 3),
            "elapsed_s": round(self.current_time_s, 3),
            "total_time_s": round(self.total_time_s, 3),
            "duration": round(self.total_time_s, 3),
            "actors_count": self.actors_count,
            "collisions": self.collisions,
            "lane_invasions": self.lane_invasions,
            "ego_state": dict(self.ego_state),
            "recording_path": self.recording_path or None,
            "recording_bytes": self.recording_bytes,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }

    def to_result_view(self) -> dict[str, Any]:
        """结果查询视图（平台 ``get_result`` 直接读取这些字段）。"""
        result = dict(self.result or {})
        view = self.to_view()
        view.update(
            {
                "success": bool(result.get("success", self.status == PLATFORM_SUCCEEDED)),
                "success_criteria_result": result.get("success_criteria_result", {}),
                "evaluation": result.get("evaluation", {}),
                "artifacts": result.get("artifacts", []),
                "failure_reason": self.message if self.status == PLATFORM_FAILED else None,
            }
        )
        return view


def _iso(value: Optional[float]) -> Optional[str]:
    """Unix 时间戳 → ISO8601 UTC（平台按 ISO 解析 started_at/finished_at/updated_at）。"""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


__all__ = [
    "ACTIVE_STATUSES",
    "ControlResponse",
    "CreateInstanceRequest",
    "InstanceRecord",
    "PLATFORM_STATUSES",
    "SubmitScenarioRequest",
    "TERMINAL_STATUSES",
    "is_terminal",
    "to_platform_status",
]
