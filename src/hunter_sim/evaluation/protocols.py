"""L4 评估与可视化层 Protocol 契约。

依赖方向：L4 仅依赖 ``core``（契约/配置/异常），不得反向依赖 L5。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from hunter_sim.core.contracts import VehicleState
from hunter_sim.evaluation.models import (
    ComfortMetrics,
    CoverageMetrics,
    EvaluationReport,
    ReplayFrame,
    SafetyMetrics,
    TrajectoryMetrics,
)


@runtime_checkable
class MetricsEngine(Protocol):
    """指标计算引擎 Protocol。"""

    def trajectory_error(
        self,
        predicted: NDArray[np.float64],
        reference: NDArray[np.float64],
    ) -> TrajectoryMetrics: ...

    def comfort(
        self,
        states: list[VehicleState],
        *,
        dt: float,
        a_max: float = 4.0,
        j_max: float = 10.0,
    ) -> ComfortMetrics: ...

    def safety(
        self,
        *,
        collision_count: int,
        lane_invasion_count: int,
        total_ticks: int,
        tick_rate: float,
        route_completion: float = 1.0,
    ) -> SafetyMetrics: ...

    def coverage(
        self,
        *,
        total_ticks: int,
        per_sensor_counts: dict[str, int],
    ) -> CoverageMetrics: ...


@runtime_checkable
class Reporter(Protocol):
    """评估报告汇总与持久化 Protocol。"""

    def build_report(
        self,
        *,
        run_id: str,
        scenario_name: str,
        trajectory: TrajectoryMetrics | None = None,
        comfort: ComfortMetrics | None = None,
        safety: SafetyMetrics | None = None,
        coverage: CoverageMetrics | None = None,
        extras: dict[str, Any] | None = None,
    ) -> EvaluationReport: ...

    async def write_json(self, report: EvaluationReport, path: Path) -> Path: ...

    def render_chart(self, payload: Any, path: Path) -> Path | None: ...


@runtime_checkable
class Replayer(Protocol):
    """数据回放 Protocol（读取 L2 writer 目录结构）。"""

    def load_metadata(self, run_root: Path) -> dict[str, Any]: ...

    def list_sensors(self, run_root: Path) -> list[str]: ...

    def iter_frames(self, run_root: Path, sensor_rel: str) -> AsyncIterator[ReplayFrame]: ...


__all__ = ["MetricsEngine", "Replayer", "Reporter"]
