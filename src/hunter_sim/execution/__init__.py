"""执行面（V2.4.0）：引擎库存、租约与场景运行器装配。

把「控制面」（REST/状态机）与「执行面」（CARLA 世界、tick 线程、引擎占用）
解耦：控制面只负责校验与状态流转，执行面由 ``ExecutionPlane`` 统一装配与释放。
"""

from hunter_sim.execution.engine_pool import (
    EngineEndpoint,
    EngineLease,
    EnginePool,
    EngineUnavailable,
    parse_engine_endpoints,
)
from hunter_sim.execution.plane import ExecutionPlane, ExecutionUnavailable

__all__ = [
    "EngineEndpoint",
    "EngineLease",
    "EnginePool",
    "EngineUnavailable",
    "ExecutionPlane",
    "ExecutionUnavailable",
    "parse_engine_endpoints",
]
