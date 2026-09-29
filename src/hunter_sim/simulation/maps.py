"""地图系统数据模型（模块 3.2）。

定义 CARLA 内置地图注册表、自定义 OpenDRIVE 地图描述与坐标系元数据。所有模型
均为不可变 ``@dataclass(slots=True, frozen=True)``，不暴露 ``carla.*`` 原生类型，
供 :class:`~hunter_sim.simulation.protocols.MapManager` 契约与各实现层跨层传递。

内容对应开发提示词 §3.2：

- 3.2.1 内置地图：``Town01``~``Town10`` 的元数据注册表；
- 3.2.2 自定义地图：OpenDRIVE 1.4 / 1.6 加载参数与来源描述；
- 3.2.3 坐标系：CARLA 左手坐标系（X 东 / Y 北 / Z 上，单位米）元数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class MapCategory(StrEnum):
    """地图场景类别，用于按用途检索内置地图。"""

    SIMPLE_URBAN = "simple_urban"  # 简单城市网格 / 小型城市
    URBAN = "urban"  # 复杂城市 / 多车道
    HIGHWAY = "highway"  # 高速公路
    RURAL = "rural"  # 乡村道路
    CBD = "cbd"  # 城市中心密集区
    CUSTOM = "custom"  # 自定义 OpenDRIVE 地图（类别由用户指定或未知）


class MapSource(StrEnum):
    """地图来源类型。"""

    BUILTIN = "builtin"  # CARLA 内置地图
    OPENDRIVE = "opendrive"  # 自定义 OpenDRIVE 地图


class BuiltInMap(StrEnum):
    """CARLA 0.9.16 内置地图标识，取值为服务端地图名。"""

    TOWN01 = "Town01"
    TOWN02 = "Town02"
    TOWN03 = "Town03"
    TOWN04 = "Town04"
    TOWN05 = "Town05"
    TOWN06 = "Town06"
    TOWN07 = "Town07"
    TOWN10 = "Town10"


@dataclass(frozen=True, slots=True)
class MapInfo:
    """地图元数据描述。

    Attributes:
        name: 服务端地图名（内置为 ``Town0x``，自定义为来源标识）。
        description: 地图说明。
        applicable_use: 适用场景（如「基础测试」「高速测试」）。
        category: 场景类别。
        source: 地图来源（内置 / OpenDRIVE）。
        opendrive_path: 自定义 OpenDRIVE 地图文件路径；内置地图为 ``None``。
    """

    name: str
    description: str
    applicable_use: str
    category: MapCategory
    source: MapSource = MapSource.BUILTIN
    opendrive_path: Path | None = None

    @property
    def is_builtin(self) -> bool:
        """是否为 CARLA 内置地图。"""
        return self.source is MapSource.BUILTIN


@dataclass(frozen=True, slots=True)
class CoordinateFrame:
    """坐标系元数据（模块 3.2.3）。

    Attributes:
        handedness: 坐标手性（左手 / 右手）。
        x_axis: X 轴朝向。
        y_axis: Y 轴朝向。
        z_axis: Z 轴朝向。
        unit: 长度单位。
        origin: 原点定义说明。
    """

    handedness: str
    x_axis: str
    y_axis: str
    z_axis: str
    unit: str
    origin: str


# CARLA 标准坐标系：左手系，X 东 / Y 北 / Z 上，单位米，原点由地图定义。
CARLA_COORDINATE_FRAME = CoordinateFrame(
    handedness="left",
    x_axis="east",
    y_axis="north",
    z_axis="up",
    unit="meter",
    origin="map-defined (typically map center)",
)

# 与实车坐标系转换由 VIL 映射模块负责，本层仅提供目标坐标系定义。
OPENDRIVE_VEHICLE_MAPPING_NOTE = "地图坐标与实车坐标系的转换通过 VIL 映射模块处理。"


class OpenDriveVersion(StrEnum):
    """支持的 OpenDRIVE 标准版本（模块 3.2.2）。"""

    V1_4 = "1.4"
    V1_6 = "1.6"


@dataclass(frozen=True, slots=True)
class OpenDriveOptions:
    """自定义 OpenDRIVE 地图生成参数（映射到 ``carla.OpendriveGenerationParameters``）。

    Attributes:
        vertex_distance: 道路几何离散化的顶点间距（米），越小越平滑但越耗性能。
        max_road_length: 单段道路最大长度（米）。
        wall_height: 道路护栏高度（米），``0`` 表示不生成护栏。
        additional_step_for_vc: 垂直曲线（vc）段的额外离散步长。
        tolerance: 几何拟合容差。
        version: 目标 OpenDRIVE 版本。
    """

    vertex_distance: float = 2.0
    max_road_length: float = 50.0
    wall_height: float = 0.0
    additional_step_for_vc: float = 0.01
    tolerance: float = 0.1
    version: OpenDriveVersion = OpenDriveVersion.V1_4

    def __post_init__(self) -> None:
        """校验生成参数取值范围。"""
        if self.vertex_distance <= 0:
            raise ValueError(f"vertex_distance 必须为正: {self.vertex_distance}")
        if self.max_road_length <= 0:
            raise ValueError(f"max_road_length 必须为正: {self.max_road_length}")
        if self.wall_height < 0:
            raise ValueError(f"wall_height 不能为负: {self.wall_height}")
        if self.tolerance <= 0:
            raise ValueError(f"tolerance 必须为正: {self.tolerance}")


# 内置地图注册表（§3.2.1）。键为 BuiltInMap，值为对应元数据。
BUILTIN_MAP_REGISTRY: dict[BuiltInMap, MapInfo] = {
    BuiltInMap.TOWN01: MapInfo(
        name=BuiltInMap.TOWN01.value,
        description="简单城市网格道路",
        applicable_use="基础测试",
        category=MapCategory.SIMPLE_URBAN,
    ),
    BuiltInMap.TOWN02: MapInfo(
        name=BuiltInMap.TOWN02.value,
        description="小型城市，有环岛",
        applicable_use="基础测试",
        category=MapCategory.SIMPLE_URBAN,
    ),
    BuiltInMap.TOWN03: MapInfo(
        name=BuiltInMap.TOWN03.value,
        description="复杂城市，多车道、高速",
        applicable_use="综合测试",
        category=MapCategory.URBAN,
    ),
    BuiltInMap.TOWN04: MapInfo(
        name=BuiltInMap.TOWN04.value,
        description="高速公路+小镇",
        applicable_use="高速测试",
        category=MapCategory.HIGHWAY,
    ),
    BuiltInMap.TOWN05: MapInfo(
        name=BuiltInMap.TOWN05.value,
        description="大城市，复杂路口",
        applicable_use="复杂场景",
        category=MapCategory.URBAN,
    ),
    BuiltInMap.TOWN06: MapInfo(
        name=BuiltInMap.TOWN06.value,
        description="长高速公路",
        applicable_use="高速测试",
        category=MapCategory.HIGHWAY,
    ),
    BuiltInMap.TOWN07: MapInfo(
        name=BuiltInMap.TOWN07.value,
        description="乡村道路",
        applicable_use="乡村场景",
        category=MapCategory.RURAL,
    ),
    BuiltInMap.TOWN10: MapInfo(
        name=BuiltInMap.TOWN10.value,
        description="城市CBD",
        applicable_use="城市密集场景",
        category=MapCategory.CBD,
    ),
}

# OpenDRIVE 自定义地图必需要素（§3.2.2 校验清单）。
OPENDRIVE_REQUIRED_FEATURES: tuple[str, ...] = (
    "road_geometry",
    "lanes",
    "junctions",
    "traffic_signals",
)


__all__ = [
    "BUILTIN_MAP_REGISTRY",
    "CARLA_COORDINATE_FRAME",
    "OPENDRIVE_REQUIRED_FEATURES",
    "OPENDRIVE_VEHICLE_MAPPING_NOTE",
    "BuiltInMap",
    "CoordinateFrame",
    "MapCategory",
    "MapInfo",
    "MapSource",
    "OpenDriveOptions",
    "OpenDriveVersion",
]
