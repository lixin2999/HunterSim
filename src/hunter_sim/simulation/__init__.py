"""L1 仿真核心层 (Simulation Core)。

模块：connection（连接管理）/ vehicle（车辆控制）/ scenario（场景管理）/
maps（地图系统）。对外暴露各层 Protocol 与实现类，供上层通过依赖注入使用。
"""

from __future__ import annotations

from hunter_sim.simulation.connection import CarlaConnectionManagerImpl
from hunter_sim.simulation.map_manager import MapManagerImpl
from hunter_sim.simulation.maps import (
    BUILTIN_MAP_REGISTRY,
    CARLA_COORDINATE_FRAME,
    BuiltInMap,
    CoordinateFrame,
    MapCategory,
    MapInfo,
    MapSource,
    OpenDriveOptions,
    OpenDriveVersion,
)
from hunter_sim.simulation.models import (
    ControlMode,
    Location,
    Rotation,
    Transform,
    Vector3D,
    VehicleCommand,
    VehicleKinematicState,
)
from hunter_sim.simulation.protocols import (
    CarlaConnectionManager,
    MapManager,
    ScenarioManager,
    ScenarioState,
    VehicleController,
)
from hunter_sim.simulation.scenario import ScenarioManagerImpl
from hunter_sim.simulation.vehicle import VehicleControllerImpl

__all__ = [
    "BUILTIN_MAP_REGISTRY",
    "CARLA_COORDINATE_FRAME",
    "BuiltInMap",
    "CarlaConnectionManager",
    "CarlaConnectionManagerImpl",
    "ControlMode",
    "CoordinateFrame",
    "Location",
    "MapCategory",
    "MapInfo",
    "MapManager",
    "MapManagerImpl",
    "MapSource",
    "OpenDriveOptions",
    "OpenDriveVersion",
    "Rotation",
    "ScenarioManager",
    "ScenarioManagerImpl",
    "ScenarioState",
    "Transform",
    "Vector3D",
    "VehicleCommand",
    "VehicleController",
    "VehicleControllerImpl",
    "VehicleKinematicState",
]
