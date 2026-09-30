"""API 层 Pydantic 数据模型。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

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


class CreateInstanceRequest(BaseModel):
    """POST /instances 请求体。"""

    mode: SimMode = Field(..., description="仿真模式（vil/sil/replay）")
    map_id: str = Field("Town03", description="地图 ID")
    quality: QualityLevel = Field(QualityLevel.MEDIUM, description="渲染画质")
    vehicle_id: str = Field("", description="关联实车 ID（VIL 模式必填）")
    scene_id: str = Field("", description="关联场景 ID")


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
    """POST /scenes/load 请求体。"""

    instance_id: str = Field(..., description="目标仿真实例 ID")
    scene_config: dict[str, Any] = Field(..., description="场景配置 JSON（SceneConfig 格式）")


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
    """POST /instances/{id}/calibrate 请求体。"""

    x0: float = Field(..., description="CARLA 地图初始 X 坐标（米）")
    y0: float = Field(..., description="CARLA 地图初始 Y 坐标（米）")
    yaw0_deg: float = Field(..., ge=-180.0, le=180.0, description="CARLA 地图初始航向（度）")


class CalibrationResponse(BaseModel):
    """标定结果响应。"""

    x0: float
    y0: float
    yaw0_rad: float
    message: str = "Calibration applied"


# ─── 资源查询 ─────────────────────────────────────────────────────────────────


class MapInfo(BaseModel):
    """地图信息。"""

    map_id: str
    name: str
    is_custom: bool = False


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
    version: str = "2.0.0"
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
