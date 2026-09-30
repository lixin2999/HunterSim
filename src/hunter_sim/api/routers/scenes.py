"""场景管理路由（PROMPT-API-001）。

路由组：/scenes
提供场景下发、状态查询、天气控制、VIL 标定接口。
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


# ─── 场景加载 ─────────────────────────────────────────────────────────────────


@router.post("/load", response_model=ApiResponse, summary="下发场景配置")
async def load_scene(
    body: LoadSceneRequest,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/load

    向指定实例下发场景配置。配置经过校验后转换为 CARLA 参数并加载。

    - **instance_id**: 目标仿真实例 ID
    - **scene_config**: 场景配置 JSON（SceneConfig 格式）
    """
    from hunter_sim.scene_runner.scene_config import SceneConfig

    runner = _get_scene_runner(request, body.instance_id)

    # 解析并验证场景配置
    try:
        config = SceneConfig(**body.scene_config)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Scene config validation failed: {exc}",
        ) from exc

    try:
        runner.load_scene(config)
    except Exception as exc:
        logger.error(f"Scene load failed for instance {body.instance_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Scene load failed: {exc}",
        ) from exc

    return ApiResponse(
        data={
            "instance_id": body.instance_id,
            "scene_id": config.scene_id,
            "status": "ready",
        }
    )


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
    runner = _get_scene_runner(request, instance_id)
    weather_mgr = getattr(runner, "weather_manager", None)
    if weather_mgr is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Weather manager not available",
        )

    if body.preset:
        # 使用预设环境
        weather_mgr.apply_preset(body.preset, transition_seconds=body.transition_seconds)
    else:
        # 自定义参数
        params: dict[str, float] = {}
        if body.cloudiness is not None:
            params["cloudiness"] = body.cloudiness
        if body.precipitation is not None:
            params["precipitation"] = body.precipitation
        if body.road_wetness is not None:
            params["road_wetness"] = body.road_wetness
        if body.wind_intensity is not None:
            params["wind_intensity"] = body.wind_intensity
        if body.sun_altitude is not None:
            params["sun_altitude"] = body.sun_altitude
        if body.sun_azimuth is not None:
            params["sun_azimuth"] = body.sun_azimuth

        if not params:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No weather parameters specified",
            )
        weather_mgr.apply_custom(params, transition_seconds=body.transition_seconds)

    return ApiResponse(data={"instance_id": instance_id, "applied": body.model_dump(exclude_none=True)})


# ─── VIL 标定 ─────────────────────────────────────────────────────────────────


@router.post("/{instance_id}/calibrate", response_model=ApiResponse, summary="VIL 坐标标定")
async def set_calibration(
    instance_id: str,
    body: CalibrationRequest,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/{instance_id}/calibrate

    设置 VIL 虚实映射的初始坐标标定参数（实车启动点在 CARLA 地图中的位姿）。
    标定后，实车 odom 坐标系与 CARLA 地图坐标系建立固定映射关系。

    - **x0**: 初始 CARLA X 坐标（米）
    - **y0**: 初始 CARLA Y 坐标（米）
    - **yaw0_deg**: 初始 CARLA 航向（度，-180~180）
    """
    runner = _get_scene_runner(request, instance_id)
    coord_transformer = getattr(runner, "coordinate_transformer", None)
    if coord_transformer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Coordinate transformer not available (not VIL mode?)",
        )

    yaw0_rad = math.radians(body.yaw0_deg)
    coord_transformer.set_calibration(body.x0, body.y0, yaw0_rad)

    resp = CalibrationResponse(x0=body.x0, y0=body.y0, yaw0_rad=round(yaw0_rad, 6))
    logger.info(f"Calibration set for instance {instance_id}: x0={body.x0}, y0={body.y0}, yaw0={yaw0_rad:.4f}rad")
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
    runner = _get_scene_runner(request, instance_id)
    screenshot: Optional[str] = getattr(runner, "get_latest_screenshot", lambda: None)()
    if not screenshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No screenshot available (RGB camera not active)",
        )
    return ApiResponse(data={"instance_id": instance_id, "image_base64": screenshot})
