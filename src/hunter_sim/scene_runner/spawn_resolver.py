"""出生点有效坐标替换与参与者偏移叠加（HunterCore V1.25.0 契约）。

平台下发 ``map.spawn_point`` 在场景模板中通常为占位原点 ``(0,0,0)``，直接使用会
让自车落在世界原点（画面黑屏、参与者挤在一起）。本模块在**仿真端**按当前
CARLA world 的 ``get_map().get_spawn_points()`` 选择合法出生点：

- 仅 ``map_type=carla_town`` 做原点/路面外替换；``hd_map`` / ``site_map`` 坐标
  由场景自带，必须逐字使用；
- 替换按 ``scene_key`` 稳定散列，保证同一场景多次下发可复现；
- ``spawn_semantics=relative_to_ego`` 时，参与者坐标为相对自车基准的偏移，
  需要先替换自车基准再叠加（平台不再代算绝对坐标）。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Optional

from hunter_sim.common.utils import get_logger
from hunter_sim.scene_runner.scene_config import SpawnPoint

logger = get_logger(__name__)

#: 米；|x|、|y|、|z| 均小于此视为「落在世界原点」的退化点
ORIGIN_EPS = 0.1

#: 单次兜底最多尝试的候选出生点数量（避免异常地图造成长循环）
MAX_SPAWN_ATTEMPTS = 16

#: 视为 CARLA 内置城镇的地图类型（仅这些类型做出生点替换）
_CARLA_TOWN_TYPES = frozenset({"carla_town", "carla", "town"})

_RELATIVE_SEMANTICS = "relative_to_ego"


def is_carla_town(map_type: str) -> bool:
    """判断地图类型是否允许做出生点替换。"""
    return (map_type or "").strip().lower() in _CARLA_TOWN_TYPES


def is_placeholder_origin(spawn_point: SpawnPoint) -> bool:
    """判断场景出生点是否为占位原点 ``(0,0,0,0)``。"""
    return all(
        abs(value) < ORIGIN_EPS
        for value in (
            spawn_point.x,
            spawn_point.y,
            spawn_point.z,
            spawn_point.yaw_deg,
        )
    )


def scene_key_of(scene_id: str, scene_version: str = "") -> str:
    """构造稳定散列键：``scene_id + logical_version``。"""
    version = (scene_version or "").strip()
    return f"{scene_id}::{version}" if version else scene_id


def candidate_spawns(world: Any) -> list[Any]:
    """取地图出生点并丢弃世界原点退化点（极端场景退回全量）。

    Args:
        world: CARLA World（或测试桩）。

    Returns:
        ``carla.Transform`` 列表；地图接口不可用时返回空列表。
    """
    try:
        raw = list(world.get_map().get_spawn_points())
    except Exception as exc:  # noqa: BLE001  CARLA 地图接口异常不阻断场景装配
        logger.warning(f"spawn_points_unavailable: {exc}")
        return []
    good = [point for point in raw if not _transform_is_origin(point)]
    if good:
        return good
    if raw:
        logger.warning("spawn_points_all_at_origin: 地图仅返回原点出生点，退回全量候选")
    return raw


def _transform_is_origin(transform: Any) -> bool:
    """判断 carla.Transform 是否落在世界原点（忽略 yaw）。"""
    location = getattr(transform, "location", None)
    if location is None:
        return False
    try:
        return all(
            abs(float(getattr(location, axis, 0.0))) < ORIGIN_EPS
            for axis in ("x", "y", "z")
        )
    except (TypeError, ValueError):
        return False


def transform_to_spawn_point(transform: Any) -> SpawnPoint:
    """把 ``carla.Transform`` / 内部 Transform 转为 ``SpawnPoint``（度制）。"""
    location = getattr(transform, "location", None) or transform
    rotation = getattr(transform, "rotation", None)
    x = float(getattr(location, "x", 0.0) or 0.0)
    y = float(getattr(location, "y", 0.0) or 0.0)
    z = float(getattr(location, "z", 0.0) or 0.0)
    pitch = float(getattr(rotation, "pitch", 0.0) or 0.0)
    yaw = float(getattr(rotation, "yaw", 0.0) or 0.0)
    roll = float(getattr(rotation, "roll", 0.0) or 0.0)
    return SpawnPoint(
        x=x,
        y=y,
        z=z,
        yaw_deg=_clamp_degrees(yaw, -360.0, 360.0),
        pitch_deg=max(-90.0, min(90.0, pitch)),
        roll_deg=max(-180.0, min(180.0, roll)),
    )


def stable_candidate_index(scene_key: str, count: int, variant_index: int = 0) -> int:
    """按 ``scene_key`` 稳定散列取候选下标（同场景可复现、同批可分散）。"""
    if count <= 0:
        return 0
    digest = hashlib.sha256(scene_key.encode("utf-8")).digest()[:8]
    base = int.from_bytes(digest, "big")
    return (base + max(0, int(variant_index))) % count


def resolve_ego_spawn_candidates(
    world: Any,
    spawn_point: SpawnPoint,
    *,
    map_type: str,
    scene_key: str,
    variant_index: int = 0,
    allow_offroad_replacement: bool = True,
) -> list[SpawnPoint]:
    """计算自车出生点候选序列（首个为首选，其余用于 spawn 失败重试）。

    规则：

    - 非 ``carla_town``：原样返回场景坐标，不查询 spawn points；
    - ``carla_town`` 且为占位原点 / 路面外：按稳定散列替换为地图合法出生点；
    - ``carla_town`` 且坐标有效：首选场景坐标，仍附带候选点用于 spawn 失败重试。
    """
    if not is_carla_town(map_type):
        return [spawn_point]

    need_replace = is_placeholder_origin(spawn_point)
    reason = "placeholder_origin" if need_replace else ""
    if not need_replace and allow_offroad_replacement:
        on_road = spawn_point_on_road(world, spawn_point)
        if on_road is False:
            need_replace = True
            reason = "off_road"

    candidates = candidate_spawns(world)
    ordered = _ordered_candidates(candidates, scene_key, variant_index)

    if need_replace and ordered:
        logger.info(
            "ego_spawn_replaced reason=%s scene=%s before=(%.2f,%.2f,%.2f,yaw=%.1f) "
            "after=(%.2f,%.2f,%.2f,yaw=%.1f) candidates=%d",
            reason,
            scene_key,
            spawn_point.x,
            spawn_point.y,
            spawn_point.z,
            spawn_point.yaw_deg,
            ordered[0].x,
            ordered[0].y,
            ordered[0].z,
            ordered[0].yaw_deg,
            len(ordered),
        )
        return ordered

    if need_replace:
        logger.warning(
            "ego_spawn_replace_skipped reason=%s scene=%s: 地图无可用出生点，沿用下发坐标 "
            "(%.2f,%.2f,%.2f)",
            reason,
            scene_key,
            spawn_point.x,
            spawn_point.y,
            spawn_point.z,
        )
        return [spawn_point]

    # 坐标本身有效：尊重场景配置，但保留候选项直到 spawn 成功（点被占/路面外）
    return [spawn_point] + ordered[: max(0, MAX_SPAWN_ATTEMPTS - 1)]


def _ordered_candidates(
    candidates: list[Any], scene_key: str, variant_index: int
) -> list[SpawnPoint]:
    """把地图候选点按稳定起点旋转成有序列表。"""
    if not candidates:
        return []
    start = stable_candidate_index(scene_key, len(candidates), variant_index)
    limit = min(len(candidates), MAX_SPAWN_ATTEMPTS)
    return [
        transform_to_spawn_point(candidates[(start + offset) % len(candidates)])
        for offset in range(limit)
    ]


def spawn_point_on_road(world: Any, spawn_point: SpawnPoint) -> Optional[bool]:
    """判断坐标是否位于可行驶车道上。

    Returns:
        ``True`` 在路面、``False`` 明确不在路面、``None`` 无法判断（接口缺失）。
    """
    try:
        get_waypoint = getattr(world.get_map(), "get_waypoint", None)
    except Exception:  # noqa: BLE001  地图接口缺失时不做替换判定
        return None
    if not callable(get_waypoint):
        return None
    try:
        import carla  # noqa: PLC0415  仅真实环境可用；无 SDK 时交由 spawn 结果判定
    except ImportError:
        return None
    location = carla.Location(x=spawn_point.x, y=spawn_point.y, z=spawn_point.z)
    try:
        try:
            waypoint = get_waypoint(location, project_to_road=False)
        except TypeError:
            # 老版本签名不支持 project_to_road，退化为投影查询（只能近似判断）
            waypoint = get_waypoint(location)
    except RuntimeError:
        return False
    except Exception as exc:  # noqa: BLE001  CARLA 内部异常不应导致误替换
        logger.debug(f"spawn_waypoint_check_failed: {exc}")
        return None
    return waypoint is not None


def resolve_actor_spawn(
    ego_transform: Any,
    offset: SpawnPoint,
    spawn_semantics: str,
) -> SpawnPoint:
    """把参与者相对自车的偏移叠加到（替换后的）自车基准上。

    平台下发的 ``x/y`` 是**自车车体系**偏移（x 向前、y 向左，米），并非世界系
    增量；自车基准被替换成地图出生点后 yaw 通常不是 0（Town01 常见 yaw=90），
    因此必须按基准 yaw 旋转后再叠加，否则「正前方 18m 的前车」会被摆到自车
    正右方 18m——环视主视区里看不到任何参与者。

    Args:
        ego_transform: 自车实际 transform（``carla.Transform`` 或 ``SpawnPoint``）。
        offset: 平台下发的参与者相对偏移（x 前向/y 左向/z 米、yaw 度）。
        spawn_semantics: ``relative_to_ego`` 时执行旋转叠加，其余原样返回。

    Returns:
        参与者世界坐标（度制，yaw 归一到 ``[-180, 180)``）。
    """
    if (spawn_semantics or "").strip().lower() != _RELATIVE_SEMANTICS:
        return offset
    base = (
        ego_transform
        if isinstance(ego_transform, SpawnPoint)
        else transform_to_spawn_point(ego_transform)
    )
    heading = math.radians(base.yaw_deg)
    cos_h = math.cos(heading)
    sin_h = math.sin(heading)
    # 车体系 (x 前向, y 左向) → 世界系旋转：forward=(cos,sin), left=(-sin,cos)
    return SpawnPoint(
        x=base.x + offset.x * cos_h - offset.y * sin_h,
        y=base.y + offset.x * sin_h + offset.y * cos_h,
        z=base.z + offset.z,
        yaw_deg=_normalize_degrees(base.yaw_deg + offset.yaw_deg),
        # 平台偏移不含俯仰/横滚；与旧 to_absolute_spawn_offsets 口径一致
        pitch_deg=0.0,
        roll_deg=0.0,
    )


def relative_offset_from_ego(
    ego_transform: Any, point: SpawnPoint
) -> tuple[float, float]:
    """计算世界坐标点相对自车的车体系偏移（排障/日志用）。

    Args:
        ego_transform: 自车 transform（``carla.Transform`` 或 ``SpawnPoint``）。
        point: 待换算的世界坐标点。

    Returns:
        ``(ahead_m, left_m)``：正值为自车前方/左侧，负值为后方/右侧。
    """
    base = (
        ego_transform
        if isinstance(ego_transform, SpawnPoint)
        else transform_to_spawn_point(ego_transform)
    )
    heading = math.radians(base.yaw_deg)
    dx = point.x - base.x
    dy = point.y - base.y
    ahead = dx * math.cos(heading) + dy * math.sin(heading)
    left = -dx * math.sin(heading) + dy * math.cos(heading)
    return ahead, left


def _normalize_degrees(value: float) -> float:
    """把角度归一到 ``[-180, 180)``。"""
    normalized = (value + 180.0) % 360.0 - 180.0
    return 180.0 if math.isclose(normalized, -180.0, abs_tol=1e-9) else normalized


def _clamp_degrees(value: float, low: float, high: float) -> float:
    """角度区间截断（SpawnPoint 字段约束）。"""
    return max(low, min(high, value))


__all__ = [
    "MAX_SPAWN_ATTEMPTS",
    "ORIGIN_EPS",
    "candidate_spawns",
    "is_carla_town",
    "is_placeholder_origin",
    "relative_offset_from_ego",
    "resolve_actor_spawn",
    "resolve_ego_spawn_candidates",
    "scene_key_of",
    "spawn_point_on_road",
    "stable_candidate_index",
    "transform_to_spawn_point",
]
