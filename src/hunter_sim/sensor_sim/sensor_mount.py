"""传感器安装位置管理（PROMPT-ENG-004-A）。

管理传感器在 HUNTER SE 车辆上的安装位置和姿态（carla.Transform）。
支持按名称查询和批量挂载/卸载。
"""

from __future__ import annotations

from typing import Any, Optional

from hunter_sim.common.exceptions import SensorSimulationError
from hunter_sim.common.models import HunterSESpec, SensorType
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


class SensorMountSpec:
    """单个传感器安装规格。

    Attributes:
        mount_id: 安装位置标识。
        sensor_type: 传感器类型。
        x: 纵向偏移（米，向前为正，相对后轴中心）。
        y: 横向偏移（米，向右为正）。
        z: 垂直偏移（米，向上为正）。
        pitch_deg: 传感器俯仰角（度）。
        yaw_deg: 传感器偏航角（度）。
        roll_deg: 传感器横滚角（度）。
    """

    def __init__(
        self,
        mount_id: str,
        sensor_type: SensorType,
        x: float,
        y: float,
        z: float,
        pitch_deg: float = 0.0,
        yaw_deg: float = 0.0,
        roll_deg: float = 0.0,
    ) -> None:
        self.mount_id = mount_id
        self.sensor_type = sensor_type
        self.x = x
        self.y = y
        self.z = z
        self.pitch_deg = pitch_deg
        self.yaw_deg = yaw_deg
        self.roll_deg = roll_deg

    def to_carla_transform(self) -> Any:
        """转换为 carla.Transform（相对坐标系）。"""
        import carla  # noqa: PLC0415
        return carla.Transform(
            carla.Location(x=self.x, y=self.y, z=self.z),
            carla.Rotation(pitch=self.pitch_deg, yaw=self.yaw_deg, roll=self.roll_deg),
        )

# HUNTER SE 标准传感器安装位置


_HUNTER_SE_MOUNTS: list[SensorMountSpec] = [
    SensorMountSpec(
        mount_id="lidar_top",
        sensor_type=SensorType.LIDAR,
        x=HunterSESpec.LIDAR_OFFSET[0],   # 0.23
        y=HunterSESpec.LIDAR_OFFSET[1],   # 0.0
        z=HunterSESpec.LIDAR_OFFSET[2],   # 0.35
    ),
    SensorMountSpec(
        mount_id="camera_front_rgb",
        sensor_type=SensorType.RGB_CAMERA,
        x=HunterSESpec.CAMERA_OFFSET[0],  # 0.40
        y=HunterSESpec.CAMERA_OFFSET[1],  # 0.0
        z=HunterSESpec.CAMERA_OFFSET[2],  # 0.30
    ),
    SensorMountSpec(
        mount_id="camera_front_depth",
        sensor_type=SensorType.DEPTH_CAMERA,
        x=HunterSESpec.CAMERA_OFFSET[0],
        y=HunterSESpec.CAMERA_OFFSET[1],
        z=HunterSESpec.CAMERA_OFFSET[2],
    ),
    SensorMountSpec(
        mount_id="imu_cg",
        sensor_type=SensorType.IMU,
        x=HunterSESpec.WHEELBASE_M * 0.5,
        y=0.0,
        z=HunterSESpec.CG_HEIGHT_M,
    ),
    SensorMountSpec(
        mount_id="gnss_roof",
        sensor_type=SensorType.GNSS,
        x=0.0,
        y=0.0,
        z=HunterSESpec.HEIGHT + 0.02,
    ),
    SensorMountSpec(
        mount_id="collision_front",
        sensor_type=SensorType.COLLISION,
        x=HunterSESpec.LENGTH * 0.5,
        y=0.0,
        z=0.1,
    ),
    SensorMountSpec(
        mount_id="lane_invasion_center",
        sensor_type=SensorType.LANE_INVASION,
        x=0.0,
        y=0.0,
        z=0.1,
    ),
    SensorMountSpec(
        # 设计文档 §6.2.5 障碍物传感器：前向探测，与碰撞传感器同位安装
        mount_id="obstacle_front",
        sensor_type=SensorType.OBSTACLE,
        x=HunterSESpec.LENGTH * 0.5,
        y=0.0,
        z=0.1,
    ),
]


class SensorMountManager:
    """传感器安装位置管理器。

    提供按 SensorType 查询安装位置、批量挂载/卸载传感器到车辆的能力。

    Args:
        vehicle_actor: CARLA Vehicle Actor 对象。
        custom_mounts: 可选的自定义安装位置列表（覆盖默认配置）。
    """

    def __init__(
        self,
        vehicle_actor: Any,
        custom_mounts: Optional[list[SensorMountSpec]] = None,
    ) -> None:
        self._vehicle = vehicle_actor
        self._mounts: list[SensorMountSpec] = custom_mounts or list(_HUNTER_SE_MOUNTS)
        self._attached: dict[str, Any] = {}  # mount_id -> sensor actor

    def get_mount(self, sensor_type: SensorType) -> Optional[SensorMountSpec]:
        """按传感器类型查找第一个安装位置。"""
        for m in self._mounts:
            if m.sensor_type == sensor_type:
                return m
        return None

    def attach_sensor(
        self,
        sensor_type: SensorType,
        blueprint: Any,
        world: Any,
    ) -> Any:
        """在车辆上挂载指定类型的传感器。

        Args:
            sensor_type: 传感器类型。
            blueprint: 已配置的 ActorBlueprint。
            world: CARLA World 对象。

        Returns:
            生成的传感器 Actor。

        Raises:
            SensorSimulationError: 找不到安装位置或生成失败。
        """
        mount = self.get_mount(sensor_type)
        if mount is None:
            raise SensorSimulationError(
                sensor_type.value, "unknown", f"No mount position for {sensor_type.value}"
            )

        try:
            tf = mount.to_carla_transform()
            sensor_actor = world.spawn_actor(blueprint, tf, attach_to=self._vehicle)
            self._attached[mount.mount_id] = sensor_actor
            logger.info(f"Sensor '{sensor_type.value}' attached at mount '{mount.mount_id}'")
            return sensor_actor
        except Exception as exc:
            raise SensorSimulationError(
                sensor_type.value, mount.mount_id, str(exc)
            ) from exc

    def detach_sensor(self, sensor_type: SensorType) -> None:
        """卸载指定类型的传感器（销毁 Actor）。"""
        for m in self._mounts:
            if m.sensor_type == sensor_type and m.mount_id in self._attached:
                actor = self._attached.pop(m.mount_id)
                try:
                    actor.stop()
                    actor.destroy()
                    logger.info(f"Sensor '{sensor_type.value}' detached from '{m.mount_id}'")
                except Exception as exc:
                    logger.warning(f"Sensor detach error: {exc}")

    def detach_all(self) -> None:
        """卸载所有已挂载传感器（场景销毁时调用）。"""
        for mount_id, actor in list(self._attached.items()):
            try:
                actor.stop()
                actor.destroy()
            except Exception as exc:
                logger.warning(f"detach_all error for '{mount_id}': {exc}")
        self._attached.clear()
        logger.info("All sensors detached")

    @property
    def attached_count(self) -> int:
        """当前已挂载传感器数量。"""
        return len(self._attached)
