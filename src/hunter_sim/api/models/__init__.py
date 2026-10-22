"""API 层 Pydantic 数据模型。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

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
    tenant: str = Field("", description="目标租户（V2.5.0，空取调用主体租户；跨租户仅 admin）")

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
    tenant: str = "default"
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

    V2.5.0 支持两种下发方式（二选一）：
    - ``scene_config``：直接提交完整场景配置；
    - ``library_scene_id``：从当前租户的场景库读取已保存配置。

    Attributes:
        scene_id: 场景 ID（可覆盖场景库/配置中的 scene_id）。
        scene_config: 完整场景配置 JSON（见数据采集系统 4.2.2 节）。
        library_scene_id: 场景库条目 ID（与 scene_config 二选一）。
    """

    scene_id: str = Field("", description="场景 ID（可空，取配置中的 scene_id）")
    scene_config: Optional[dict[str, Any]] = Field(None, description="完整场景配置 JSON")
    library_scene_id: str = Field("", description="场景库条目 ID（V2.5.0）")

    @model_validator(mode="after")
    def _require_one_source(self) -> "InstanceSceneRequest":
        """scene_config 与 library_scene_id 必须二选一。"""
        if not self.scene_config and not self.library_scene_id:
            raise ValueError("either scene_config or library_scene_id is required")
        if self.scene_config and self.library_scene_id:
            raise ValueError("scene_config and library_scene_id are mutually exclusive")
        return self


# ─── 平台化（V2.5.0） ──────────────────────────────────────────────────────


class ApiKeyCreateRequest(BaseModel):
    """POST /auth/api-keys 请求体（服务账号签发）。"""

    name: str = Field(..., min_length=1, max_length=64, description="服务账号名称（可读标识）")
    role: str = Field(
        "operator", pattern="^(viewer|operator|admin)$", description="角色（viewer/operator/admin）"
    )
    tenant: str = Field("", description="所属租户（空取调用主体租户）")
    expires_in_days: int = Field(0, ge=0, le=3650, description="有效期天数（0 表示永不过期）")


class SceneLibraryCreateRequest(BaseModel):
    """POST /scenes/library 请求体（场景库保存）。"""

    name: str = Field(..., min_length=1, max_length=128, description="场景名称（同名自动递增版本）")
    description: str = Field("", max_length=512, description="场景说明")
    config: dict[str, Any] = Field(..., description="完整场景配置 JSON（SceneConfig 格式）")
    scene_id: str = Field("", description="场景 ID（空取配置中的 scene_id）")


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
    version: str = ""
    uptime_seconds: float = 0.0
    carla_connected: bool = False
    carla_sdk_installed: bool = Field(False, description="服务解释器是否可导入 carla SDK")
    carla_probe_error: str | None = Field(None, description="最近一次探测失败原因（target + 异常摘要）")
    active_instances: int = 0
    state_persistence: bool = Field(False, description="状态持久化是否启用（V2.4.0）")
    restored_instances: int = Field(0, description="启动时从持久化恢复的实例数")


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


# ─── 多用户体系（V2.8.0 P2-13） ────────────────────────────────────────────


class UserCreateRequest(BaseModel):
    """POST /auth/users 请求体（admin 创建用户）。"""

    username: str = Field(
        ...,
        min_length=3,
        max_length=64,
        pattern=r"^[0-9A-Za-z._-]+$",
        description="登录名（3-64 位，仅限字母/数字/`.`/`_`/`-`）",
    )
    password: str = Field(..., min_length=6, max_length=256, description="初始口令（最短长度受 PLATFORM_MIN_PASSWORD_LENGTH 约束）")
    role: str = Field(
        "operator", pattern="^(viewer|operator|admin)$", description="角色（viewer/operator/admin）"
    )
    tenant: str = Field("", description="所属租户（空取调用主体租户）")


class UserUpdateRequest(BaseModel):
    """PATCH /auth/users/{username} 请求体（字段留空表示不修改）。"""

    role: Optional[str] = Field(None, pattern="^(viewer|operator|admin)$", description="新角色")
    tenant: Optional[str] = Field(None, max_length=64, description="新租户")
    password: Optional[str] = Field(None, min_length=6, max_length=256, description="重置口令（明文，入库仅存摘要）")
    disabled: Optional[bool] = Field(None, description="是否禁用（禁用后不可登录，已发 Token 靠自然过期失效）")


class PasswordChangeRequest(BaseModel):
    """POST /auth/password 请求体（当前登录用户自助改口令）。"""

    old_password: str = Field(..., min_length=1, max_length=256, description="当前口令")
    new_password: str = Field(..., min_length=6, max_length=256, description="新口令（最短长度受 PLATFORM_MIN_PASSWORD_LENGTH 约束）")
