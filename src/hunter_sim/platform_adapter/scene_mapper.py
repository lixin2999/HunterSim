"""平台 ``scene_config`` → 内部 ``SceneConfig`` 映射。

平台在 ``scene-service`` 已合并 ``param_overrides``，本模块**只读** ``scene_config``，
不重新应用覆盖项（尤其不能改 ``map``）。

不支持的参与者行为/仿真模式不会被静默降级为直线行驶，而是抛出带错误码的
``SceneMappingError``，由执行器把错误码写入实例 ``message``：

- ``unsupported_behavior:<behavior_type>``
- ``unsupported_mode:<mode>``
- ``invalid_scene_config:<detail>``
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from hunter_sim.common.models import QualityLevel, SimMode
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.coordinate_converter import CalibrationParams
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
    MappingWarning,
    SceneConfig,
    SceneEventDefinition,
    SceneEventTrigger,
    SceneEventType,
    ScenarioBehaviorType,
    SensorSpec,
    SpawnPoint,
    TrafficParticipantConfig,
    TriggerType,
    WeatherConfig,
)

logger = get_logger(__name__)

_MODE_MAP: dict[str, SimMode] = {
    "sil": SimMode.SIL,
    "vil": SimMode.VIL,
}

# 平台 behavior_type → 内部可执行行为（仅收录 BuiltinScenario 能如实表达的取值）
_BEHAVIOR_MAP: dict[str, ScenarioBehaviorType] = {
    # 纵向匀速/车道保持
    "constant_velocity": ScenarioBehaviorType.CONSTANT_SPEED,
    "lane_follow": ScenarioBehaviorType.CONSTANT_SPEED,
    "lane_keep": ScenarioBehaviorType.CONSTANT_SPEED,
    "car_following": ScenarioBehaviorType.CONSTANT_SPEED,
    "forward": ScenarioBehaviorType.CONSTANT_SPEED,
    "creep": ScenarioBehaviorType.CONSTANT_SPEED,
    "queue_creep": ScenarioBehaviorType.CONSTANT_SPEED,
    "start_up": ScenarioBehaviorType.CONSTANT_SPEED,
    "borrow_oncoming": ScenarioBehaviorType.CONSTANT_SPEED,
    # 加减速
    "accelerate": ScenarioBehaviorType.ACCELERATE,
    "lane_change_accelerate": ScenarioBehaviorType.ACCELERATE,
    "decelerate": ScenarioBehaviorType.DECELERATE,
    "emergency_brake": ScenarioBehaviorType.DECELERATE,
    "yield": ScenarioBehaviorType.DECELERATE,
    "breakdown_stop": ScenarioBehaviorType.DECELERATE,
    # 静止
    "static": ScenarioBehaviorType.STATIC,
    "stop": ScenarioBehaviorType.STATIC,
    "double_park": ScenarioBehaviorType.STATIC,
    "parking": ScenarioBehaviorType.STATIC,
    "hesitation_stop": ScenarioBehaviorType.STATIC,
    "door_open": ScenarioBehaviorType.STATIC,
    # 切入/切出
    "cut_in": ScenarioBehaviorType.CUT_IN,
    "aggressive_merge": ScenarioBehaviorType.CUT_IN,
    "cut_out": ScenarioBehaviorType.CUT_OUT,
    # 行人横穿
    "cross_road": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    "pedestrian_crossing": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    "sudden_cross": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    "child_chase_ball": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    "occluded_dash": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    "boarding": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    # 兼容内部取值
    "constant_speed": ScenarioBehaviorType.CONSTANT_SPEED,
    "stop_and_go": ScenarioBehaviorType.STOP_AND_GO,
    "pedestrian_cross": ScenarioBehaviorType.PEDESTRIAN_CROSS,
}

#: 平台语义只能近似的 3 种行为（映射后仍可执行，但需在报告中标注）
_BEHAVIOR_APPROX: dict[str, str] = {
    "borrow_oncoming": "平台生成点已在对向车道且 yaw=3.14159，按逆向匀速近似",
    "door_open": "先按路边静止车辆近似；开门动画待 door API 探测",
    "boarding": "按短程行人横穿（走向最近停放车辆）近似",
}

_EVENT_TYPE_MAP: dict[str, SceneEventType] = {
    "collision": SceneEventType.COLLISION,
    "emergency_brake": SceneEventType.EMERGENCY_BRAKE,
    "obstacle_appearance": SceneEventType.OBSTACLE_AVOID,
    "cut_in": SceneEventType.CUSTOM,
    "pedestrian_crossing": SceneEventType.CUSTOM,
    "manual_takeover": SceneEventType.CUSTOM,
    "sensor_failure": SceneEventType.CUSTOM,
}

_CONDITION_TYPE_MAP: dict[str, TriggerType] = {
    "time": TriggerType.TIME,
    "simulation_time": TriggerType.TIME,
    "distance": TriggerType.DISTANCE,
    "relative_distance": TriggerType.DISTANCE,
    "traveled_distance": TriggerType.TRAVELED_DISTANCE,
    "reach_position": TriggerType.TRAVELED_DISTANCE,
    "speed": TriggerType.VELOCITY,
    "velocity": TriggerType.VELOCITY,
    "standstill": TriggerType.VELOCITY,
    "acceleration": TriggerType.ACCELERATION,
    "offroad": TriggerType.OFFROAD,
    "collision": TriggerType.EVENT,
    "time_headway": TriggerType.TIME_HEADWAY,
    "time_to_collision": TriggerType.TIME_TO_COLLISION,
    "event": TriggerType.EVENT,
}

#: 平台语义只能近似的触发条件
_CONDITION_APPROX: dict[str, str] = {
    "reach_position": "traveled_distance（平台只下发标量距离）",
    "offroad": "0/1 越界指标（平台阈值 0.5 的物理含义未明确）",
}

_COMPARATORS = frozenset({">", ">=", "<", "<=", "==", "!="})
_RELATIONS = frozenset({"", "longitudinal", "lateral", "euclidean", "sightline"})

_FAILURE_EVENT_TYPES = frozenset(
    {SceneEventType.COLLISION, SceneEventType.EMERGENCY_BRAKE}
)

#: 平台旧 object_kind 别名 → 归一化取值
_KIND_ALIASES: dict[str, str] = {
    "parked": "parked_vehicle",
    "fallen_load": "fallen_load",
}
_CYCLIST_VEHICLE_KINDS = frozenset({"bicycle"})
_ANIMAL_KINDS = frozenset({"animal", "dog", "cat", "deer"})
_VEHICLE_LIKE_PROP_KINDS = frozenset(
    {"parked_vehicle", "accident_vehicle", "broken_vehicle"}
)
#: 平台 object_kind → static.prop 蓝图候选链（末尾为通配兜底）
_PROP_BLUEPRINT_CHAINS: dict[str, tuple[str, ...]] = {
    "cone": ("static.prop.constructioncone", "static.prop.trafficcone01", "static.prop.*"),
    "construction": ("static.prop.streetbarrier", "static.prop.constructioncone", "static.prop.*"),
    "road_barrier": ("static.prop.streetbarrier", "static.prop.concretebarrier01", "static.prop.*"),
    "container": ("static.prop.container", "static.prop.metalcontainer", "static.prop.*"),
    "tyre": ("static.prop.tyre", "static.prop.*"),
    "fallen_load": ("static.prop.woodbarrel", "static.prop.crate", "static.prop.*"),
    "debris": ("static.prop.crate", "static.prop.woodbarrel", "static.prop.*"),
    "sign": ("static.prop.trafficwarning", "static.prop.sign_*", "static.prop.*"),
    "bin": ("static.prop.bin", "static.prop.*"),
    "ball": ("static.prop.constructioncone", "static.prop.*"),
    "bus_stop_shelter": ("static.prop.streetbarrier", "static.prop.*"),
    "building_corner": ("static.prop.streetbarrier", "static.prop.*"),
    "gravel": ("static.prop.*",),
    "liquid": ("static.prop.*",),
}
#: 无专用资产、按方盒/锥桶近似的 object_kind
_DEGRADED_PROP_KINDS = frozenset(
    {"ball", "bus_stop_shelter", "building_corner", "gravel", "liquid"}
)
_DEFAULT_PROP_CHAIN: tuple[str, ...] = ("static.prop.constructioncone", "static.prop.*")

_SPEED_KEYS: tuple[str, ...] = (
    "speed",
    "speed_ms",
    "target_speed",
    "target_speed_ms",
    "velocity",
    "crossing_speed",
    "cut_in_speed",
    "cyclist_speed",
    "animal_speed",
)
_DEFAULT_SPEED: dict[str, float] = {
    "walker": 1.5,
    "cyclist": 4.0,
    "vehicle": 8.0,
    "prop": 0.0,
}
_WALKER_MIN_SPEED = 0.3
_WALKER_MAX_SPEED = 4.0

#: 平台参与者容器键（按优先级）。平台不同版本/不同场景模板会把交通参与者放在
#: ``actors`` 之外的别名键下（如 ``traffic_participants``）；只读 ``actors`` 会让
#: 「平台下发了行人、仿真端只有自车」这类问题无从定位，故合并读取并在日志里
#: 打印命中来源。
_ACTOR_CONTAINER_KEYS: tuple[str, ...] = (
    "actors",
    "traffic_participants",
    "participants",
    "pedestrians",
    "road_users",
    "figures",
)


class SceneMappingError(ValueError):
    """场景映射失败；``code`` 会写入实例 ``message``。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}{': ' + detail if detail else ''}")
        self.code = code
        self.detail = detail


@dataclass
class _MappingReport:
    """映射过程告警收集器（贯穿 B–H，最终写入 ``mapping_report``）。"""

    warnings: list[dict[str, Any]] = field(default_factory=list)

    def warn(self, code: str, detail: str = "", **ctx: str) -> None:
        """记录一条映射告警，同时落日志。"""
        item: dict[str, Any] = {"code": code, "detail": detail}
        item.update({key: value for key, value in ctx.items() if value})
        self.warnings.append(item)
        logger.warning(
            "scene_mapping_warning code=%s detail=%s ctx=%s", code, detail, ctx
        )


def resolve_mode(raw: Optional[str], default: str = "sil") -> SimMode:
    """解析并校验仿真模式；replay/未知模式明确失败（不静默回退）。

    Raises:
        SceneMappingError: ``unsupported_mode:<mode>``。
    """
    value = (raw or default or "sil").strip().lower()
    if value == SimMode.REPLAY.value:
        raise SceneMappingError(f"unsupported_mode:{value}", "replay 模式未实现")
    mode = _MODE_MAP.get(value)
    if mode is None:
        raise SceneMappingError(f"unsupported_mode:{value}", "仅支持 sil/vil")
    return mode


def map_scene_config(
    platform_config: dict[str, Any],
    *,
    scene_id: str,
    scene_name: str = "",
    mode: SimMode = SimMode.SIL,
    default_duration_seconds: float = 60.0,
    max_duration_seconds: float = 7200.0,
    quality: Optional[str] = None,
) -> SceneConfig:
    """把平台场景配置映射为内部 ``SceneConfig``。

    Args:
        platform_config: 平台下发的 ``scene_config``（已合并覆盖项）。
        scene_id: 场景 ID。
        scene_name: 场景名称。
        mode: 仿真模式。
        default_duration_seconds: 缺省时长。
        max_duration_seconds: 时长上限。
        quality: 平台画质（Low/Medium/High/Epic）。

    Returns:
        内部 ``SceneConfig``。

    Raises:
        SceneMappingError: 配置非法或包含不支持的行为。
    """
    if not isinstance(platform_config, dict) or not platform_config:
        raise SceneMappingError("invalid_scene_config:empty", "scene_config 为空")

    report = _MappingReport()
    map_section = _as_dict(platform_config.get("map"))
    if not map_section:
        raise SceneMappingError("invalid_scene_config:map", "缺少 map 段")
    map_id = str(map_section.get("map_id") or "").strip()
    if not map_id:
        raise SceneMappingError("invalid_scene_config:map_id", "map.map_id 为空")

    ego_section = _as_dict(platform_config.get("ego_vehicle"))
    ego_spawn = _spawn_point(map_section.get("spawn_point"), "map.spawn_point")
    raw_model = str(ego_section.get("model") or "hunter_se").strip() or "hunter_se"
    ego_blueprint = _normalize_blueprint(raw_model)
    if ego_blueprint != raw_model:
        report.warn(
            f"ego_blueprint_normalized:{raw_model}->{ego_blueprint}",
            "平台模型名大小写/连字符已归一化",
        )
    initial_speed = _clamp(_as_float(ego_section.get("initial_speed"), 0.0), 0.0, 50.0)
    initial_steer = _clamp(_as_float(ego_section.get("initial_steer"), 0.0), -1.0, 1.0)
    map_type = _map_type(map_section, map_id)
    spawn_semantics = _spawn_semantics(platform_config.get("spawn_semantics"))
    sensor_mount_reference = _as_dict(platform_config.get("sensor_mount_reference"))

    duration = _as_float(platform_config.get("duration"), default_duration_seconds)
    if duration <= 0:
        duration = default_duration_seconds
    if duration > max_duration_seconds:
        raise SceneMappingError(
            "invalid_scene_config:duration",
            f"duration={duration}s 超过上限 {max_duration_seconds}s",
        )

    actor_sections, actor_sources = _collect_actor_sections(platform_config)
    participants = [
        _map_actor(actor, index, report)
        for index, actor in enumerate(actor_sections)
    ]
    _log_participants(platform_config, actor_sections, participants, actor_sources)
    events = [
        _map_event(event, index, report)
        for index, event in enumerate(_as_list(platform_config.get("events")))
    ]
    mapping_report = _build_mapping_report(
        scene_id, platform_config, actor_sections, participants, events, report
    )

    success_criteria = _as_dict(platform_config.get("success_criteria"))
    return SceneConfig(
        scene_id=scene_id,
        scene_name=scene_name,
        map_id=map_id,
        map_type=map_type,
        spawn_semantics=spawn_semantics,
        sensor_mount_reference=sensor_mount_reference,
        quality=_quality(quality),
        mode=mode,
        ego_vehicle=EgoVehicleConfig(
            vehicle_blueprint=ego_blueprint,
            spawn_point=ego_spawn,
            # VIL 由实车位姿驱动，不能启用 autopilot（否则与 set_transform 争夺控制权）
            autopilot=mode is SimMode.SIL,
            initial_speed_ms=initial_speed,
            initial_steer=initial_steer,
        ),
        weather=_map_weather(_as_dict(platform_config.get("weather"))),
        traffic_participants=participants,
        sensors=_map_sensors(platform_config.get("sensors")),
        scene_events=events,
        duration_seconds=duration,
        timeout_seconds=max(60.0, duration * 3.0),
        mapping_warnings=[MappingWarning(**warning) for warning in report.warnings],
        extra_params={
            "success_criteria": success_criteria,
            "platform_scene_config": {
                "map": map_section,
                "spawn_semantics": spawn_semantics,
                "sensor_mount_reference": sensor_mount_reference,
                "success_criteria": success_criteria,
            },
            "mapping_report": mapping_report,
        },
    )


def map_calibration(vil: Optional[dict[str, Any]]) -> Optional[CalibrationParams]:
    """把平台 VIL 标定转换为 ``CalibrationParams``（弧度制）。

    平台字段：``map_x/map_y/map_heading/odom_x/odom_y/odom_heading``。
    转换公式（与 ``CoordinateTransformer`` 一致）::

        yaw0 = -map_heading - odom_heading
        x0 = map_x - (odom_x*cos(yaw0) - odom_y*sin(yaw0))
        y0 = map_y + (odom_x*sin(yaw0) + odom_y*cos(yaw0))

    Returns:
        标定参数；``vil`` 缺少 calibration 时返回 None（使用配置默认值）。
    """
    if not isinstance(vil, dict):
        return None
    calibration = _as_dict(vil.get("calibration")) or vil
    required = ("map_x", "map_y", "map_heading", "odom_x", "odom_y", "odom_heading")
    if not all(key in calibration for key in required):
        return None
    map_x = _as_float(calibration["map_x"])
    map_y = _as_float(calibration["map_y"])
    map_heading = _as_float(calibration["map_heading"])
    odom_x = _as_float(calibration["odom_x"])
    odom_y = _as_float(calibration["odom_y"])
    odom_heading = _as_float(calibration["odom_heading"])

    yaw0 = -map_heading - odom_heading
    x0 = map_x - (odom_x * math.cos(yaw0) - odom_y * math.sin(yaw0))
    y0 = map_y + (odom_x * math.sin(yaw0) + odom_y * math.cos(yaw0))
    return CalibrationParams(x0=x0, y0=y0, yaw0=yaw0, z_source="map")


# ── 内部映射 ─────────────────────────────────────────────────────────────────


def _actor_entries(raw: Any) -> list[Any]:
    """把参与者容器取值展开为条目列表（容错平台的多层包装）。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for nested in ("items", "list", "actors", "participants"):
            inner = raw.get(nested)
            if isinstance(inner, list):
                return inner
    return []


def _collect_actor_sections(
    platform_config: dict[str, Any],
) -> tuple[list[Any], list[str]]:
    """汇总全部参与者条目（兼容 ``_ACTOR_CONTAINER_KEYS`` 别名键）。

    同一条目（完全相同）在多个别名键重复出现时只取一次，避免平台同时下发
    ``actors`` 与 ``traffic_participants`` 时重复生成参与者。

    Args:
        platform_config: 平台 ``scene_config``。

    Returns:
        ``(条目列表, 命中来源摘要)``；来源摘要形如 ``["actors=1", "pedestrians=2"]``。
    """
    sections: list[Any] = []
    sources: list[str] = []
    seen: set[str] = set()
    for key in _ACTOR_CONTAINER_KEYS:
        items = _actor_entries(platform_config.get(key))
        if not items:
            continue
        sources.append(f"{key}={len(items)}")
        for item in items:
            fingerprint = repr(item)
            if fingerprint in seen:
                logger.warning(
                    f"Duplicate participant entry in scene_config ignored (key={key})"
                )
                continue
            seen.add(fingerprint)
            sections.append(item)
    return sections, sources


def _log_participants(
    platform_config: dict[str, Any],
    actor_sections: list[Any],
    participants: list[TrafficParticipantConfig],
    sources: list[str],
) -> None:
    """输出参与者映射汇总，现场可直接判定「平台没下发」还是「字段不匹配」。"""
    if not participants:
        logger.warning(
            "Scene participants mapped: count=0; searched keys="
            f"{list(_ACTOR_CONTAINER_KEYS)} scene_config keys="
            f"{sorted(platform_config.keys())}；平台未下发任何交通参与者"
        )
        return
    type_counts: dict[str, int] = {}
    behavior_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    detail: list[str] = []
    for participant in participants:
        type_counts[participant.actor_type] = type_counts.get(participant.actor_type, 0) + 1
        behavior = str(getattr(participant.behavior, "value", participant.behavior))
        behavior_counts[behavior] = behavior_counts.get(behavior, 0) + 1
        source = participant.source_actor_type or "<missing>"
        source_counts[source] = source_counts.get(source, 0) + 1
        detail.append(f"{participant.participant_id}[{participant.actor_type}/{behavior}]")
    logger.info(
        f"Scene participants mapped: count={len(participants)} "
        f"sources={sources or ['none']} source_types={source_counts} "
        f"types={type_counts} behaviors={behavior_counts} detail={detail}"
    )
    declared_pedestrians = sum(
        1
        for raw in actor_sections
        if str(_as_dict(raw).get("type") or "").strip().lower() == "pedestrian"
    )
    if declared_pedestrians and "walker" not in type_counts:
        logger.warning(
            "Scene participants mapped: 平台声明了 %d 个 pedestrian，但映射后没有任何 "
            "walker；鬼探头/儿童追球类场景看不到行人请核对 type 字段",
            declared_pedestrians,
        )


def _map_actor(
    actor: Any, index: int, report: _MappingReport
) -> TrafficParticipantConfig:
    """映射单个交通参与者。

    平台未声明 ``type`` 但行为是行人横穿类（鬼探头/儿童追球等）时，按 ``walker``
    处理：否则会生成一辆「横穿马路的特斯拉」，可视化里看不到行人。
    """
    section = _as_dict(actor)
    actor_id = str(section.get("actor_id") or f"actor-{index}")
    behavior_section = _as_dict(section.get("behavior"))
    parameters = _as_dict(behavior_section.get("parameters"))
    raw_type = str(section.get("type") or "").strip()
    actor_type = _actor_type(raw_type, parameters, report, actor_id)
    behavior_type = str(behavior_section.get("behavior_type") or "static").strip()
    behavior = _BEHAVIOR_MAP.get(behavior_type)
    if behavior is None:
        raise SceneMappingError(
            f"unsupported_behavior:{behavior_type}",
            f"参与者 {actor_id} 的行为不受支持",
        )
    if behavior is ScenarioBehaviorType.PEDESTRIAN_CROSS and not raw_type:
        actor_type = "walker"
        logger.info(
            f"Participant {actor_id} has pedestrian behavior '{behavior_type}' "
            f"but no type; mapped as walker"
        )
    if behavior_type in _BEHAVIOR_APPROX:
        report.warn(
            f"behavior_approx:{behavior_type}",
            _BEHAVIOR_APPROX[behavior_type],
            participant_id=actor_id,
        )

    vehicle_kind = _norm_kind(
        parameters.get("vehicle_kind"), report=report, actor_id=actor_id
    )
    object_kind = _norm_kind(
        parameters.get("object_kind"), report=report, actor_id=actor_id
    )
    animal_kind = _norm_kind(
        parameters.get("animal_kind"), report=report, actor_id=actor_id
    )

    default_speed = (
        0.0 if behavior is ScenarioBehaviorType.STATIC
        else _DEFAULT_SPEED.get(actor_type, 5.0)
    )
    speed = _first_float(
        parameters,
        _SPEED_KEYS,
        default=default_speed,
        report=report,
        owner=actor_id,
    )
    if (
        actor_type == "walker"
        and behavior is not ScenarioBehaviorType.STATIC
        and speed > 0
    ):
        if not (_WALKER_MIN_SPEED <= speed <= _WALKER_MAX_SPEED):
            report.warn(
                f"speed_out_of_range:{speed:g}",
                f"行人速度按 [{_WALKER_MIN_SPEED}, {_WALKER_MAX_SPEED}] m/s 夹取",
                participant_id=actor_id,
            )
        speed = max(_WALKER_MIN_SPEED, min(_WALKER_MAX_SPEED, speed))

    blueprint, candidates = _participant_blueprint_spec(
        actor_type, parameters, object_kind, report, actor_id
    )
    target_distance = _first_float(
        parameters,
        ("target_dist", "target_distance", "distance"),
        default=math.nan,
        report=report,
        owner=actor_id,
    )
    trigger_section = parameters.get("trigger")
    trigger = (
        _map_trigger(
            _as_dict(trigger_section),
            report=report,
            owner=actor_id,
            participant_id=actor_id,
        )
        if trigger_section
        else None
    )
    behavior_params = {
        key: value for key, value in parameters.items() if key != "trigger"
    }
    return TrafficParticipantConfig(
        participant_id=actor_id,
        actor_type=actor_type,
        blueprint=blueprint,
        spawn_point=_spawn_point(section.get("spawn_point"), f"actors[{index}].spawn_point"),
        behavior=behavior,
        speed_ms=_clamp(speed, 0.0, 30.0),
        behavior_trigger=trigger,
        is_essential=bool(parameters.get("is_essential", False)),
        source_actor_type=raw_type,
        source_behavior_type=behavior_type,
        object_kind=object_kind,
        vehicle_kind=vehicle_kind,
        animal_kind=animal_kind,
        blueprint_candidates=candidates,
        behavior_params=behavior_params,
        target_distance_m=None if math.isnan(target_distance) else target_distance,
    )


def _participant_blueprint_spec(
    actor_type: str,
    parameters: dict[str, Any],
    object_kind: str,
    report: _MappingReport,
    actor_id: str,
) -> tuple[str, list[str]]:
    """按 actor 类型/object_kind 生成主蓝图与回退候选链（实际可用性由执行端判断）。"""
    explicit = str(parameters.get("blueprint") or "").strip()
    candidates: list[str] = []

    if actor_type == "walker":
        blueprint = explicit or "walker.pedestrian.0001"
        candidates = ["walker.pedestrian.0001", "walker.pedestrian.*"]
    elif actor_type == "cyclist":
        blueprint = explicit or "vehicle.bh.crossbike"
        candidates = [
            blueprint,
            "vehicle.diamondback.century",
            "vehicle.gazelle.omafiets",
            "vehicle.*",
        ]
    elif actor_type == "prop":
        if object_kind in _VEHICLE_LIKE_PROP_KINDS:
            blueprint = explicit or "vehicle.tesla.model3"
            candidates = [blueprint, "vehicle.*"]
        else:
            chain = _PROP_BLUEPRINT_CHAINS.get(object_kind)
            if chain is None:
                if object_kind:
                    report.warn(
                        f"object_kind_unknown:{object_kind}",
                        "未识别的 object_kind，按锥桶/方盒近似",
                        participant_id=actor_id,
                    )
                chain = _DEFAULT_PROP_CHAIN
            elif object_kind in _DEGRADED_PROP_KINDS:
                report.warn(
                    f"prop_asset_degraded:{object_kind}",
                    "CARLA 0.9.16 无专用资产，按方盒/锥桶近似",
                    participant_id=actor_id,
                )
            blueprint = explicit or chain[0]
            candidates = list(chain)
    else:
        blueprint = explicit or "vehicle.tesla.model3"
        candidates = ["vehicle.tesla.model3", "vehicle.*"]

    ordered: list[str] = []
    for candidate in [blueprint, *candidates]:
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return blueprint, ordered[1:]


def _map_event(
    event: Any, index: int, report: _MappingReport
) -> SceneEventDefinition:
    """映射单个场景事件（仅用于评估/事件检测定义）。"""
    section = _as_dict(event)
    event_id = str(section.get("event_id") or f"event-{index}")
    raw_type = str(section.get("type") or "custom").strip()
    event_type = _EVENT_TYPE_MAP.get(raw_type)
    if event_type is None:
        report.warn(
            f"event_type_unknown:{raw_type}", "按 custom 处理", event_id=event_id
        )
        event_type = SceneEventType.CUSTOM
    trigger = _map_trigger(
        _as_dict(section.get("trigger")),
        report=report,
        owner=event_id,
        event_id=event_id,
    )
    action = _as_dict(section.get("action"))
    return SceneEventDefinition(
        event_id=event_id,
        event_type=event_type,
        trigger=trigger,
        description=str(section.get("description") or raw_type),
        is_failure_condition=event_type in _FAILURE_EVENT_TYPES,
        action_type=str(action.get("action_type") or ""),
        action_parameters=_as_dict(action.get("parameters")),
        is_runtime_evaluable=trigger.trigger_type is not TriggerType.DISABLED,
    )


def _map_trigger(
    section: dict[str, Any],
    *,
    report: _MappingReport,
    owner: str = "",
    participant_id: str = "",
    event_id: str = "",
) -> SceneEventTrigger:
    """映射触发条件；缺条件/未知条件/值不可解析一律 ``DISABLED``，绝不回落 TIME。"""
    raw_type = str(section.get("condition_type") or "").strip().lower()
    parameters = _as_dict(section.get("parameters"))

    def _disabled(code: str, detail: str) -> SceneEventTrigger:
        report.warn(
            code,
            detail,
            participant_id=participant_id,
            event_id=event_id,
        )
        return SceneEventTrigger(
            trigger_type=TriggerType.DISABLED,
            source_condition_type=raw_type,
        )

    if not raw_type:
        return _disabled(
            "condition_missing",
            "平台未下发 condition_type（旧载荷或描述性事件），本事件不参与运行时评估",
        )
    trigger_type = _CONDITION_TYPE_MAP.get(raw_type)
    if trigger_type is None:
        return _disabled(
            f"condition_unknown:{raw_type}",
            "未知触发条件，禁止回落 TIME",
        )

    # EVENT（collision）由碰撞传感器驱动，平台只给人类可读 condition，
    # 不要求数值 value；不能因为缺 value 把它禁用。
    if trigger_type is TriggerType.EVENT:
        return SceneEventTrigger(
            trigger_type=TriggerType.EVENT,
            source_condition_type=raw_type,
            event_name=str(section.get("condition") or ""),
        )

    default_value = 0.1 if raw_type == "standstill" else math.nan
    value = _first_float(
        parameters,
        ("value", "threshold", "time", "distance", "speed", "sim_time"),
        default=default_value,
        report=report,
        owner=owner,
    )
    if math.isnan(value):
        return _disabled(
            f"condition_value_unresolved:{raw_type}",
            f"trigger.parameters={parameters}",
        )

    comparator = str(parameters.get("rule") or ("<" if raw_type == "standstill" else ">")).strip()
    if comparator not in _COMPARATORS:
        report.warn(f"condition_rule_unknown:{comparator}", "按 '>' 处理")
        comparator = ">"
    relation = str(parameters.get("relative_distance_type") or "").strip().lower()
    if relation not in _RELATIONS:
        report.warn(f"condition_relation_unknown:{relation}", "按 euclidean 处理")
        relation = ""
    if raw_type in _CONDITION_APPROX:
        report.warn(
            f"condition_approx:{raw_type}",
            _CONDITION_APPROX[raw_type],
            participant_id=participant_id,
            event_id=event_id,
        )

    return SceneEventTrigger(
        trigger_type=trigger_type,
        value=_clamp(value, 0.0, 1_000_000.0),
        comparator=comparator,
        relation=relation,
        source_condition_type=raw_type,
        is_approximate=raw_type in _CONDITION_APPROX,
        event_name="",
    )


def _map_sensors(raw: Any) -> list[SensorSpec]:
    """平台 ``sensors`` 段 → 内部 ``SensorSpec`` 列表（V3.0.0）。

    平台可下发两种形式：

    - 省略 / 空值：默认挂 HUNTER SE 三件套（不改变旧场景行为）；
    - 列表：每项至少含 ``profile``，可选 ``enabled`` / ``ros_enabled`` / ``record``。

    未知剖面名不在此拦截，由 ``profiles.get_profile`` 回退默认剖面并记日志，
    避免平台新剖面先于仿真端版本时阻断整场景。

    Args:
        raw: 平台 ``scene_config.sensors`` 原值。

    Returns:
        传感器规格列表（至少一项）。
    """
    items = _as_list(raw)
    if not items:
        return [SensorSpec()]
    specs: list[SensorSpec] = []
    for item in items:
        section = _as_dict(item)
        profile = str(section.get("profile") or "").strip()
        if not profile:
            # 分条声明但未指定剖面：跳过该条，不静默猜测型号
            continue
        specs.append(
            SensorSpec(
                profile=profile,
                enabled=_as_bool(section.get("enabled"), True),
                ros_enabled=_as_bool(section.get("ros_enabled"), False),
                record=_as_bool(section.get("record"), True),
            )
        )
    return specs or [SensorSpec()]


def _map_weather(section: dict[str, Any]) -> WeatherConfig:
    """平台天气字段 → 内部天气字段。"""
    return WeatherConfig(
        cloudiness=_clamp(_as_float(section.get("cloudiness"), 15.0), 0.0, 100.0),
        precipitation=_clamp(_as_float(section.get("rain"), 0.0), 0.0, 100.0),
        precipitation_deposits=_clamp(_as_float(section.get("wetness"), 0.0), 0.0, 100.0),
        wind_intensity=_clamp(_as_float(section.get("wind"), 10.0), 0.0, 100.0),
        sun_azimuth_angle=_clamp(_as_float(section.get("sun_azimuth"), 0.0), 0.0, 360.0),
        sun_altitude_angle=_clamp(_as_float(section.get("sun_altitude"), 45.0), -90.0, 90.0),
        fog_density=_clamp(_as_float(section.get("fog"), 0.0), 0.0, 100.0),
    )


def _map_type(map_section: dict[str, Any], map_id: str) -> str:
    """规范化地图类型；缺省时按 CARLA 内置城镇命名推断。

    平台 V1.25.0 恒带 ``map_type``；旧载荷缺失时，``Town*`` 地图仍按
    ``carla_town`` 处理，才能触发仿真端「占位原点 → 合法出生点」兜底。
    """
    raw = str(map_section.get("map_type") or "").strip().lower()
    if raw:
        return raw
    return "carla_town" if map_id.strip().lower().startswith("town") else ""


def _spawn_semantics(raw: Any) -> str:
    """规范化出生点语义；缺省按 V1.25.0 契约取 ``relative_to_ego``。"""
    value = str(raw or "relative_to_ego").strip().lower()
    return "absolute" if value == "absolute" else "relative_to_ego"


def _spawn_point(section: Any, label: str) -> SpawnPoint:
    """平台 spawn_point（yaw 为弧度）→ 内部 SpawnPoint（度）。"""
    data = _as_dict(section)
    if not data:
        raise SceneMappingError(f"invalid_scene_config:{label}", "缺少生成点")
    try:
        x = _as_float(data.get("x"))
        y = _as_float(data.get("y"))
    except (TypeError, ValueError) as exc:
        raise SceneMappingError(f"invalid_scene_config:{label}", str(exc)) from exc
    yaw_rad = _as_float(data.get("yaw"), 0.0)
    lane_id: Optional[str] = None
    lane_index: Optional[int] = None
    raw_lane = data.get("lane_id")
    if raw_lane is not None and str(raw_lane).strip():
        lane_id = str(raw_lane).strip()
        try:
            lane_index = int(float(lane_id))
        except (TypeError, ValueError):
            # "lane-1" 是旧平台占位值，不是真实车道号；只保留原值，不参与选道
            lane_index = None
            logger.debug("lane_id_unusable:%s (%s)", lane_id, label)
    return SpawnPoint(
        x=x,
        y=y,
        z=_as_float(data.get("z"), 0.0),
        yaw_deg=math.degrees(yaw_rad),
        lane_id=lane_id,
        lane_index=lane_index,
    )


def _normalize_blueprint(value: str, default: str = "hunter_se") -> str:
    """归一化平台模型名（大小写/连字符），完整蓝图名保持小写。"""
    text = (value or "").strip()
    if not text:
        return default
    if "." in text:
        return text.lower()
    return text.lower().replace("-", "_")


def _norm_kind(
    value: Any,
    *,
    report: Optional[_MappingReport] = None,
    actor_id: str = "",
) -> str:
    """归一化平台 kind 取值（大小写/连字符/旧别名），并记录变更。"""
    raw = str(value or "").strip().lower().replace("-", "_")
    normalized = _KIND_ALIASES.get(raw, raw)
    if report is not None and raw and raw != normalized:
        report.warn(
            f"object_kind_normalized:{raw}->{normalized}",
            "旧别名/连字符已归一化",
            participant_id=actor_id,
        )
    return normalized


def _actor_type(
    raw_type: str,
    parameters: dict[str, Any],
    report: _MappingReport,
    actor_id: str,
) -> str:
    """平台 ActorType + kind → 内部 actor_type（vehicle/walker/cyclist/prop）。"""
    value = str(raw_type or "vehicle").strip().lower()
    if value == "pedestrian":
        return "walker"
    if value in ("walker", "cyclist", "prop"):
        return value

    vehicle_kind = _norm_kind(parameters.get("vehicle_kind"))
    object_kind = _norm_kind(parameters.get("object_kind"))
    animal_kind = _norm_kind(parameters.get("animal_kind"))

    if vehicle_kind in _CYCLIST_VEHICLE_KINDS:
        return "cyclist"
    if value == "vehicle":
        return "vehicle"
    if value == "other":
        if animal_kind or object_kind in _ANIMAL_KINDS:
            report.warn(
                "animal_asset_missing",
                f"object_kind={object_kind or animal_kind}",
                participant_id=actor_id,
            )
            # CARLA 0.9.16 无动物资产：白名单外观 + 报告标注
            return "walker"
        if object_kind:
            return "prop"
        report.warn(
            "actor_type_legacy_other",
            "type=other 但无 object_kind/vehicle_kind/animal_kind，按旧行为映射为 walker",
            participant_id=actor_id,
        )
        return "walker"

    report.warn(
        f"actor_type_unknown:{value}",
        "未识别的参与者 type，按 vehicle 处理",
        participant_id=actor_id,
    )
    return "vehicle"


def _quality(raw: Optional[str]) -> QualityLevel:
    """平台画质（Low/Medium/High/Epic）→ 内部画质。"""
    value = (raw or "medium").strip().lower()
    if value == "high":
        return QualityLevel.EPIC
    if value == "epic":
        return QualityLevel.EPIC
    if value == "low":
        return QualityLevel.LOW
    return QualityLevel.MEDIUM


def _first_float(
    data: dict[str, Any],
    keys: tuple[str, ...],
    default: float,
    *,
    report: Optional[_MappingReport] = None,
    owner: str = "",
) -> float:
    """按顺序读取第一个可转换为浮点的字段。

    参数值若仍是变量名/表达式（平台 A 缺陷），不静默当作 0，而是记录
    ``unresolved_param`` 并继续找下一个键。
    """
    for key in keys:
        if key not in data:
            continue
        raw = data[key]
        try:
            return float(raw)
        except (TypeError, ValueError):
            if isinstance(raw, str) and raw.strip() and report is not None:
                report.warn(
                    f"unresolved_param:{key}={raw}",
                    "参数值仍是未解析的变量名/表达式，已使用默认值",
                    participant_id=owner,
                )
            continue
    return default


def _build_mapping_report(
    scene_id: str,
    platform_config: dict[str, Any],
    actor_sections: list[Any],
    participants: list[TrafficParticipantConfig],
    events: list[SceneEventDefinition],
    report: _MappingReport,
) -> dict[str, Any]:
    """汇总映射结果与告警，写入 ``extra_params["mapping_report"]``。"""
    return {
        "scene_id": scene_id,
        "platform_version": str(platform_config.get("version") or ""),
        "actors": {
            "declared": len(actor_sections),
            "mapped": len(participants),
            "by_source_type": _count_by(actor_sections, "type"),
            "by_actor_type": _count_by(participants, "actor_type"),
            "by_behavior": _count_by(participants, lambda p: p.behavior.value),
        },
        "events": {
            "total": len(events),
            "runtime_evaluable": sum(1 for event in events if event.is_runtime_evaluable),
            "disabled": sum(1 for event in events if not event.is_runtime_evaluable),
            "by_condition": _count_by(
                events, lambda e: e.trigger.source_condition_type or "<missing>"
            ),
        },
        "warnings": report.warnings,
        "warning_counts": _warning_counts(report.warnings),
    }


def _count_by(items: list[Any], key: Any) -> dict[str, int]:
    """按属性名/字典键/可调用对象计数（空值统一记为 ``<missing>``）。"""
    counts: dict[str, int] = {}
    for item in items:
        if callable(key):
            raw = key(item)
        elif isinstance(item, dict):
            raw = item.get(key)
        else:
            raw = getattr(item, key, None)
        value = str(raw) if raw not in (None, "") else "<missing>"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _warning_counts(warnings: list[dict[str, Any]]) -> dict[str, int]:
    """按告警码聚合计数（码中带 ``:<detail>`` 的部分归并到前缀）。"""
    counts: dict[str, int] = {}
    for warning in warnings:
        code = str(warning.get("code") or "<unknown>")
        prefix = code.split(":", 1)[0]
        counts[prefix] = counts.get(prefix, 0) + 1
    return counts


def _as_float(value: Any, default: float = 0.0) -> float:
    """安全转 float。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    """安全转 dict。"""
    return value if isinstance(value, dict) else {}


def _as_bool(value: Any, default: bool) -> bool:
    """安全转 bool（字符串/数字按常见写法解析，非法值回退默认）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    return default


def _as_list(value: Any) -> list[Any]:
    """安全转 list。"""
    return value if isinstance(value, list) else []


def _clamp(value: float, low: float, high: float) -> float:
    """区间截断。"""
    return max(low, min(high, value))


__all__ = [
    "SceneMappingError",
    "map_calibration",
    "map_scene_config",
    "resolve_mode",
]
