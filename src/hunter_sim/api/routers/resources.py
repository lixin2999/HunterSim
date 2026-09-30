"""资源管理路由（PROMPT-API-001）。

路由组：/maps /vehicles /environments /resources
提供地图列表、车辆蓝图列表、天气预设查询等只读接口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from hunter_sim.api.deps import get_current_user
from hunter_sim.api.models import ApiResponse, MapInfo, VehicleInfo, WeatherPresetInfo
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()

# 内置地图列表（CARLA 0.9.16）
_BUILTIN_MAPS = [
    {"map_id": "Town01", "name": "Town 01"},
    {"map_id": "Town02", "name": "Town 02"},
    {"map_id": "Town03", "name": "Town 03"},
    {"map_id": "Town04", "name": "Town 04"},
    {"map_id": "Town05", "name": "Town 05"},
    {"map_id": "Town06", "name": "Town 06"},
    {"map_id": "Town07", "name": "Town 07"},
    {"map_id": "Town10HD_Opt", "name": "Town 10 HD Opt"},
]

# 天气预设列表
_WEATHER_PRESETS = [
    {"preset_name": "sunny_noon", "description": "晴天正午，光照充足"},
    {"preset_name": "cloudy", "description": "阴天，漫射光照"},
    {"preset_name": "light_rain", "description": "小雨，路面微湿"},
    {"preset_name": "heavy_rain", "description": "大雨，路面积水，LiDAR 衰减"},
    {"preset_name": "foggy", "description": "雾天，能见度低"},
    {"preset_name": "night", "description": "夜间，需开启车灯"},
    {"preset_name": "dusk", "description": "黄昏，低光照"},
    {"preset_name": "dawn", "description": "黎明，低光照"},
]


@router.get("/maps", response_model=ApiResponse, summary="查询可用地图列表")
async def list_maps(
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/maps

    返回 CARLA 服务器当前可用的地图列表（含内置地图和自定义地图）。
    """
    map_mgr = getattr(request.app.state, "map_manager", None)
    if map_mgr is not None:
        # 从运行中的 CARLA 服务获取实时地图列表
        live_maps = map_mgr.get_available_maps()
        maps = [MapInfo(map_id=m, name=m, is_custom=False) for m in live_maps]
    else:
        maps = [MapInfo(**m) for m in _BUILTIN_MAPS]
    return ApiResponse(data={"maps": [m.model_dump() for m in maps], "total": len(maps)})


@router.get("/vehicles", response_model=ApiResponse, summary="查询车辆蓝图列表")
async def list_vehicles(
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/vehicles

    返回 CARLA 蓝图库中所有可用车辆模型列表。
    """
    from hunter_sim.engine.vehicle_blueprint_generator import SUPPORTED_VEHICLES

    vehicles = [VehicleInfo(**v) for v in SUPPORTED_VEHICLES]
    return ApiResponse(data={"vehicles": [v.model_dump() for v in vehicles], "total": len(vehicles)})


@router.get("/environments", response_model=ApiResponse, summary="查询天气预设列表")
async def list_environments(
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/environments

    返回 8 种预设天气环境列表。
    """
    presets = [WeatherPresetInfo(**p) for p in _WEATHER_PRESETS]
    return ApiResponse(data={"presets": [p.model_dump() for p in presets], "total": len(presets)})


@router.get("/resources/gpu", response_model=ApiResponse, summary="查询 GPU 资源状态")
async def get_gpu_status(
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/resources/gpu

    返回 GPU 资源池当前状态（显存使用、活跃实例数）。
    """
    gpu_pool = getattr(request.app.state, "gpu_pool", None)
    if gpu_pool is None:
        return ApiResponse(data={"available": False, "devices": []})
    status_list = gpu_pool.get_status()
    return ApiResponse(data={"available": True, "devices": status_list})


@router.get("/resources/quota", response_model=ApiResponse, summary="查询用户配额")
async def get_quota(
    request: Request,
    user_id: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/resources/quota

    返回当前用户的实例配额使用情况。
    """
    quota_mgr = getattr(request.app.state, "quota_manager", None)
    if quota_mgr is None:
        return ApiResponse(data={"user_id": user_id, "usage": "quota manager not available"})
    usage = quota_mgr.get_usage(user_id=user_id)
    return ApiResponse(data={"user_id": user_id, **usage})
