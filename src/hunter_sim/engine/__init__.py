"""HunterSim 仿真引擎层（ENG-001）。

封装 CARLA 0.9.16 Python API，提供地图管理、HUNTER SE 车辆模型、
坐标转换、天气控制等底层功能。禁止在此层包含业务逻辑。
"""

from hunter_sim.engine.carla_server_config import CarlaServerConfig, check_carla_installed
from hunter_sim.engine.check_environment import EnvironmentCheckResult, check_environment
from hunter_sim.engine.coordinate_converter import (
    CalibrationParams,
    CoordinateTransformer,
    MapHeightGetterProtocol,
)
from hunter_sim.engine.hunter_se_vehicle import (
    HunterSEParameters,
    HunterSEVehicleController,
    HunterSESILController,
    VehicleBlueprintGenerator,
)
from hunter_sim.engine.map_manager import (
    BUILTIN_MAPS,
    CarlaClientProtocol,
    CarlaWorldProtocol,
    MapInfo,
    MapManager,
)
from hunter_sim.engine.vehicle_blueprint_generator import (
    HUNTER_SE_SENSOR_MOUNTS,
    SensorMountPosition,
    get_sensor_mount,
    spawn_hunter_se,
)
from hunter_sim.engine.vehicle_controller import VehicleControllerFactory
from hunter_sim.engine.weather_manager import (
    PRESET_ENVIRONMENTS,
    WeatherManager,
    WeatherProfile,
    get_preset_profile,
)

__all__ = [
    # carla_server_config
    "CarlaServerConfig", "check_carla_installed",
    # check_environment
    "EnvironmentCheckResult", "check_environment",
    # coordinate_converter
    "CalibrationParams", "CoordinateTransformer", "MapHeightGetterProtocol",
    # hunter_se_vehicle
    "HunterSEParameters", "HunterSEVehicleController", "HunterSESILController",
    "VehicleBlueprintGenerator",
    # map_manager
    "BUILTIN_MAPS", "CarlaClientProtocol", "CarlaWorldProtocol", "MapInfo", "MapManager",
    # vehicle_blueprint_generator
    "HUNTER_SE_SENSOR_MOUNTS", "SensorMountPosition", "get_sensor_mount", "spawn_hunter_se",
    # vehicle_controller
    "VehicleControllerFactory",
    # weather_manager
    "PRESET_ENVIRONMENTS", "WeatherManager", "WeatherProfile", "get_preset_profile",
]
