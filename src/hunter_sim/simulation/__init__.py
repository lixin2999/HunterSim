"""L1 仿真核心层 (Simulation Core)。

模块：connection（连接管理）/ vehicle（车辆控制）/ scenario（场景管理）。
对外暴露各层 Protocol 与实现类，供上层通过依赖注入使用。
"""

from __future__ import annotations

from hunter_sim.simulation.connection import CarlaConnectionManagerImpl
from hunter_sim.simulation.models import Location, Rotation, Transform, VehicleCommand
from hunter_sim.simulation.protocols import (
    CarlaConnectionManager,
    ScenarioManager,
    ScenarioState,
    VehicleController,
)
from hunter_sim.simulation.scenario import ScenarioManagerImpl
from hunter_sim.simulation.vehicle import VehicleControllerImpl

__all__ = [
    "CarlaConnectionManager",
    "CarlaConnectionManagerImpl",
    "Location",
    "Rotation",
    "ScenarioManager",
    "ScenarioManagerImpl",
    "ScenarioState",
    "Transform",
    "VehicleCommand",
    "VehicleController",
    "VehicleControllerImpl",
]
