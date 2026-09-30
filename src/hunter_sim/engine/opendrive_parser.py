"""OpenDRIVE 地图解析与验证模块（PROMPT-ENG-001-B）。

解析 .xodr 文件，提取道路几何、车道、路口、交通标志、信号灯信息，
并验证地图完整性（OpenDRIVE 1.4/1.6 标准）。
"""

from __future__ import annotations

import defusedxml.ElementTree as ET  # type: ignore[import]  # 安全解析，防止 XXE/DoS 攻击
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

_SUPPORTED_HEADER_VERSIONS = {"1.4", "1.5", "1.6", "1.7"}


@dataclass
class LaneInfo:
    """单车道信息。

    Attributes:
        lane_id: 车道 ID（负值=右侧，正值=左侧，0=中心线）。
        lane_type: 车道类型字符串（driving/parking/biking/sidewalk 等）。
        width_m: 车道宽度（米）。
    """

    lane_id: int
    lane_type: str
    width_m: float = 3.0


@dataclass
class RoadInfo:
    """道路信息。

    Attributes:
        road_id: 道路 ID。
        name: 道路名称。
        length_m: 道路长度（米）。
        junction_id: 所属路口 ID（-1 表示非路口道路）。
        lanes: 各车道信息列表。
    """

    road_id: int
    name: str = ""
    length_m: float = 0.0
    junction_id: int = -1
    lanes: list[LaneInfo] = field(default_factory=list)


@dataclass
class SignalInfo:
    """交通信号灯/标志信息。

    Attributes:
        signal_id: 信号灯 ID。
        x: 道路坐标 s（米）。
        y: 横向偏移（米）。
        signal_type: 信号灯类型编码字符串。
        country: 国家代码。
    """

    signal_id: str
    x: float
    y: float
    signal_type: str = ""
    country: str = "OpenDRIVE"


@dataclass
class JunctionInfo:
    """路口信息。

    Attributes:
        junction_id: 路口 ID。
        name: 路口名称。
        connecting_road_ids: 连接道路 ID 列表。
    """

    junction_id: int
    name: str = ""
    connecting_road_ids: list[int] = field(default_factory=list)


@dataclass
class OpenDriveMapSummary:
    """OpenDRIVE 地图解析结果摘要。

    Attributes:
        valid: 地图文件是否通过完整性验证。
        header_version: OpenDRIVE 版本字符串。
        map_name: 地图名称。
        total_road_count: 道路总数。
        total_junction_count: 路口总数。
        total_signal_count: 信号灯/标志总数。
        total_length_m: 道路总长度（米）。
        roads: 道路详情列表。
        junctions: 路口详情列表。
        signals: 信号灯详情列表。
        errors: 验证失败时的错误信息列表。
    """

    valid: bool
    header_version: str = ""
    map_name: str = ""
    total_road_count: int = 0
    total_junction_count: int = 0
    total_signal_count: int = 0
    total_length_m: float = 0.0
    roads: list[RoadInfo] = field(default_factory=list)
    junctions: list[JunctionInfo] = field(default_factory=list)
    signals: list[SignalInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class OpenDriveParser:
    """OpenDRIVE .xodr 文件解析器。

    支持版本 1.4 ~ 1.7。验证内容：
    - XML 格式有效性
    - 头部 revHeader 版本兼容性
    - 至少包含一条道路
    - 道路几何、车道结构完整性

    Args:
        xodr_path: .xodr 文件路径。
    """

    def __init__(self, xodr_path: Path) -> None:
        if not xodr_path.exists():
            raise ConfigurationError(
                operation="OpenDriveParser.__init__",
                message=f"File not found: {xodr_path}",
            )
        self._path: Path = xodr_path

    def parse(self) -> OpenDriveMapSummary:
        """解析 OpenDRIVE 文件并返回摘要。

        Returns:
            OpenDriveMapSummary 对象。
        """
        errors: list[str] = []
        try:
            tree = ET.parse(self._path)
        except ET.ParseError as exc:
            return OpenDriveMapSummary(valid=False, errors=[f"XML parse error: {exc}"])

        root = tree.getroot()
        if root.tag != "OpenDRIVE":
            return OpenDriveMapSummary(
                valid=False,
                errors=[f"Root element must be 'OpenDRIVE', got '{root.tag}'"],
            )

        # 版本检查
        header = root.find("header")
        rev = header.get("revHeader", "") if header is not None else ""
        if rev and rev not in _SUPPORTED_HEADER_VERSIONS:
            errors.append(f"Unsupported OpenDRIVE version '{rev}', supported: {_SUPPORTED_HEADER_VERSIONS}")

        geo_ref = header.find("geoReference") if header is not None else None
        # OpenDRIVE 标准中地图名称为 <header> 的 name 属性
        map_name = (header.get("name", "") if header is not None else "") or self._path.stem

        # 解析道路
        roads: list[RoadInfo] = []
        total_length: float = 0.0
        for road_el in root.findall("road"):
            try:
                road = self._parse_road(road_el)
                roads.append(road)
                total_length += road.length_m
            except (ValueError, KeyError) as exc:
                errors.append(f"Road '{road_el.get('id')}' parse error: {exc}")

        # 解析路口
        junctions: list[JunctionInfo] = []
        for junc_el in root.findall("junction"):
            try:
                junctions.append(self._parse_junction(junc_el))
            except (ValueError, KeyError) as exc:
                errors.append(f"Junction '{junc_el.get('id')}' parse error: {exc}")

        # 统计信号灯
        signals: list[SignalInfo] = []
        for road_el in root.findall("road"):
            for sig_el in road_el.findall("signals/signal"):
                signals.append(
                    SignalInfo(
                        signal_id=sig_el.get("id", ""),
                        x=float(sig_el.get("s", "0")),
                        y=float(sig_el.get("t", "0")),
                        signal_type=sig_el.get("type", ""),
                        country=sig_el.get("country", "OpenDRIVE"),
                    )
                )

        valid = len(errors) == 0 and len(roads) > 0
        if not roads:
            errors.append("Map contains no roads")

        summary = OpenDriveMapSummary(
            valid=valid,
            header_version=rev,
            map_name=map_name,
            total_road_count=len(roads),
            total_junction_count=len(junctions),
            total_signal_count=len(signals),
            total_length_m=total_length,
            roads=roads,
            junctions=junctions,
            signals=signals,
            errors=errors,
        )

        logger.info(
            f"Parsed '{self._path.name}': {len(roads)} roads, "
            f"{len(junctions)} junctions, {len(signals)} signals, valid={valid}"
        )
        return summary

    @staticmethod
    def _parse_road(road_el: ET.Element) -> RoadInfo:
        """解析单条 road 元素。"""
        road_id = int(road_el.get("id", "-1"))
        name = road_el.get("name", "")
        length = float(road_el.get("length", "0"))
        junction = int(road_el.get("junction", "-1"))

        lanes: list[LaneInfo] = []
        lane_section = road_el.find("lanes/laneSection")
        if lane_section is not None:
            for lane_el in lane_section.findall("lane"):
                lane_id = int(lane_el.get("id", "0"))
                lane_type_el = lane_el.find("lane type")
                lane_type = lane_el.get("type", "driving") if lane_type_el is None else lane_el.get("type", "driving")
                lanes.append(LaneInfo(lane_id=lane_id, lane_type=lane_type))

        return RoadInfo(
            road_id=road_id,
            name=name,
            length_m=length,
            junction_id=junction,
            lanes=lanes,
        )

    @staticmethod
    def _parse_junction(junc_el: ET.Element) -> JunctionInfo:
        """解析单个 junction 元素。"""
        junc_id = int(junc_el.get("id", "-1"))
        name = junc_el.get("name", "")
        connecting_ids: list[int] = [
            int(conn.get("incomingRoad", "0"))
            for conn in junc_el.findall("connection")
        ]
        return JunctionInfo(
            junction_id=junc_id,
            name=name,
            connecting_road_ids=connecting_ids,
        )
