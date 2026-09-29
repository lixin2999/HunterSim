"""模块 2.1：传感器生命周期管理。

在 CARLA 车辆上按配置附着各类传感器（相机 / LiDAR / Radar / IMU / GNSS），注册数据回调，
将 CARLA 流回调线程中的测量投递到 :mod:`~hunter_sim.acquisition.buffer`；支持动态启停与销毁。

CARLA 依赖以不透明句柄（``Any``）处理：真实对象由运行期提供，单测用 fake carla + mock 世界。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import carla

from hunter_sim.acquisition.protocols import BufferRegistry
from hunter_sim.core.config import SensorConfig
from hunter_sim.core.exceptions import SensorSimulationError
from hunter_sim.core.logging import logger

#: SensorConfig.type → CARLA 蓝图别名
_BLUEPRINT_ALIAS: dict[str, str] = {
    "camera.rgb": "sensor.camera.rgb",
    "camera.depth": "sensor.camera.depth",
    "camera.semantic": "sensor.camera.semantic_segmentation",
    "lidar.ray_cast": "sensor.lidar.ray_cast",
    "radar": "sensor.other.radar",
    "imu": "sensor.other.imu",
    "gnss": "sensor.other.gnss",
}

#: 配置字段名 → CARLA 蓝图属性名
_ATTR_NAME: dict[str, str] = {
    "width": "image_size_x",
    "height": "image_size_y",
    "tick_rate": "sampling_rate",
}

#: 从 SensorConfig 读取并尝试写入蓝图的数值字段
_NUMERIC_FIELDS: tuple[str, ...] = (
    "width",
    "height",
    "fov",
    "channels",
    "range",
    "points_per_second",
    "rotation_frequency",
    "tick_rate",
)


class SensorManagerImpl:
    """满足 :class:`~hunter_sim.acquisition.protocols.SensorManager` 契约。"""

    def __init__(self, registry: BufferRegistry, loop: asyncio.AbstractEventLoop) -> None:
        """初始化传感器管理器。

        Args:
            registry: 传感器数据缓冲注册表（回调投递目标）。
            loop: 消费者事件循环（跨线程投递所用）。
        """
        self._registry = registry
        self._loop = loop
        self._actors: dict[str, Any] = {}
        self._active: set[str] = set()
        self._callbacks: dict[str, list[Callable[[Any], None]]] = {}

    async def initialize(self, configs: list[SensorConfig], *, vehicle: Any, world: Any) -> None:
        """按配置在车辆上附着传感器并注册流回调（初始为停用状态）。"""
        for cfg in configs:
            alias = _BLUEPRINT_ALIAS.get(cfg.type)
            if alias is None:
                raise SensorSimulationError(f"不支持的传感器类型: {cfg.type}")
            blueprint = world.get_blueprint_library().find(alias)
            self._apply_attributes(blueprint, cfg)
            transform = carla.Transform(
                carla.Location(*cfg.position),
                carla.Rotation(*cfg.rotation),
            )
            actor = world.spawn_actor(blueprint, transform, attach_to=vehicle)
            self._actors[cfg.id] = actor
            actor.listen(lambda measurement, _id=cfg.id: self._on_data(_id, measurement))
            logger.bind(component="sensor_manager", sensor_id=cfg.id).info(
                "已附着传感器 {}", cfg.type
            )

    def register_callback(self, sensor_id: str, callback: Callable[[Any], None]) -> None:
        """为指定传感器追加数据处理回调（在 CARLA 回调线程执行）。"""
        self._callbacks.setdefault(sensor_id, []).append(callback)

    async def start_all(self) -> None:
        """启动全部已附着传感器（开启数据门控）。"""
        self._active = set(self._actors)

    async def stop_all(self) -> None:
        """停止全部传感器数据投递（保留演员，可再次启动）。"""
        self._active.clear()

    async def start_sensor(self, sensor_id: str) -> None:
        """启动单个传感器。"""
        if sensor_id not in self._actors:
            raise SensorSimulationError(f"未附着的传感器: {sensor_id}")
        self._active.add(sensor_id)

    async def stop_sensor(self, sensor_id: str) -> None:
        """停止单个传感器数据投递。"""
        self._active.discard(sensor_id)

    async def destroy_all(self) -> None:
        """停止并销毁全部传感器演员，清理内部状态（幂等）。"""
        self._active.clear()
        for sensor_id, actor in list(self._actors.items()):
            try:
                actor.stop()
            except Exception:  # 销毁尽力而为
                logger.bind(component="sensor_manager", sensor_id=sensor_id).debug("停止传感器失败")
            try:
                actor.destroy()
            except Exception:  # 销毁尽力而为
                logger.bind(component="sensor_manager", sensor_id=sensor_id).debug("销毁传感器失败")
            self._actors.pop(sensor_id, None)

    @property
    def sensor_ids(self) -> list[str]:
        """当前已附着的传感器 id。"""
        return list(self._actors)

    def _on_data(self, sensor_id: str, measurement: Any) -> None:
        """CARLA 流回调：门控后经缓冲投递，并转发到用户回调（异常隔离）。"""
        if sensor_id not in self._active:
            return
        self._registry.get_or_create(sensor_id).put_from_thread(measurement)
        for callback in self._callbacks.get(sensor_id, ()):
            try:
                callback(measurement)
            except Exception:  # 用户回调异常不得中断采集
                logger.bind(component="sensor_manager", sensor_id=sensor_id).exception(
                    "传感器回调异常"
                )

    @staticmethod
    def _apply_attributes(blueprint: Any, cfg: SensorConfig) -> None:
        """将配置字段写入蓝图（仅当蓝图支持该属性）。"""
        if blueprint.has_attribute("role_name"):
            blueprint.set_attribute("role_name", cfg.id)
        for field in _NUMERIC_FIELDS:
            value = getattr(cfg, field, None)
            if value is None:
                continue
            attr = _ATTR_NAME.get(field, field)
            if blueprint.has_attribute(attr):
                blueprint.set_attribute(attr, str(value))


__all__ = ["SensorManagerImpl"]
