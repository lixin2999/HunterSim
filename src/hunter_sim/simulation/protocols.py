"""L1 仿真核心层接口契约（``typing.Protocol``）。

上层（L2+）仅依赖这些 Protocol，具体实现类以 ``Impl`` 结尾并由容器注入。
CARLA 原生类型不出现在契约签名中，统一使用本层 :mod:`models` 的不可变数据对象。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from hunter_sim.core.contracts import VehicleState
from hunter_sim.simulation.maps import (
    CoordinateFrame,
    MapCategory,
    MapInfo,
    OpenDriveOptions,
)
from hunter_sim.simulation.models import Transform, VehicleCommand


@runtime_checkable
class CarlaConnectionManager(Protocol):
    """CARLA 连接生命周期管理契约（模块 1.1）。"""

    @property
    def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    def get_client(self) -> Any:
        """返回底层 ``carla.Client``（对上层为不透明对象）。"""
        ...

    def get_world(self, *, reload: bool = False) -> Any:
        """返回当前 ``carla.World``，懒加载并缓存。"""
        ...

    async def load_world(self, map_name: str) -> Any:
        """切换地图并刷新世界缓存。"""
        ...


@runtime_checkable
class VehicleController(Protocol):
    """主车生命周期与控制契约（模块 1.2）。"""

    @property
    def is_alive(self) -> bool: ...

    def get_actor(self) -> Any:
        """返回底层 ``carla`` 车辆演员（未生成时 ``None``），供传感器附着。"""
        ...

    async def spawn(
        self, blueprint: str, spawn_point: Transform, *, autopilot: bool = False
    ) -> None: ...

    async def apply_control(self, control: VehicleCommand) -> None: ...

    async def set_autopilot(self, enabled: bool) -> None: ...

    def get_state(self) -> VehicleState: ...

    async def destroy(self) -> None: ...


class ScenarioState(StrEnum):
    """场景运行状态机（模块 1.3）。"""

    IDLE = "idle"
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


# 场景事件钩子签名：无参可调用。
Hook = Callable[[], None]


@runtime_checkable
class ScenarioManager(Protocol):
    """场景配置、交通流与运行生命周期契约（模块 1.3）。"""

    @property
    def state(self) -> ScenarioState: ...

    async def configure(self) -> None:
        """加载地图、设置天气与交通流。"""
        ...

    async def start(self) -> None: ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def reset(self) -> None: ...

    async def stop(self) -> None: ...

    async def step(self) -> VehicleState:
        """推进一帧，触发 on_tick 钩子并返回主车状态。"""
        ...

    def register_hook(self, name: str, hook: Hook) -> None: ...


@runtime_checkable
class MapManager(Protocol):
    """地图系统契约（模块 3.2）。

    统一封装内置地图注册表查询、内置地图加载、自定义 OpenDRIVE 地图生成，
    以及坐标系元数据访问。CARLA 原生句柄（``World``）以 ``Any`` 返回。
    """

    @property
    def current_map(self) -> MapInfo | None:
        """当前已加载地图的元数据；未加载时为 ``None``。"""
        ...

    @property
    def coordinate_frame(self) -> CoordinateFrame:
        """CARLA 坐标系元数据（左手系，单位米）。"""
        ...

    def list_builtin_maps(self) -> list[MapInfo]:
        """返回全部内置地图元数据（§3.2.1）。"""
        ...

    def list_maps_by_category(self, category: MapCategory) -> list[MapInfo]:
        """按场景类别过滤内置地图。"""
        ...

    def get_map_info(self, map_name: str) -> MapInfo:
        """按名称查询内置地图元数据，未找到时抛出异常。"""
        ...

    def is_known_map(self, map_name: str) -> bool:
        """名称是否为合法内置地图。"""
        ...

    async def load_map(self, map_name: str) -> Any:
        """按名称加载内置地图并刷新当前地图上下文。"""
        ...

    async def load_opendrive(
        self, xodr_path: str, *, options: OpenDriveOptions | None = None
    ) -> Any:
        """从本地 OpenDRIVE 文件生成自定义地图世界（§3.2.2）。"""
        ...

    async def load_opendrive_xml(
        self, opendrive_xml: str, *, options: OpenDriveOptions | None = None
    ) -> Any:
        """从 OpenDRIVE XML 字符串生成自定义地图世界。"""
        ...


__all__ = [
    "CarlaConnectionManager",
    "Hook",
    "MapManager",
    "ScenarioManager",
    "ScenarioState",
    "VehicleController",
]
