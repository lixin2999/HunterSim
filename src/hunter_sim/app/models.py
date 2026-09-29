"""L5 应用调度层数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hunter_sim.core.contracts import RunMetadata
from hunter_sim.evaluation.models import EvaluationReport


class RunResult(BaseModel):
    """一次采集运行的聚合结果。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    scenario_name: str
    ticks: int = Field(ge=0, description="完成的仿真 tick 数")
    written_frames: int = Field(ge=0, description="落盘帧总数")
    sensor_counts: dict[str, int] = Field(default_factory=dict)
    metadata: RunMetadata
    report: EvaluationReport | None = None


__all__ = ["RunResult"]
