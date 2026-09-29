"""L4 评估与可视化层 (Evaluation & Visualization)。

模块：metrics（指标引擎）/ reporter（报告与图表）/ replay（数据回放）。
对外暴露各 Protocol 与实现类，供 L5 管线调用；依赖方向仅向下依赖 core。
"""

from __future__ import annotations

from hunter_sim.evaluation.metrics import MetricsEngineImpl
from hunter_sim.evaluation.models import (
    ComfortMetrics,
    CoverageMetrics,
    EvaluationReport,
    ReplayFrame,
    SafetyMetrics,
    TrajectoryMetrics,
    TrajectorySeries,
)
from hunter_sim.evaluation.protocols import MetricsEngine, Replayer, Reporter
from hunter_sim.evaluation.replay import ReplayerImpl
from hunter_sim.evaluation.reporter import ReporterImpl

__all__ = [
    "ComfortMetrics",
    "CoverageMetrics",
    "EvaluationReport",
    "MetricsEngine",
    "MetricsEngineImpl",
    "ReplayFrame",
    "Replayer",
    "ReplayerImpl",
    "Reporter",
    "ReporterImpl",
    "SafetyMetrics",
    "TrajectoryMetrics",
    "TrajectorySeries",
]
