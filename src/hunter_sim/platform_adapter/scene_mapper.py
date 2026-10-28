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
from typing import Any, Optional

from hunter_sim.common.models import QualityLevel, SimMode
from hunter_sim.engine.coordinate_converter import CalibrationParams
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
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
    "cross_road": ScenarioBehaviorType.CONSTANT_SPEED,
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
    # 切入/切出
    "cut_in": ScenarioBehaviorType.CUT_IN,
    "aggressive_merge": ScenarioBehaviorType.CUT_IN,
    "cut_out": ScenarioBehaviorType.CUT_OUT,
    # 行人横穿
    "pedestrian_crossing": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    "sudden_cross": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    "child_chase_ball": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    "occluded_dash": ScenarioBehaviorType.PEDESTRIAN_CROSS,
    # 兼容内部取值
    "constant_speed": ScenarioBehaviorType.CONSTANT_SPEED,
    "stop_and_go": ScenarioBehaviorType.STOP_AND_GO,
    "pedestrian_cross": ScenarioBehaviorType.PEDESTRIAN_CROSS,
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
    "traveled_distance": TriggerType.DISTANCE,
    "speed": TriggerType.VELOCITY,
    "velocity": TriggerType.VELOCITY,
    "collision": TriggerType.EVENT,
    "time_to_collision": TriggerType.EVENT,
    "time_headway": TriggerType.EVENT,
    "event": TriggerType.EVENT,
}

_FAILURE_EVENT_TYPES = frozenset(
    {SceneEventType.COLLISION, SceneEventType.EMERGENCY_BRAKE}
)


class SceneMappingError(ValueError):
    """场景映射失败；``code`` 会写入实例 ``message``。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}{': ' + detail if detail else ''}")
        self.code = code
        self.detail = detail


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

    map_section = _as_dict(platform_config.get("map"))
    if not map_section:
        raise SceneMappingError("invalid_scene_config:map", "缺少 map 段")
    map_id = str(map_section.get("map_id") or "").strip()
    if not map_id:
        raise SceneMappingError("invalid_scene_config:map_id", "map.map_id 为空")

    ego_section = _as_dict(platform_config.get("ego_vehicle"))
    ego_spawn = _spawn_point(map_section.get("spawn_point"), "map.spawn_point")
    ego_blueprint = str(ego_section.get("model") or "hunter_se").strip() or "hunter_se"
    initial_speed = _clamp(_as_float(ego_section.get("initial_speed"), 0.0), 0.0, 50.0)

    duration = _as_float(platform_config.get("duration"), default_duration_seconds)
    if duration <= 0:
        duration = default_duration_seconds
    if duration > max_duration_seconds:
        raise SceneMappingError(
            "invalid_scene_config:duration",
            f"duration={duration}s 超过上限 {max_duration_seconds}s",
        )

    participants = [
        _map_actor(actor, index)
        for index, actor in enumerate(_as_list(platform_config.get("actors")))
    ]
    events = [
        _map_event(event, index)
        for index, event in enumerate(_as_list(platform_config.get("events")))
    ]

    success_criteria = _as_dict(platform_config.get("success_criteria"))
    return SceneConfig(
        scene_id=scene_id,
        scene_name=scene_name,
        map_id=map_id,
        quality=_quality(quality),
        mode=mode,
        ego_vehicle=EgoVehicleConfig(
            vehicle_blueprint=ego_blueprint,
            spawn_point=ego_spawn,
            # VIL 由实车位姿驱动，不能启用 autopilot（否则与 set_transform 争夺控制权）
            autopilot=mode is SimMode.SIL,
            initial_speed_ms=initial_speed,
        ),
        weather=_map_weather(_as_dict(platform_config.get("weather"))),
        traffic_participants=participants,
        sensors=_map_sensors(platform_config.get("sensors")),
        scene_events=events,
        duration_seconds=duration,
        timeout_seconds=max(60.0, duration * 3.0),
        extra_params={
            "success_criteria": success_criteria,
            "platform_scene_config": {
                "map": map_section,
                "success_criteria": success_criteria,
            },
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


def _map_actor(actor: Any, index: int) -> TrafficParticipantConfig:
    """映射单个交通参与者。"""
    section = _as_dict(actor)
    actor_id = str(section.get("actor_id") or f"actor-{index}")
    actor_type = _actor_type(section.get("type"))
    behavior_section = _as_dict(section.get("behavior"))
    behavior_type = str(behavior_section.get("behavior_type") or "static").strip()
    behavior = _BEHAVIOR_MAP.get(behavior_type)
    if behavior is None:
        raise SceneMappingError(
            f"unsupported_behavior:{behavior_type}",
            f"参与者 {actor_id} 的行为不受支持",
        )
    parameters = _as_dict(behavior_section.get("parameters"))
    speed = _first_float(
        parameters,
        ("speed", "speed_ms", "target_speed", "target_speed_ms", "velocity"),
        default=0.0 if behavior is ScenarioBehaviorType.STATIC else 5.0,
    )
    blueprint = str(
        parameters.get("blueprint")
        or ("walker.pedestrian.0001" if actor_type == "walker" else "vehicle.tesla.model3")
    )
    trigger = _map_trigger(_as_dict(parameters.get("trigger"))) if parameters.get("trigger") else None
    return TrafficParticipantConfig(
        participant_id=actor_id,
        actor_type=actor_type,
        blueprint=blueprint,
        spawn_point=_spawn_point(section.get("spawn_point"), f"actors[{index}].spawn_point"),
        behavior=behavior,
        speed_ms=_clamp(speed, 0.0, 30.0),
        behavior_trigger=trigger,
        is_essential=bool(parameters.get("is_essential", False)),
    )


def _map_event(event: Any, index: int) -> SceneEventDefinition:
    """映射单个场景事件（仅用于评估/事件检测定义）。"""
    section = _as_dict(event)
    event_id = str(section.get("event_id") or f"event-{index}")
    raw_type = str(section.get("type") or "custom").strip()
    event_type = _EVENT_TYPE_MAP.get(raw_type, SceneEventType.CUSTOM)
    trigger = _map_trigger(_as_dict(section.get("trigger")))
    return SceneEventDefinition(
        event_id=event_id,
        event_type=event_type,
        trigger=trigger,
        description=str(section.get("description") or raw_type),
        is_failure_condition=event_type in _FAILURE_EVENT_TYPES,
    )


def _map_trigger(section: dict[str, Any]) -> SceneEventTrigger:
    """映射触发条件。"""
    condition = str(section.get("condition") or "")
    condition_type = str(section.get("condition_type") or "time").strip().lower()
    trigger_type = _CONDITION_TYPE_MAP.get(condition_type, TriggerType.TIME)
    parameters = _as_dict(section.get("parameters"))
    value = _first_float(
        parameters,
        ("value", "threshold", "time", "distance", "speed", "sim_time"),
        default=0.0,
    )
    return SceneEventTrigger(
        trigger_type=trigger_type,
        value=_clamp(value, 0.0, 1_000_000.0),
        event_name=condition if trigger_type is TriggerType.EVENT else "",
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
    return SpawnPoint(
        x=x,
        y=y,
        z=_as_float(data.get("z"), 0.0),
        yaw_deg=math.degrees(yaw_rad),
    )


def _actor_type(raw: Any) -> str:
    """平台 ActorType → 内部 actor_type。"""
    value = str(raw or "vehicle").strip().lower()
    if value == "pedestrian":
        return "walker"
    if value == "vehicle":
        return "vehicle"
    if value == "other":
        return "walker"
    if value in ("walker", "cyclist"):
        return value
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


def _first_float(data: dict[str, Any], keys: tuple[str, ...], default: float) -> float:
    """按顺序读取第一个可转换为浮点的字段。"""
    for key in keys:
        if key in data:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    return default


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
