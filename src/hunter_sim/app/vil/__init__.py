"""VIL 虚实映射模块（模块 4，实车在环）。

依赖注入：全部子模块以 Protocol 契约对上层暴露，实现类以 ``Impl`` 结尾。
本包 ``__init__`` 仅再导出无 CARLA / Kafka 依赖的接口与纯计算实现，含后端
依赖的 :class:`KafkaTelemetryConsumerImpl` / :class:`CARLAVisualizerImpl` 请从
对应子模块显式导入。
"""

from __future__ import annotations

from hunter_sim.app.vil.coordinate_mapper import CoordinateMapperImpl
from hunter_sim.app.vil.models import (
    ChassisData,
    ImuData,
    LocalizationData,
    PerceptionData,
    PerceptionObject,
    VehicleTelemetry,
    VILCalibration,
    VILConfig,
)
from hunter_sim.app.vil.orchestrator import VILEngineImpl
from hunter_sim.app.vil.protocols import (
    CoordinateMapper,
    StateSynchronizer,
    SyncController,
    TelemetrySource,
    VILEngine,
    VILVisualizer,
)
from hunter_sim.app.vil.state_synchronizer import StateSynchronizerImpl
from hunter_sim.app.vil.sync_controller import SyncControllerImpl

__all__ = [
    "ChassisData",
    "CoordinateMapper",
    "CoordinateMapperImpl",
    "ImuData",
    "LocalizationData",
    "PerceptionData",
    "PerceptionObject",
    "StateSynchronizer",
    "StateSynchronizerImpl",
    "SyncController",
    "SyncControllerImpl",
    "TelemetrySource",
    "VILCalibration",
    "VILConfig",
    "VILEngine",
    "VILEngineImpl",
    "VILVisualizer",
    "VehicleTelemetry",
]
