"""L5 应用与调度层 (Application & Orchestration)。

模块：scheduler（采集调度器）/ orchestrator（运行编排器）/ bootstrap（组合根装配 DI
容器）/ cli（命令行入口，惰性依赖 ``typer``）。

为避免在仅需轻量导入本层 Protocol/模型时牵连 CARLA 等重依赖，包 ``__init__`` 仅再导出
不含后端依赖的接口与实现；:func:`build_container` 与 :func:`collect` 请从子模块
``hunter_sim.app.bootstrap`` / ``hunter_sim.app.cli`` 显式导入。
"""

from __future__ import annotations

from hunter_sim.app.models import RunResult
from hunter_sim.app.orchestrator import RunOrchestratorImpl
from hunter_sim.app.protocols import Orchestrator, Scheduler
from hunter_sim.app.scheduler import CollectionSchedulerImpl

__all__ = [
    "CollectionSchedulerImpl",
    "Orchestrator",
    "RunOrchestratorImpl",
    "RunResult",
    "Scheduler",
]
