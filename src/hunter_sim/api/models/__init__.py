"""API 层 Pydantic 数据模型。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from hunter_sim.common.models import (
    EvalGrade,
    InstanceStatus,
    QualityLevel,
    SceneStatus,
    SimMode,
)


# ─── 统一响应格式 ─────────────────────────────────────────────────────────────


class ApiError(BaseModel):
    """统一错误响应体。"""

    code: int = Field(..., description="错误码")
    message: str = Field(..., description="错误描述")
    data: Optional[Any] = Field(None, description="额外数据")


class ApiResponse(BaseModel):
    """统一成功响应体。"""

    code: int = Field(0, description="0 表示成功")
    message: str = Field("success")
    data: Optional[Any] = Field(None)


# ─── 实例管理 ─────────────────────────────────────────────────────────────────


class ReplayWindow(BaseModel):
    """replay 模式数据回放时间窗口（设计文档 §12.2）。

    Attributes:
        vehicle_id: 回放实车 ID。
        start_time: 回放起始时间（ISO8601）。
        end_time: 回放结束时间（ISO8601）。
    """

    vehicle_id: str = Field("", description="回放实车 ID")
    start_time: str = Field("", description="回放起始时间（ISO8601）")
    end_time: str = Field("", description="回放结束时间（ISO8601）")


class CreateInstanceRequest(BaseModel):
    """POST /instances 请求体（设计文档 §12.2）。

    兼容旧字段名 `map_id`（与 `map` 等效）。
    """

    model_config = ConfigDict(populate_by_name=True)

    mode: SimMode = Field(..., description="仿真模式（vil/sil/replay）")
    map: str = Field(
        "Town03",
        validation_alias=AliasChoices("map", "map_id"),
        description="地图 ID（如 Town03）",
    )
    vehicle_model: str = Field("hunter.se", description="自车模型 ID")
    quality: QualityLevel = Field(QualityLevel.MEDIUM, description="渲染画质（Low/Medium/Epic，不区分大小写）")
    scene_id: str = Field("", description="可选，预加载场景 ID")
    vehicle_id: str = Field("", description="VIL 模式必填，关联实车 ID")
    replay_config: Optional[ReplayWindow] = Field(None, description="replay 模式回放配置")

    @field_validator("quality", mode="before")
    @classmethod
    def _normalize_quality(cls, v: object) -> object:
        """画质大小写不敏感（文档示例为 Medium）。"""
        return v.lower() if isinstance(v, str) else v


class InstanceResponse(BaseModel):
    """实例信息响应体。"""

    sim_instance_id: str
    status: InstanceStatus
    mode: SimMode
    map_id: str
    quality: QualityLevel
    gpu_id: int
    carla_host: str
    carla_rpc_port: int
    scene_id: str
    vehicle_id: str
    user_id: str
    create_time: float
    start_time: float
    error_message: str = ""


# ─── 场景管理 ─────────────────────────────────────────────────────────────────


class LoadSceneRequest(BaseModel):
    """POST /scenes/load 请求体（内部扩展路径，兼容保留）。"""

    instance_id: str = Field(..., description="目标仿真实例 ID")
    scene_config: dict[str, Any] = Field(..., description="场景配置 JSON（SceneConfig 格式）")


class InstanceSceneRequest(BaseModel):
    """POST /instances/{id}/scene 请求体（设计文档 §12.4）。

    Attributes:
        scene_id: 场景 ID。
        scene_config: 完整场景配置 JSON（见数据采集系统 4.2.2 节）。
    """

    scene_id: str = Field(..., description="场景 ID")
    scene_config: dict[str, Any] = Field(..., description="完整场景配置 JSON")


class BatchScenarioRequest(BaseModel):
    """POST /scenarios/batch 请求体（设计文档 §12.1 / §9.5）。

    Attributes:
        task_id: 任务 ID（空则自动生成）。
        instance_id: 指定执行实例（空由调度器分配）。
        scenes: 场景配置列表。
    """

    task_id: str = Field("", description="批量任务 ID（空自动生成）")
    instance_id: str = Field("", description="执行实例 ID（可选）")
    scenes: list[dict[str, Any]] = Field(..., min_length=1, description="场景配置列表")


class SceneStatusResponse(BaseModel):
    """场景状态响应体。"""

    scene_id: str
    status: SceneStatus
    current_time_s: float = 0.0
    duration_s: float = 0.0
    events: list[dict[str, Any]] = Field(default_factory=list)


# ─── 天气控制 ─────────────────────────────────────────────────────────────────


class SetWeatherRequest(BaseModel):
    """POST /instances/{id}/weather 请求体。"""

    preset: Optional[str] = Field(
        None,
        description="预设环境名称：sunny_noon / cloudy / light_rain / heavy_rain / foggy / night / dusk / dawn",
    )
    cloudiness: Optional[float] = Field(None, ge=0.0, le=100.0, description="云量 0-100")
    precipitation: Optional[float] = Field(None, ge=0.0, le=100.0, description="降雨量 0-100")
    road_wetness: Optional[float] = Field(None, ge=0.0, le=100.0, description="路面积水 0-100")
    wind_intensity: Optional[float] = Field(None, ge=0.0, le=100.0, description="风力 0-100")
    sun_altitude: Optional[float] = Field(None, ge=-90.0, le=90.0, description="太阳高度角（度）")
    sun_azimuth: Optional[float] = Field(None, ge=0.0, le=360.0, description="太阳方位角（度）")
    transition_seconds: float = Field(2.0, ge=0.0, le=30.0, description="渐变过渡时长（秒）")


# ─── VIL 标定 ─────────────────────────────────────────────────────────────────


class CalibrationRequest(BaseModel):
    """POST /instances/{id}/vil/calibrate 请求体（设计文档 §12.3）。

    标定实车 odom 原点在仿真地图中的对应位置和朝向：
    odom 位姿 (odom_x, odom_y, odom_heading) 与地图位姿
    (map_x, map_y, map_heading) 建立映射关系。
    """

    map_x: float = Field(..., description="odom 原点对应的地图 X 坐标（米）")
    map_y: float = Field(..., description="odom 原点对应的地图 Y 坐标（米）")
    map_heading: float = Field(
        ..., ge=-360.0, le=360.0, description="odom 原点对应的地图航向（度）"
    )
    odom_x: float = Field(0.0, description="标定时刻实车 odom X（米，通常为原点 0）")
    odom_y: float = Field(0.0, description="标定时刻实车 odom Y（米）")
    odom_heading: float = Field(0.0, ge=-360.0, le=360.0, description="标定时刻 odom 航向（度）")


class CalibrationResponse(BaseModel):
    """标定结果响应（含推导出的内部变换参数）。"""

    x0: float = Field(..., description="内部标定参数：地图 X 偏移（米）")
    y0: float = Field(..., description="内部标定参数：地图 Y 偏移（米）")
    yaw0_rad: float = Field(..., description="内部标定参数：初始航向（弧度）")
    map_x: float = Field(..., description="回显：地图 X（米）")
    map_y: float = Field(..., description="回显：地图 Y（米）")
    map_heading: float = Field(..., description="回显：地图航向（度）")
    message: str = "Calibration applied"


# ─── 资源查询 ─────────────────────────────────────────────────────────────────


class MapInfo(BaseModel):
    """地图信息。"""

    map_id: str
    name: str
    is_custom: bool = False


class MapUploadRequest(BaseModel):
    """POST /maps/upload 请求体（设计文档 §10.4.2）。

    Attributes:
        map_id: 自定义地图 ID，仅允许字母/数字/下划线/中划线。
        content: OpenDRIVE (.xodr) XML 文本内容。
    """

    map_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]+$", description="地图 ID（防路径穿越）")
    content: str = Field(..., min_length=1, description="OpenDRIVE xodr XML 内容")


class VehicleInfo(BaseModel):
    """车辆蓝图信息。"""

    blueprint_id: str
    display_name: str
    num_wheels: int = 4


class WeatherPresetInfo(BaseModel):
    """天气预设信息。"""

    preset_name: str
    description: str


# ─── 评估 ─────────────────────────────────────────────────────────────────────


class EvaluationSummary(BaseModel):
    """批量评估摘要响应。"""

    overall_grade: EvalGrade
    total_scenes: int
    passed: int
    failed: int
    pass_rate: float
    results: list[dict[str, Any]] = Field(default_factory=list)


# ─── 健康检查 ─────────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """健康检查响应体。"""

    status: str = Field(..., description="healthy / degraded / unhealthy")
    version: str = "2.1.0"
    uptime_seconds: float = 0.0
    carla_connected: bool = False
    active_instances: int = 0


# ─── 认证 ─────────────────────────────────────────────────────────────────────


class TokenRequest(BaseModel):
    """POST /auth/token 请求体。"""

    username: str
    password: str


class TokenResponse(BaseModel):
    """JWT Token 响应。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="过期时间（秒）")
