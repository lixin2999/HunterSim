"""场景管理路由（PROMPT-API-001）。

路由组：/scenes（内部扩展路径，文档 §12.1 标准路径为 /instances/{id}/...）
场景下发、天气控制、VIL 标定、截图的核心实现提取为 *_impl 函数，
供 instances 路由（文档标准接口）复用。
"""

from __future__ import annotations

import math
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from hunter_sim.api.deps import get_current_user
from hunter_sim.api.models import (
    ApiResponse,
    CalibrationRequest,
    CalibrationResponse,
    LoadSceneRequest,
    SceneStatusResponse,
    SetWeatherRequest,
)
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _get_scene_runner(request: Request, instance_id: str) -> Any:
    """从 app.state 获取场景运行服务（按 instance_id 路由）。"""
    runners: dict = getattr(request.app.state, "scene_runners", {})
    runner = runners.get(instance_id)
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No scene runner for instance '{instance_id}'",
        )
    return runner


# ─── 核心实现（供 /scenes 与 /instances 两组路由复用） ────────────────────


def load_scene_impl(request: Request, instance_id: str, scene_config: dict[str, Any]) -> dict[str, Any]:
    """校验并下发场景配置到指定实例的场景运行器。

    Args:
        request: FastAPI 请求（用于访问 app.state.scene_runners）。
        instance_id: 目标仿真实例 ID。
        scene_config: 场景配置 JSON（SceneConfig 格式）。

    Returns:
        包含 instance_id/scene_id/status 的响应数据。
    """
    from hunter_sim.scene_runner.scene_config import SceneConfig  # noqa: PLC0415

    runner = _get_scene_runner(request, instance_id)

    try:
        config = SceneConfig(**scene_config)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Scene config validation failed: {exc}",
        ) from exc

    try:
        runner.load_scene(config)
    except Exception as exc:
        logger.error(f"Scene load failed for instance {instance_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Scene load failed: {exc}",
        ) from exc

    return {"instance_id": instance_id, "scene_id": config.scene_id, "status": "ready"}


def set_weather_impl(request: Request, instance_id: str, body: SetWeatherRequest) -> dict[str, Any]:
    """设置实例天气（预设或自定义参数，支持渐变过渡）。

    调用 WeatherManager 真实接口（engine/weather_manager.py）：
    - transition_seconds > 0：start_transition 渐变（由场景运行循环每 tick 推进）；
    - transition_seconds == 0：set_preset / set_weather 立即生效。
    天气参数统一 0-100 刻度（设计文档 §3.4.1）。
    """
    from hunter_sim.engine.weather_manager import (  # noqa: PLC0415
        PRESET_ENVIRONMENTS,
        WeatherProfile,
        get_preset_profile,
    )

    runner = _get_scene_runner(request, instance_id)
    weather_mgr = getattr(runner, "weather_manager", None)
    if weather_mgr is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Weather manager not available",
        )

    if body.preset:
        if body.preset not in PRESET_ENVIRONMENTS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown weather preset '{body.preset}'. Available: {list(PRESET_ENVIRONMENTS)}",
            )
        if body.transition_seconds > 0:
            weather_mgr.start_transition(get_preset_profile(body.preset), body.transition_seconds)
        else:
            weather_mgr.set_preset(body.preset)
    else:
        # 自定义参数：API 字段名 → WeatherProfile 字段名映射，未提供字段保持当前天气
        field_map: dict[str, Optional[float]] = {
            "cloudiness": body.cloudiness,
            "precipitation": body.precipitation,
            "precipitation_deposits": body.road_wetness,
            "wind_intensity": body.wind_intensity,
            "sun_altitude_angle": body.sun_altitude,
            "sun_azimuth_angle": body.sun_azimuth,
        }
        overrides = {k: v for k, v in field_map.items() if v is not None}
        if not overrides:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No weather parameters specified",
            )
        base = weather_mgr.current_profile.model_dump()
        base.update(overrides, preset_name="")
        try:
            profile = WeatherProfile(**base)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid weather parameters: {exc}",
            ) from exc
        if body.transition_seconds > 0:
            weather_mgr.start_transition(profile, body.transition_seconds)
        else:
            weather_mgr.set_weather(profile)

    return {
        "instance_id": instance_id,
        "applied": body.model_dump(exclude_none=True),
        "transitioning": body.transition_seconds > 0,
    }


def calibrate_impl(request: Request, instance_id: str, body: CalibrationRequest) -> CalibrationResponse:
    """VIL 坐标标定（设计文档 §12.3）。

    建立 odom 位姿 (odom_x, odom_y, odom_heading) 到地图位姿
    (map_x, map_y, map_heading) 的映射，推导 CoordinateTransformer
    内部参数 (x0, y0, yaw0)：

        yaw0 = -(map_heading + odom_heading)（弧度，含 CARLA 左手系取反约定）
        (x0, y0) = (map_x, map_y) - R(yaw0) · (odom_x, odom_y)，Y 轴翻转补偿

     odom 为默认原点 (0,0,0) 时退化为 x0=map_x, y0=map_y。
    """
    from hunter_sim.engine.coordinate_converter import CalibrationParams  # noqa: PLC0415

    runner = _get_scene_runner(request, instance_id)
    coord_transformer = getattr(runner, "coordinate_transformer", None)
    if coord_transformer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Coordinate transformer not available (not VIL mode?)",
        )

    yaw0 = -math.radians(body.map_heading + body.odom_heading)
    cos_y0 = math.cos(yaw0)
    sin_y0 = math.sin(yaw0)
    x_rot = body.odom_x * cos_y0 - body.odom_y * sin_y0
    y_rot = body.odom_x * sin_y0 + body.odom_y * cos_y0
    x0 = body.map_x - x_rot
    y0 = body.map_y + y_rot  # CARLA 左手系 Y 翻转

    coord_transformer.update_calibration(
        CalibrationParams(x0=x0, y0=y0, yaw0=yaw0)
    )

    logger.info(
        f"Calibration set for instance {instance_id}: "
        f"x0={x0:.2f}, y0={y0:.2f}, yaw0={yaw0:.4f}rad"
    )
    return CalibrationResponse(
        x0=round(x0, 6),
        y0=round(y0, 6),
        yaw0_rad=round(yaw0, 6),
        map_x=body.map_x,
        map_y=body.map_y,
        map_heading=body.map_heading,
    )


def screenshot_impl(request: Request, instance_id: str) -> dict[str, Any]:
    """获取实例当前帧 RGB 截图（Base64）。"""
    runner = _get_scene_runner(request, instance_id)
    screenshot: Optional[str] = getattr(runner, "get_latest_screenshot", lambda: None)()
    if not screenshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No screenshot available (RGB camera not active)",
        )
    return {"instance_id": instance_id, "image_base64": screenshot}


# ─── 场景加载 ─────────────────────────────────────────────────────────────────


@router.post("/load", response_model=ApiResponse, summary="下发场景配置")
async def load_scene(
    body: LoadSceneRequest,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/load

    向指定实例下发场景配置。配置经过校验后转换为 CARLA 参数并加载。
    """
    return ApiResponse(data=load_scene_impl(request, body.instance_id, body.scene_config))


@router.get("/{instance_id}/status", response_model=ApiResponse, summary="查询场景状态")
async def get_scene_status(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/scenes/{instance_id}/status"""
    runner = _get_scene_runner(request, instance_id)
    state = runner.get_scene_state()
    return ApiResponse(data=state)


@router.post("/{instance_id}/start", response_model=ApiResponse, summary="启动场景运行")
async def start_scene(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/{instance_id}/start"""
    runner = _get_scene_runner(request, instance_id)
    runner.start()
    return ApiResponse(data={"instance_id": instance_id, "status": "running"})


@router.post("/{instance_id}/stop", response_model=ApiResponse, summary="停止场景运行")
async def stop_scene(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/{instance_id}/stop"""
    runner = _get_scene_runner(request, instance_id)
    runner.stop()
    return ApiResponse(data={"instance_id": instance_id, "status": "stopped"})


@router.post("/{instance_id}/pause", response_model=ApiResponse, summary="暂停场景")
async def pause_scene(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/{instance_id}/pause"""
    runner = _get_scene_runner(request, instance_id)
    runner.pause()
    return ApiResponse(data={"instance_id": instance_id, "status": "paused"})


@router.post("/{instance_id}/resume", response_model=ApiResponse, summary="恢复场景")
async def resume_scene(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/{instance_id}/resume"""
    runner = _get_scene_runner(request, instance_id)
    runner.resume()
    return ApiResponse(data={"instance_id": instance_id, "status": "running"})


# ─── 天气控制 ─────────────────────────────────────────────────────────────────


@router.post("/{instance_id}/weather", response_model=ApiResponse, summary="设置天气")
async def set_weather(
    instance_id: str,
    body: SetWeatherRequest,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/{instance_id}/weather

    设置仿真实例的天气参数，支持预设环境和自定义参数。
    渐变过渡时长可配置（transition_seconds），避免传感器数据跳变。
    """
    return ApiResponse(data=set_weather_impl(request, instance_id, body))


# ─── VIL 标定 ─────────────────────────────────────────────────────────────────


@router.post("/{instance_id}/calibrate", response_model=ApiResponse, summary="VIL 坐标标定")
async def set_calibration(
    instance_id: str,
    body: CalibrationRequest,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/{instance_id}/calibrate

    VIL 标定内部兼容路径，核心逻辑见 calibrate_impl（文档 §12.3）。
    """
    resp = calibrate_impl(request, instance_id, body)
    return ApiResponse(data=resp.model_dump())


# ─── 截图 ─────────────────────────────────────────────────────────────────────


@router.get("/{instance_id}/screenshot", response_model=ApiResponse, summary="获取仿真截图")
async def get_screenshot(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/scenes/{instance_id}/screenshot

    获取当前帧的 RGB 相机截图（Base64 编码 PNG）。
    """
    return ApiResponse(data=screenshot_impl(request, instance_id))
