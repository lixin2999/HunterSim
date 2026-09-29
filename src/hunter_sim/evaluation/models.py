"""L4 评估层数据模型（不可变 pydantic + 回放帧轻量结构）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hunter_sim.core.contracts import FloatArray


class _FrozenBase(BaseModel):
    """不可变结果模型基类。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class TrajectoryMetrics(_FrozenBase):
    """轨迹跟踪误差指标。"""

    ade: float = Field(ge=0, description="平均位移误差 ADE")
    fde: float = Field(ge=0, description="末位移误差 FDE")
    max_error: float = Field(ge=0, description="最大误差")


class ComfortMetrics(_FrozenBase):
    """驾驶舒适度指标。"""

    rms_accel: float = Field(ge=0, description="加速度均方根")
    max_accel: float = Field(ge=0, description="最大水平加速度模")
    max_jerk: float = Field(ge=0, description="最大加加速度模")
    comfort_score: float = Field(ge=0, le=1, description="舒适度得分 0-1")


class SafetyMetrics(_FrozenBase):
    """安全与合规指标。"""

    collision_count: int = Field(ge=0)
    lane_invasion_count: int = Field(ge=0)
    collision_rate: float = Field(ge=0, description="每秒碰撞次数")
    route_completion: float = Field(ge=0, le=1, description="路线完成率")


class CoverageMetrics(_FrozenBase):
    """传感器数据覆盖率指标。"""

    total_ticks: int = Field(ge=0)
    per_sensor: dict[str, float] = Field(default_factory=dict)
    overall: float = Field(ge=0, le=1, description="各传感器覆盖率的均值")


class EvaluationReport(_FrozenBase):
    """一次运行聚合的评估报告。"""

    run_id: str
    scenario_name: str
    generated_at: datetime | None = None
    trajectory: TrajectoryMetrics | None = None
    comfort: ComfortMetrics | None = None
    safety: SafetyMetrics | None = None
    coverage: CoverageMetrics | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class ReplayFrame(BaseModel):
    """回放时从磁盘读取的一帧（可变以简化构造，仅在本层内部传递）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    frame_id: int
    path: Path
    payload: Any


class TrajectorySeries(BaseModel):
    """图表渲染用的数值序列（供 Reporter 绘制）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    values: FloatArray
    label: str = "series"


__all__ = [
    "ComfortMetrics",
    "CoverageMetrics",
    "EvaluationReport",
    "ReplayFrame",
    "SafetyMetrics",
    "TrajectoryMetrics",
    "TrajectorySeries",
]
