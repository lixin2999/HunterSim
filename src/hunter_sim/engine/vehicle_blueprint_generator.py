"""HUNTER SE 车辆蓝图生成器（PROMPT-ENG-001-C）。

独立于 hunter_se_vehicle.py，专注于蓝图对象创建和传感器安装位置计算。
供场景运行服务和传感器工厂调用。
"""

from __future__ import annotations

from typing import Any, Optional

from hunter_sim.common.models import HunterSESpec
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.hunter_se_vehicle import (
    BlueprintLibraryProtocol,
    HunterSEParameters,
    VehicleBlueprintGenerator,
)

logger = get_logger(__name__)

# 支持的车辆蓝图列表（供 /vehicles API 查询）
SUPPORTED_VEHICLES: list[dict[str, Any]] = [
    {"blueprint_id": "vehicle.hunter_se", "display_name": "HUNTER SE (自动驾驶底盘)", "num_wheels": 4},
    {"blueprint_id": "vehicle.tesla.model3", "display_name": "Tesla Model 3", "num_wheels": 4},
    {"blueprint_id": "vehicle.chevrolet.impala", "display_name": "Chevrolet Impala", "num_wheels": 4},
    {"blueprint_id": "vehicle.audi.a2", "display_name": "Audi A2", "num_wheels": 4},
    {"blueprint_id": "vehicle.bmw.grandturismo", "display_name": "BMW GranTurismo", "num_wheels": 4},
    {"blueprint_id": "vehicle.carlamotors.carlacola", "display_name": "Carla Motors CarLaCola (商用车)", "num_wheels": 6},
    {"blueprint_id": "vehicle.vespa.vespa", "display_name": "Vespa (摩托车)", "num_wheels": 2},
    {"blueprint_id": "vehicle.diamondback.century", "display_name": "Diamondback Century (自行车)", "num_wheels": 2},
]


class SensorMountPosition:
    """传感器在 HUNTER SE 车辆上的安装位置描述。

    Attributes:
        sensor_name: 传感器标识名。
        x: 纵向偏移（相对后轴中心，向前为正，米）。
        y: 横向偏移（向右为正，米）。
        z: 垂直偏移（向上为正，米）。
        yaw_deg: 传感器朝向（相对车头，度，逆时针为正）。
    """

    def __init__(
        self,
        sensor_name: str,
        x: float,
        y: float,
        z: float,
        yaw_deg: float = 0.0,
    ) -> None:
        self.sensor_name = sensor_name
        self.x = x
        self.y = y
        self.z = z
        self.yaw_deg = yaw_deg

    def to_carla_transform(self) -> Any:
        """转换为 carla.Transform 对象（相对坐标系）。"""
        import math
        import carla  # noqa: PLC0415
        return carla.Transform(
            carla.Location(x=self.x, y=self.y, z=self.z),
            carla.Rotation(yaw=self.yaw_deg),
        )


# HUNTER SE 标准传感器安装位置（基于实车设计）
HUNTER_SE_SENSOR_MOUNTS: list[SensorMountPosition] = [
    SensorMountPosition(
        sensor_name="lidar_top",
        x=HunterSESpec.LIDAR_OFFSET[0],   # 0.23 m（车顶中心，距后轴中心）
        y=HunterSESpec.LIDAR_OFFSET[1],   # 0.0 m
        z=HunterSESpec.LIDAR_OFFSET[2],   # 0.35 m（车顶高度）
        yaw_deg=0.0,
    ),
    SensorMountPosition(
        sensor_name="camera_front",
        x=HunterSESpec.CAMERA_OFFSET[0],  # 0.40 m
        y=HunterSESpec.CAMERA_OFFSET[1],  # 0.0 m
        z=HunterSESpec.CAMERA_OFFSET[2],  # 0.30 m
        yaw_deg=0.0,
    ),
    SensorMountPosition(
        sensor_name="camera_depth",
        x=HunterSESpec.CAMERA_OFFSET[0],
        y=HunterSESpec.CAMERA_OFFSET[1],
        z=HunterSESpec.CAMERA_OFFSET[2],
        yaw_deg=0.0,
    ),
    SensorMountPosition(
        sensor_name="imu_center",
        x=HunterSESpec.WHEELBASE_M * 0.5,  # 车辆几何中心
        y=0.0,
        z=HunterSESpec.CG_HEIGHT_M,
        yaw_deg=0.0,
    ),
    SensorMountPosition(
        sensor_name="gnss_roof",
        x=0.0,
        y=0.0,
        z=HunterSESpec.HEIGHT + 0.02,   # 车顶上方 2cm
        yaw_deg=0.0,
    ),
]


def get_sensor_mount(sensor_name: str) -> Optional[SensorMountPosition]:
    """按名称查询传感器安装位置。

    Args:
        sensor_name: 传感器标识名。

    Returns:
        SensorMountPosition 对象，未找到时返回 None。
    """
    for mount in HUNTER_SE_SENSOR_MOUNTS:
        if mount.sensor_name == sensor_name:
            return mount
    return None


def spawn_hunter_se(
    world: Any,
    blueprint_library: BlueprintLibraryProtocol,
    spawn_point: Any,
    params: Optional[HunterSEParameters] = None,
) -> Any:
    """在 CARLA 世界中生成 HUNTER SE 虚拟车辆。

    便捷函数，封装 VehicleBlueprintGenerator 和 world.spawn_actor 调用。

    Args:
        world: CARLA World 对象。
        blueprint_library: CARLA BlueprintLibrary。
        spawn_point: carla.Transform 生成位姿。
        params: 可选的自定义物理参数。

    Returns:
        生成的 CARLA Vehicle Actor。

    Raises:
        RuntimeError: 生成失败（位置被占用等）。
    """
    generator = VehicleBlueprintGenerator(blueprint_library, params)
    bp = generator.create_blueprint()
    vehicle = world.spawn_actor(bp, spawn_point)
    if vehicle is None:
        raise RuntimeError(f"Failed to spawn HUNTER SE at {spawn_point.location}")
    logger.info(f"HUNTER SE spawned at ({spawn_point.location.x:.1f}, {spawn_point.location.y:.1f})")
    return vehicle
