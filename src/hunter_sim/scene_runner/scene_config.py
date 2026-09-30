"""场景配置数据模型（PROMPT-ENG-003-A）。

定义平台下发的场景配置 JSON 结构，使用 pydantic v2 进行校验。
场景配置包含：地图、天气、自车生成点、交通参与者、场景事件。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from hunter_sim.common.models import QualityLevel, SimMode, SensorType, Transform


class SpawnPoint(BaseModel):
    """自车生成点配置。

    Attributes:
        x: CARLA 地图 X 坐标（米）。
        y: CARLA 地图 Y 坐标（米）。
        z: 高度（米，0 表示由地图决定）。
        yaw_deg: 航向角（度，北向顺时针）。
        roll_deg: 横滚角（度）。
        pitch_deg: 俯仰角（度）。
    """

    x: float
    y: float
    z: float = 0.0
    yaw_deg: float = Field(0.0, ge=-360.0, le=360.0)
    roll_deg: float = Field(0.0, ge=-180.0, le=180.0)
    pitch_deg: float = Field(0.0, ge=-90.0, le=90.0)

    def to_transform(self) -> Transform:
        """转换为内部 Transform（弧度制）。"""
        import math
        return Transform(
            x=self.x, y=self.y, z=self.z,
            yaw=math.radians(self.yaw_deg),
            pitch=math.radians(self.pitch_deg),
            roll=math.radians(self.roll_deg),
        )


class EgoVehicleConfig(BaseModel):
    """自车配置。

    Attributes:
        vehicle_blueprint: CARLA 车辆蓝图名称（默认使用 HUNTER SE）。
        spawn_point: 生成点坐标。
        autopilot: 是否启用自动驾驶（SIL 模式用）。
        sensors: 需要挂载的传感器类型列表。
        initial_speed_ms: 初始速度（m/s）。
    """

    vehicle_blueprint: str = Field("hunter_se", description="车辆蓝图")
    spawn_point: SpawnPoint
    autopilot: bool = False
    sensors: list[SensorType] = Field(
        default=[SensorType.LIDAR, SensorType.RGB_CAMERA, SensorType.IMU],
        description="挂载传感器列表",
    )
    initial_speed_ms: float = Field(0.0, ge=0.0, le=50.0)


class WeatherConfig(BaseModel):
    """场景天气配置。

    Attributes:
        preset_name: 预设环境名称（空字符串表示自定义）。
        cloudiness: 云量 (0-100)。
        precipitation: 降雨量 (0-100)。
        sun_altitude_angle: 太阳高度角（度）。
        fog_density: 雾浓度 (0-100)。
    """

    preset_name: str = Field("", description="预设环境名称")
    cloudiness: float = Field(15.0, ge=0.0, le=100.0)
    precipitation: float = Field(0.0, ge=0.0, le=100.0)
    precipitation_deposits: float = Field(0.0, ge=0.0, le=100.0)
    wind_intensity: float = Field(10.0, ge=0.0, le=100.0)
    sun_azimuth_angle: float = Field(0.0, ge=0.0, le=360.0)
    sun_altitude_angle: float = Field(45.0, ge=-90.0, le=90.0)
    fog_density: float = Field(0.0, ge=0.0, le=100.0)
    fog_distance: float = Field(0.0, ge=0.0)


class TriggerType(str, Enum):
    """事件触发类型。"""

    TIME = "time"
    DISTANCE = "distance"
    POSITION = "position"
    VELOCITY = "velocity"
    EVENT = "event"


class SceneEventTrigger(BaseModel):
    """场景事件触发条件。

    Attributes:
        trigger_type: 触发类型。
        value: 触发阈值（时间秒/距离米/坐标/速度 m/s）。
        event_name: 关联的事件名称（trigger_type=EVENT 时使用）。
    """

    trigger_type: TriggerType
    value: float = Field(0.0, ge=0.0)
    event_name: str = ""


class ScenarioBehaviorType(str, Enum):
    """交通参与者行为类型。"""

    CONSTANT_SPEED = "constant_speed"
    DECELERATE = "decelerate"
    ACCELERATE = "accelerate"
    CUT_IN = "cut_in"
    CUT_OUT = "cut_out"
    STOP_AND_GO = "stop_and_go"
    PEDESTRIAN_CROSS = "pedestrian_cross"
    STATIC = "static"


class TrafficParticipantConfig(BaseModel):
    """交通参与者配置。

    Attributes:
        participant_id: 参与者唯一标识（场景内）。
        actor_type: Actor 类型（vehicle/walker/cyclist）。
        blueprint: CARLA 蓝图名称。
        spawn_point: 生成位置。
        behavior: 行为类型。
        speed_ms: 目标速度（m/s）。
        behavior_trigger: 行为触发条件。
        is_essential: 是否为场景关键参与者（影响评估）。
    """

    participant_id: str
    actor_type: Literal["vehicle", "walker", "cyclist"] = "vehicle"
    blueprint: str = "vehicle.tesla.model3"
    spawn_point: SpawnPoint
    behavior: ScenarioBehaviorType = ScenarioBehaviorType.CONSTANT_SPEED
    speed_ms: float = Field(5.0, ge=0.0, le=30.0)
    behavior_trigger: Optional[SceneEventTrigger] = None
    is_essential: bool = False


class SceneEventType(str, Enum):
    """场景事件类型。"""

    COLLISION = "collision"
    LANE_INVASION = "lane_invasion"
    EMERGENCY_BRAKE = "emergency_brake"
    SPEED_VIOLATION = "speed_violation"
    RED_LIGHT = "red_light"
    OBSTACLE_AVOID = "obstacle_avoid"
    CUSTOM = "custom"


class SceneEventDefinition(BaseModel):
    """场景评估事件定义。

    Attributes:
        event_id: 事件 ID。
        event_type: 事件类型。
        trigger: 触发条件。
        description: 事件描述字符串。
        is_failure_condition: 是否判定为失败事件。
    """

    event_id: str
    event_type: SceneEventType
    trigger: SceneEventTrigger
    description: str = ""
    is_failure_condition: bool = True


class SceneConfig(BaseModel):
    """完整场景配置模型（平台下发 JSON 解析后的结构）。

    Attributes:
        scene_id: 场景唯一 ID。
        scene_name: 场景显示名称。
        scene_version: 配置版本号。
        map_id: 地图 ID。
        quality: 渲染画质等级。
        mode: 仿真模式（VIL/SIL）。
        ego_vehicle: 自车配置。
        weather: 天气配置。
        traffic_participants: 交通参与者列表。
        scene_events: 场景评估事件定义列表。
        duration_seconds: 场景最大运行时长（秒）。
        timeout_seconds: 超时自动结束时间（秒）。
        tags: 场景标签列表（用于分类检索）。
        extra_params: 扩展参数字典（自定义场景脚本可用）。
    """

    scene_id: str
    scene_name: str = ""
    scene_version: str = "1.0"
    map_id: str = Field("Town03", description="地图 ID")
    quality: QualityLevel = QualityLevel.MEDIUM
    mode: SimMode = SimMode.SIL
    ego_vehicle: EgoVehicleConfig
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    traffic_participants: list[TrafficParticipantConfig] = Field(default_factory=list)
    scene_events: list[SceneEventDefinition] = Field(default_factory=list)
    duration_seconds: float = Field(60.0, gt=0.0, le=7200.0)
    timeout_seconds: float = Field(7200.0, gt=0.0)
    tags: list[str] = Field(default_factory=list)
    extra_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("map_id")
    @classmethod
    def validate_map_id(cls, v: str) -> str:
        """验证地图 ID 不为空。"""
        if not v.strip():
            raise ValueError("map_id cannot be empty")
        return v.strip()
