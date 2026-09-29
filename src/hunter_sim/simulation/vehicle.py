"""模块 1.2：车辆控制器实现。

封装主车的生成、控制指令下发、状态读取与销毁。CARLA 原生调用统一经
``asyncio.to_thread`` 包装以避免阻塞事件循环。控制器依赖
:class:`CarlaConnectionManager` 契约获取世界对象，便于注入 mock 进行测试。
"""

from __future__ import annotations

import asyncio
from typing import Any

import carla

from hunter_sim.core.contracts import VehicleState
from hunter_sim.core.exceptions import CarlaSimulationError
from hunter_sim.core.logging import logger
from hunter_sim.simulation.models import Transform, VehicleCommand
from hunter_sim.simulation.protocols import CarlaConnectionManager


class VehicleControllerImpl:
    """满足 :class:`~hunter_sim.simulation.protocols.VehicleController` 契约。"""

    def __init__(self, connection: CarlaConnectionManager) -> None:
        """初始化车辆控制器。

        Args:
            connection: CARLA 连接管理器（Protocol 注入）。
        """
        self._connection = connection
        self._vehicle: Any | None = None
        self._last_command = VehicleCommand()
        self._autopilot = False

    @property
    def is_alive(self) -> bool:
        """主车是否已生成且存活。"""
        return self._vehicle is not None

    @property
    def autopilot_enabled(self) -> bool:
        """当前是否处于自动驾驶（Traffic Manager）模式。"""
        return self._autopilot

    def get_actor(self) -> Any | None:
        """返回底层 ``carla`` 车辆演员（未生成时为 ``None``），供传感器附着。"""
        return self._vehicle

    def _require_vehicle(self) -> Any:
        if self._vehicle is None:
            raise CarlaSimulationError("主车尚未生成，请先调用 spawn()")
        return self._vehicle

    @staticmethod
    def _to_carla_transform(transform: Transform) -> Any:
        return carla.Transform(
            carla.Location(x=transform.location.x, y=transform.location.y, z=transform.location.z),
            carla.Rotation(
                pitch=transform.rotation.pitch,
                yaw=transform.rotation.yaw,
                roll=transform.rotation.roll,
            ),
        )

    def _select_blueprint(self, world: Any, blueprint: str, color: str | None) -> Any:
        lib = world.get_blueprint_library()
        matches = lib.filter(blueprint)
        if not matches:
            raise CarlaSimulationError(f"未找到车辆蓝图: {blueprint}")
        bp = matches[0]
        if color is not None and bp.has_attribute("color"):
            bp.set_attribute("color", color)
        return bp

    async def spawn(
        self,
        blueprint: str,
        spawn_point: Transform,
        *,
        autopilot: bool = False,
        color: str | None = None,
    ) -> None:
        """在主车生成点生成车辆。

        Args:
            blueprint: CARLA 蓝图名，如 ``vehicle.tesla.model3``。
            spawn_point: 生成位姿。
            autopilot: 是否启用 Traffic Manager 自动驾驶。
            color: 可选车身颜色 "R,G,B"。

        Raises:
            CarlaSimulationError: 生成失败（位姿被占用或蓝图非法）。
        """
        world = self._connection.get_world()
        bp = await asyncio.to_thread(self._select_blueprint, world, blueprint, color)
        transform = await asyncio.to_thread(self._to_carla_transform, spawn_point)
        actor = await asyncio.to_thread(world.try_spawn_actor, bp, transform)
        if actor is None:
            raise CarlaSimulationError(
                f"车辆生成失败，位姿不可用: loc=({spawn_point.location.x}, "
                f"{spawn_point.location.y}, {spawn_point.location.z})"
            )
        self._vehicle = actor
        logger.bind(component="vehicle").info("主车已生成: {}", blueprint)
        await self.set_autopilot(autopilot)

    async def apply_control(self, control: VehicleCommand) -> None:
        """下发油门/刹车/转向/手刹控制。

        Args:
            control: 高层控制指令。档位信息记录于最近指令，供 :meth:`get_state` 回报。
        """
        vehicle = self._require_vehicle()
        vc = carla.VehicleControl(
            throttle=control.throttle,
            steer=control.steer,
            brake=control.brake,
            hand_brake=control.hand_brake,
            reverse=control.reverse,
        )
        await asyncio.to_thread(vehicle.apply_control, vc)
        self._last_command = control

    async def set_autopilot(self, enabled: bool) -> None:
        """切换自动驾驶（Traffic Manager）与手动控制模式。"""
        vehicle = self._require_vehicle()
        await asyncio.to_thread(vehicle.set_autopilot, enabled)
        self._autopilot = enabled

    def get_state(self) -> VehicleState:
        """读取主车当前完整状态。

        时间戳与帧号取自世界快照，控制字段回读最近一次下发的指令。

        Raises:
            CarlaSimulationError: 主车未生成。
        """
        vehicle = self._require_vehicle()
        world = self._connection.get_world()
        snapshot = world.get_snapshot()
        loc = vehicle.get_location()
        rot = vehicle.get_transform().rotation
        vel = vehicle.get_velocity()
        acc = vehicle.get_acceleration()
        cmd = self._last_command
        return VehicleState(
            timestamp=float(snapshot.timestamp),
            frame_id=int(snapshot.frame),
            x=float(loc.x),
            y=float(loc.y),
            z=float(loc.z),
            roll=float(rot.roll),
            pitch=float(rot.pitch),
            yaw=float(rot.yaw),
            velocity_x=float(vel.x),
            velocity_y=float(vel.y),
            velocity_z=float(vel.z),
            acceleration_x=float(acc.x),
            acceleration_y=float(acc.y),
            acceleration_z=float(acc.z),
            throttle=cmd.throttle,
            brake=cmd.brake,
            steer=cmd.steer,
            gear=cmd.gear,
        )

    async def destroy(self) -> None:
        """销毁主车并释放引用（幂等）。"""
        if self._vehicle is not None:
            vehicle = self._vehicle
            self._vehicle = None
            self._autopilot = False
            await asyncio.to_thread(vehicle.destroy)
            logger.bind(component="vehicle").info("主车已销毁")

    async def __aenter__(self) -> VehicleControllerImpl:
        """进入上下文：返回自身（主车需显式 spawn）。"""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """退出上下文：销毁主车释放资源。"""
        await self.destroy()


__all__ = ["VehicleControllerImpl"]
