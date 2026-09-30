"""地图系统管理模块（PROMPT-ENG-001-B）。

封装 CARLA 0.9.16 地图加载、切换、列表查询、OpenDRIVE 自定义地图导入，
以及地图预加载缓存管理。

CARLA 内置地图：Town01 ~ Town07, Town10HD_Opt。
自定义地图：通过 client.generate_opendrive_world() 加载 .xodr 文件。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Protocol

from pydantic import BaseModel, Field

from hunter_sim.common.exceptions import CarlaSimulationError, ConfigurationError
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# CARLA 0.9.16 内置地图列表
BUILTIN_MAPS: frozenset[str] = frozenset([
    "Town01", "Town02", "Town03", "Town04", "Town05",
    "Town06", "Town07", "Town10HD", "Town10HD_Opt",
])


class MapInfo(BaseModel):
    """地图元信息。

    Attributes:
        map_id: 地图唯一标识符。
        display_name: 显示名称。
        is_builtin: 是否为 CARLA 内置地图。
        xodr_path: OpenDRIVE 文件路径（自定义地图时有效）。
        recommended_quality: 推荐画质等级字符串。
        load_time_ms: 上次加载耗时（毫秒）。
    """

    map_id: str
    display_name: str = ""
    is_builtin: bool = False
    xodr_path: Optional[Path] = None
    recommended_quality: str = Field("medium", pattern="^(low|medium|epic)$")
    load_time_ms: float = 0.0

    def model_post_init(self, __context: object) -> None:
        """自动填充 display_name。"""
        if not self.display_name:
            self.display_name = self.map_id


class CarlaWorldProtocol(Protocol):
    """CARLA World 对象的协议（供类型检查，运行时使用 carla.World）。"""

    def get_map(self) -> object: ...
    def get_settings(self) -> object: ...
    def apply_settings(self, settings: object) -> None: ...
    def tick(self) -> int: ...


class CarlaClientProtocol(Protocol):
    """CARLA Client 对象的协议。"""

    def get_world(self) -> CarlaWorldProtocol: ...
    def load_world(self, map_name: str) -> CarlaWorldProtocol: ...
    def generate_opendrive_world(self, xodr_str: str) -> CarlaWorldProtocol: ...


class MapManager:
    """CARLA 地图管理器。

    通过依赖注入的 CARLA client 实例进行地图操作。
    支持内置地图加载、自定义 OpenDRIVE 地图导入、地图列表查询和预加载缓存。

    Args:
        client: CARLA 客户端连接实例。
        preload_maps: 初始化时预加载的地图 ID 列表。
        custom_map_dir: 自定义 OpenDRIVE 文件存储目录。
    """

    def __init__(
        self,
        client: CarlaClientProtocol,
        preload_maps: list[str] | None = None,
        custom_map_dir: Path | None = None,
    ) -> None:
        self._client = client
        self._custom_map_dir: Path = custom_map_dir or Path("data/maps")
        self._loaded_maps: dict[str, MapInfo] = {}
        self._current_map_id: Optional[str] = None
        self._current_world: Optional[CarlaWorldProtocol] = None

        # 注册内置地图
        for m in BUILTIN_MAPS:
            self._loaded_maps[m] = MapInfo(map_id=m, is_builtin=True, display_name=m)

        # 预加载
        if preload_maps:
            for map_id in preload_maps:
                try:
                    self.load_map(map_id)
                except Exception as exc:
                    logger.warning(f"Failed to preload map '{map_id}': {exc}")

    @property
    def current_map_id(self) -> Optional[str]:
        """当前已加载地图 ID。"""
        return self._current_map_id

    @property
    def current_world(self) -> Optional[CarlaWorldProtocol]:
        """当前 CARLA World 对象引用。"""
        return self._current_world

    def list_available_maps(self) -> list[MapInfo]:
        """返回所有已注册地图的元信息列表。

        Returns:
            MapInfo 列表（内置 + 已导入的自定义地图）。
        """
        return list(self._loaded_maps.values())

    def is_map_available(self, map_id: str) -> bool:
        """检查指定地图是否已注册可用。

        Args:
            map_id: 地图 ID 字符串。

        Returns:
            True 表示地图可用。
        """
        return map_id in self._loaded_maps

    def load_map(self, map_id: str) -> CarlaWorldProtocol:
        """加载内置地图或已注册的自定义地图。

        支持 "custom/{map_name}" 引用形式（设计文档附录 B）：
        若未注册，自动从 custom_map_dir 目录查找 {map_name}.xodr 并导入。

        Args:
            map_id: 地图 ID。

        Returns:
            加载后的 CARLA World 对象。

        Raises:
            ConfigurationError: 地图 ID 未注册。
            CarlaSimulationError: 加载过程中发生错误。
        """
        if map_id not in self._loaded_maps and map_id.startswith("custom/"):
            # 附录 B：创建实例时指定 map: "custom/{map_name}"，动态导入后加载
            name = map_id.split("/", 1)[1]
            xodr_path = self._custom_map_dir / f"{name}.xodr"
            if xodr_path.exists():
                self.import_opendrive_map(map_id, xodr_path)

        if map_id not in self._loaded_maps:
            raise ConfigurationError(
                operation="load_map",
                message=f"Map '{map_id}' not registered. Call import_opendrive_map() first or use a builtin map.",
            )

        info = self._loaded_maps[map_id]
        logger.info(f"Loading map: {map_id} (builtin={info.is_builtin})")
        t0 = time.perf_counter()

        try:
            if info.is_builtin:
                world = self._client.load_world(map_id)
            else:
                xodr_str = self._read_xodr_file(info.xodr_path)
                world = self._client.generate_opendrive_world(xodr_str)

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            info.load_time_ms = elapsed_ms
            self._current_world = world
            self._current_map_id = map_id
            logger.info(f"Map '{map_id}' loaded in {elapsed_ms:.0f}ms")
            return world

        except Exception as exc:
            raise CarlaSimulationError(
                operation=f"load_map({map_id})",
                message=str(exc),
            ) from exc

    def import_opendrive_map(
        self,
        map_id: str,
        xodr_path: Path,
        recommended_quality: str = "medium",
    ) -> MapInfo:
        """导入自定义 OpenDRIVE 地图并注册到管理器。

        Args:
            map_id: 地图唯一 ID。
            xodr_path: .xodr 文件本地路径。
            recommended_quality: 推荐画质等级。

        Returns:
            注册的 MapInfo。

        Raises:
            ConfigurationError: 文件不存在或格式不支持。
        """
        if not xodr_path.exists():
            raise ConfigurationError(
                operation="import_opendrive_map",
                message=f"OpenDRIVE file not found: {xodr_path}",
            )
        if xodr_path.suffix.lower() != ".xodr":
            raise ConfigurationError(
                operation="import_opendrive_map",
                message=f"Invalid file extension (expected .xodr): {xodr_path}",
            )

        info = MapInfo(
            map_id=map_id,
            display_name=map_id,
            is_builtin=False,
            xodr_path=xodr_path,
            recommended_quality=recommended_quality,
        )
        self._loaded_maps[map_id] = info
        logger.info(f"Registered OpenDRIVE map '{map_id}' from {xodr_path}")
        return info

    def get_spawn_points(self, world: CarlaWorldProtocol) -> list[object]:
        """获取当前地图所有可用生成点（vehicle spawn points）。

        Args:
            world: CARLA World 对象。

        Returns:
            Transform 列表。
        """
        try:
            return world.get_map().get_spawn_points()  # type: ignore[union-attr]
        except Exception as exc:
            raise CarlaSimulationError(
                operation="get_spawn_points",
                message=str(exc),
            ) from exc

    def get_ground_z(self, world: CarlaWorldProtocol, x: float, y: float) -> float:
        """获取地图坐标 (x, y) 处的路面 Z 坐标。

        Args:
            world: CARLA World 对象。
            x: 地图 X 坐标（米）。
            y: 地图 Y 坐标（米）。

        Returns:
            路面 Z 坐标（米）。
        """
        try:
            map_obj = world.get_map()  # type: ignore[union-attr]
            loc = _make_carla_location(x, y, 100.0)
            ground_loc = map_obj.get_waypoint_z(loc)  # type: ignore[union-attr]
            return float(ground_loc) if ground_loc is not None else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _read_xodr_file(xodr_path: Optional[Path]) -> str:
        """读取 .xodr 文件内容为字符串。"""
        if xodr_path is None:
            raise ConfigurationError("read_xodr", "xodr_path is None")
        return xodr_path.read_text(encoding="utf-8")


def _make_carla_location(x: float, y: float, z: float) -> object:
    """创建 CARLA Location 对象（CARLA 库延迟导入）。"""
    import carla  # noqa: PLC0415
    return carla.Location(x=x, y=y, z=z)
