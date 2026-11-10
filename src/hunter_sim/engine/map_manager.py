"""地图系统管理模块（PROMPT-ENG-001-B）。

封装 CARLA 0.9.16 地图加载、切换、列表查询、OpenDRIVE 自定义地图导入，
以及地图预加载缓存管理。

CARLA 内置地图：Town01 ~ Town07, Town10HD_Opt。
自定义地图：通过 client.generate_opendrive_world() 加载 .xodr 文件。
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol, cast

from pydantic import BaseModel, Field

from hunter_sim.common.exceptions import CarlaSimulationError, ConfigurationError
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# CARLA 0.9.16 内置地图列表
BUILTIN_MAPS: frozenset[str] = frozenset([
    "Town01", "Town02", "Town03", "Town04", "Town05",
    "Town06", "Town07", "Town10HD", "Town10HD_Opt",
])

# custom/{map_name} 引用名称安全字符集（防路径穿越，与 API 层 _MAP_ID_PATTERN 一致）
_CUSTOM_MAP_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

# 地图加载默认 RPC 超时（秒）：首次 load_world 需构建世界/导航网格，
# Town06 等大地图冷启动常达 20~60s，远大于常规 RPC 超时。
_DEFAULT_MAP_LOAD_TIMEOUT_SECONDS = 180.0


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
    def get_trafficmanager(self, port: int = 8000) -> object: ...
    def set_timeout(self, seconds: float) -> None: ...


class MapManager:
    """CARLA 地图管理器。

    通过依赖注入的 CARLA client 实例进行地图操作。
    支持内置地图加载、自定义 OpenDRIVE 地图导入、地图列表查询和预加载缓存。

    Args:
        client: CARLA 客户端连接实例。
        preload_maps: 初始化时预加载的地图 ID 列表。
        custom_map_dir: 自定义 OpenDRIVE 文件存储目录。
        rpc_timeout_seconds: 常规 RPC 超时（地图加载结束后恢复为该值）。
        map_load_timeout_seconds: 地图加载/切换专用超时（首次 ``load_world``
            可达数十秒，不能沿用常规 RPC 超时）。
    """

    def __init__(
        self,
        client: CarlaClientProtocol,
        preload_maps: list[str] | None = None,
        custom_map_dir: Path | None = None,
        rpc_timeout_seconds: float = 10.0,
        map_load_timeout_seconds: float = _DEFAULT_MAP_LOAD_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._rpc_timeout_seconds = float(rpc_timeout_seconds)
        # 地图加载超时至少不短于常规 RPC 超时，避免配置写反把加载卡死
        self._map_load_timeout_seconds = max(
            float(map_load_timeout_seconds), float(rpc_timeout_seconds)
        )
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
    def client(self) -> CarlaClientProtocol:
        """底层 CARLA Client（Traffic Manager 等扩展用）。"""
        return self._client

    @property
    def current_world(self) -> Optional[CarlaWorldProtocol]:
        """当前 CARLA World 对象引用。"""
        return self._current_world

    def get_traffic_manager(self, port: int = 8000) -> Any:
        """获取指定端口的 CARLA Traffic Manager 实例。

        CARLA 的 Traffic Manager 归属于 ``client``（``carla.Client.get_trafficmanager``），
        并不在 ``World`` 上。同步模式下必须显式把 TM 也切到同步模式，否则
        ``set_autopilot(True)`` 的车辆会一直等待世界 tick 而不移动。

        Args:
            port: Traffic Manager 端口（默认 8000）。

        Returns:
            Traffic Manager 实例。

        Raises:
            CarlaSimulationError: 客户端不支持 Traffic Manager。
        """
        getter = getattr(self._client, "get_trafficmanager", None)
        if not callable(getter):
            raise CarlaSimulationError(
                operation="get_traffic_manager",
                message="CARLA client 不支持 get_trafficmanager()",
            )
        return getter(int(port))

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

    @contextmanager
    def _map_load_timeout(self) -> Iterator[None]:
        """地图加载期间临时放宽 CARLA RPC 超时。

        CARLA ``load_world`` 与后续 RPC 共用同一个客户端超时：若沿用常规
        超时（默认 10s），首次加载/切换大地图（如 Town06）会在服务端仍在
        构建世界时抛 ``time-out of 10000ms while waiting for the simulator``。
        退出时（无论成功/异常）恢复为常规超时。
        """
        setter = getattr(self._client, "set_timeout", None)
        if not callable(setter) or self._map_load_timeout_seconds <= self._rpc_timeout_seconds:
            yield
            return
        try:
            setter(self._map_load_timeout_seconds)
            applied = True
        except Exception as exc:  # noqa: BLE001  Mock/受限客户端可能不支持
            logger.debug(f"Cannot extend CARLA RPC timeout for map load: {exc}")
            applied = False
        try:
            yield
        finally:
            if applied:
                try:
                    setter(self._rpc_timeout_seconds)
                except Exception as exc:  # noqa: BLE001  恢复失败不应掩藏原始错误
                    logger.debug(f"Cannot restore CARLA RPC timeout: {exc}")

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
            # 安全校验：名称仅限字母/数字/_/-（防 ../ 路径穿越读取任意文件）
            if not _CUSTOM_MAP_NAME_PATTERN.fullmatch(name):
                raise ConfigurationError(
                    operation="load_map",
                    message=(
                        f"Invalid custom map name '{name}' in '{map_id}' "
                        "(allowed: letters, digits, '_', '-')"
                    ),
                )
            xodr_path = (self._custom_map_dir / f"{name}.xodr").resolve()
            # 双重保险：解析后路径必须仍在自定义地图目录内
            if not xodr_path.is_relative_to(self._custom_map_dir.resolve()):
                raise ConfigurationError(
                    operation="load_map",
                    message=f"Custom map path escapes the resource directory: {map_id}",
                )
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
        timeout_raised = self._map_load_timeout_seconds > self._rpc_timeout_seconds

        try:
            with self._map_load_timeout():
                if info.is_builtin:
                    world = self._client.load_world(map_id)
                else:
                    xodr_str = self._read_xodr_file(info.xodr_path)
                    world = self._client.generate_opendrive_world(xodr_str)

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            info.load_time_ms = elapsed_ms
            self._current_world = world
            self._current_map_id = map_id
            logger.info(
                f"Map '{map_id}' loaded in {elapsed_ms:.0f}ms"
                + (
                    f" (rpc timeout raised to {self._map_load_timeout_seconds:.0f}s)"
                    if timeout_raised
                    else ""
                )
            )
            return world

        except Exception as exc:
            raise CarlaSimulationError(
                operation=f"load_map({map_id})",
                message=self._load_failure_message(map_id, exc),
            ) from exc

    def _load_failure_message(self, map_id: str, exc: Exception) -> str:
        """构造地图加载失败信息（超时附加处置提示）。

        超时的两种典型成因必须能一眼区分：
        1. 大地图首次加载耗时超过客户端 RPC 超时 → 调大 ``CARLA_MAP_LOAD_TIMEOUT_SECONDS``；
        2. 上一个场景结束时世界停在同步模式等待 ``tick`` → 需重启 CARLA server。
        """
        message = str(exc)
        if "time-out" not in message.lower() and "timeout" not in message.lower():
            return message
        return (
            f"{message}（地图 '{map_id}' 加载超过 {self._map_load_timeout_seconds:.0f}s："
            "若为大地图冷启动可调大 CARLA_MAP_LOAD_TIMEOUT_SECONDS；"
            "若 CARLA server 处于同步模式等待 tick（上一个场景未恢复异步），需重启 CARLA server）"
        )

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
            carla_world: Any = world  # CARLA 对象无类型 stub，Any 间接层避免 suppress 真实错写
            return cast(list[object], carla_world.get_map().get_spawn_points())
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
            carla_world: Any = world
            map_obj: Any = carla_world.get_map()
            loc = _make_carla_location(x, y, 100.0)
            ground_loc = map_obj.get_waypoint_z(loc)
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
