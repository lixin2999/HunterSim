"""模块 3.2：地图系统实现。

统一封装三类能力：

- 3.2.1 内置地图：基于 :data:`~hunter_sim.simulation.maps.BUILTIN_MAP_REGISTRY`
  提供注册表查询、按类别过滤、名称校验与地图加载；
- 3.2.2 自定义地图：从本地 ``.xodr`` 文件或 OpenDRIVE XML 字符串经
  ``client.generate_opendrive_world`` 生成世界，支持生成参数配置；
- 3.2.3 坐标系：暴露 CARLA 左手坐标系元数据（与实车坐标转换由 VIL 映射模块负责）。

CARLA 依赖隔离：地图查询与校验不触及 ``carla`` 包；仅在生成 OpenDRIVE 世界时惰性
导入 ``carla``，与 :mod:`hunter_sim.simulation.connection` 的工厂注入策略一致，
使单测无需真实服务端即可运行。所有阻塞式 CARLA 调用经 ``asyncio.to_thread`` 包装。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from hunter_sim.core.exceptions import CarlaSimulationError, ConfigurationError
from hunter_sim.core.logging import logger
from hunter_sim.simulation.maps import (
    BUILTIN_MAP_REGISTRY,
    CARLA_COORDINATE_FRAME,
    BuiltInMap,
    CoordinateFrame,
    MapCategory,
    MapInfo,
    MapSource,
    OpenDriveOptions,
)
from hunter_sim.simulation.protocols import CarlaConnectionManager

_XODR_SUFFIX = ".xodr"


class MapManagerImpl:
    """满足 :class:`~hunter_sim.simulation.protocols.MapManager` 契约。"""

    def __init__(self, connection: CarlaConnectionManager) -> None:
        """初始化地图管理器。

        Args:
            connection: CARLA 连接管理器（Protocol 注入），用于切换地图与生成世界。
        """
        self._connection = connection
        self._current: MapInfo | None = None

    @property
    def current_map(self) -> MapInfo | None:
        """当前已加载地图的元数据；未加载时为 ``None``。"""
        return self._current

    @property
    def coordinate_frame(self) -> CoordinateFrame:
        """CARLA 坐标系元数据（左手系，单位米，X 东 / Y 北 / Z 上）。"""
        return CARLA_COORDINATE_FRAME

    def list_builtin_maps(self) -> list[MapInfo]:
        """返回全部内置地图元数据（§3.2.1，按注册表定义顺序）。"""
        return list(BUILTIN_MAP_REGISTRY.values())

    def list_maps_by_category(self, category: MapCategory) -> list[MapInfo]:
        """按场景类别过滤内置地图。

        Args:
            category: 目标场景类别。

        Returns:
            匹配类别的地图元数据列表（可能为空）。
        """
        return [info for info in BUILTIN_MAP_REGISTRY.values() if info.category is category]

    def is_known_map(self, map_name: str) -> bool:
        """名称是否为合法内置地图。"""
        return any(member.value == map_name for member in BuiltInMap)

    def get_map_info(self, map_name: str) -> MapInfo:
        """按名称查询内置地图元数据。

        Args:
            map_name: 内置地图名（如 ``Town03``）。

        Returns:
            对应的 :class:`MapInfo`。

        Raises:
            ConfigurationError: 名称不是合法的内置地图。
        """
        for member, info in BUILTIN_MAP_REGISTRY.items():
            if member.value == map_name:
                return info
        valid = ", ".join(member.value for member in BuiltInMap)
        raise ConfigurationError(f"未知内置地图: {map_name}（可选: {valid}）")

    async def load_map(self, map_name: str) -> Any:
        """加载内置地图并刷新当前地图上下文。

        Args:
            map_name: 内置地图名（如 ``Town05``）。

        Returns:
            底层 ``carla.World``（对上层为不透明对象）。

        Raises:
            ConfigurationError: 名称不是合法的内置地图。
        """
        info = self.get_map_info(map_name)
        world = await self._connection.load_world(info.name)
        self._current = info
        logger.bind(component="map").info("已加载内置地图: {} ({})", info.name, info.description)
        return world

    async def load_opendrive(
        self,
        xodr_path: str | Path,
        *,
        options: OpenDriveOptions | None = None,
        category: MapCategory = MapCategory.CUSTOM,
    ) -> Any:
        """从本地 OpenDRIVE 文件生成自定义地图世界（§3.2.2）。

        Args:
            xodr_path: ``.xodr`` 文件路径。
            options: 地图生成参数；缺省使用 :class:`OpenDriveOptions` 默认值。
            category: 自定义地图归类，默认 ``CUSTOM``。

        Returns:
            底层 ``carla.World``。

        Raises:
            ConfigurationError: 路径不存在或扩展名非 ``.xodr``。
            CarlaSimulationError: 文件读取失败。
        """
        path = Path(xodr_path)
        if not path.is_file():
            raise ConfigurationError(f"OpenDRIVE 地图文件不存在: {path}")
        if path.suffix.lower() != _XODR_SUFFIX:
            raise ConfigurationError(f"地图文件须为 {_XODR_SUFFIX} 格式: {path}")
        try:
            xml = await asyncio.to_thread(path.read_text, "utf-8")
        except OSError as exc:
            raise CarlaSimulationError(f"OpenDRIVE 文件读取失败: {path} ({exc})") from exc
        world = await self._generate_world(xml, options)
        self._current = MapInfo(
            name=path.name,
            description=f"自定义 OpenDRIVE 地图 ({path.name})",
            applicable_use="用户自定义场景",
            category=category,
            source=MapSource.OPENDRIVE,
            opendrive_path=path,
        )
        logger.bind(component="map").info("已从文件生成 OpenDRIVE 地图: {}", path)
        return world

    async def load_opendrive_xml(
        self,
        opendrive_xml: str,
        *,
        options: OpenDriveOptions | None = None,
        category: MapCategory = MapCategory.CUSTOM,
        name: str = "opendrive",
    ) -> Any:
        """从 OpenDRIVE XML 字符串生成自定义地图世界。

        Args:
            opendrive_xml: OpenDRIVE 规范的 XML 内容。
            options: 地图生成参数。
            category: 自定义地图归类，默认 ``CUSTOM``。
            name: 当前地图上下文显示名。

        Returns:
            底层 ``carla.World``。
        """
        world = await self._generate_world(opendrive_xml, options)
        self._current = MapInfo(
            name=name,
            description=f"自定义 OpenDRIVE 地图 ({name})",
            applicable_use="用户自定义场景",
            category=category,
            source=MapSource.OPENDRIVE,
        )
        logger.bind(component="map").info("已从 XML 生成 OpenDRIVE 地图: {}", name)
        return world

    async def _generate_world(self, opendrive_xml: str, options: OpenDriveOptions | None) -> Any:
        """调用 CARLA 生成 OpenDRIVE 世界并刷新连接缓存。

        Raises:
            CarlaSimulationError: CARLA 生成世界失败。
        """
        opts = options or OpenDriveOptions()
        client = self._connection.get_client()
        try:
            await asyncio.to_thread(self._generate_opendrive_world, client, opendrive_xml, opts)
        except Exception as exc:  # CARLA RPC / 解析异常统一归为仿真错误
            raise CarlaSimulationError(f"生成 OpenDRIVE 世界失败: {exc}") from exc
        return self._connection.get_world(reload=True)

    @staticmethod
    def _generate_opendrive_world(
        client: Any, opendrive_xml: str, options: OpenDriveOptions
    ) -> Any:
        """惰性导入 ``carla`` 并按生成参数创建世界（阻塞调用，运行于工作线程）。"""
        import carla

        params = carla.OpendriveGenerationParameters(
            vertex_distance=options.vertex_distance,
            max_road_length=options.max_road_length,
            wall_height=options.wall_height,
            additional_step_for_vc_values=options.additional_step_for_vc,
        )
        return client.generate_opendrive_world(opendrive_xml, params)


__all__ = ["MapManagerImpl"]
