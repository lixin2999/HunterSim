"""资源管理路由（PROMPT-API-001）。

路由组：/maps /vehicles /environments /resources
对应设计文档 §10.4.2 资源 API：
- GET  /resources/maps          获取可用地图列表（含自定义地图）
- POST /resources/maps/upload   上传自定义 OpenDRIVE 地图（附录 B：multipart/form-data）
- GET  /resources/vehicles      获取可用车辆模型
- GET  /resources/environments  获取环境预设列表
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError as PydanticValidationError

from hunter_sim.api.deps import get_current_user, require_admin
from hunter_sim.api.models import (
    ApiResponse,
    MapInfo,
    MapUploadRequest,
    VehicleInfo,
    WeatherPresetInfo,
)
from hunter_sim.common.exceptions import ValidationError
from hunter_sim.common.models import ResourceSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()

# map_id 安全字符集（防路径穿越，与 MapUploadRequest.pattern 一致；
# 使用 fullmatch 而非 match+$，避免尾部换行绕过）
_MAP_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

# 上传地图文件大小上限（§14.3 内容安全：防止恶意超大内容）
_MAX_MAP_SIZE_BYTES = 50 * 1024 * 1024

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


def _custom_maps_dir(request: Request) -> Path:
    """返回资源仓库自定义地图目录（设计文档 §10.4.1）。"""
    root = getattr(request.app.state, "resources_dir", None) or ResourceSettings().resources_dir
    return Path(root) / "maps" / "custom"


@router.get("/maps", response_model=ApiResponse, summary="查询可用地图列表")
async def list_maps(
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/maps

    返回 CARLA 服务器当前可用的地图列表（含内置地图和自定义地图）。
    """
    entries: dict[str, MapInfo] = {}
    map_mgr = getattr(request.app.state, "map_manager", None)
    if map_mgr is not None:
        # 从运行中的 CARLA 服务获取实时地图列表
        live_maps = map_mgr.get_available_maps()
        entries.update({m: MapInfo(map_id=m, name=m, is_custom=False) for m in live_maps})
    else:
        entries.update({m["map_id"]: MapInfo(**m) for m in _BUILTIN_MAPS})

    # 叠加资源仓库中的自定义 OpenDRIVE 地图（§10.4.1 maps/custom）
    # 附录 B：创建实例时通过 "custom/{map_name}" 引用自定义地图
    custom_dir = _custom_maps_dir(request)
    if custom_dir.is_dir():
        for xodr in sorted(custom_dir.glob("*.xodr")):
            stem = xodr.stem
            entries[f"custom/{stem}"] = MapInfo(
                map_id=f"custom/{stem}", name=stem, is_custom=True
            )

    maps = list(entries.values())
    return ApiResponse(data={"maps": [m.model_dump() for m in maps], "total": len(maps)})


@router.post("/maps/upload", response_model=ApiResponse, summary="上传自定义 OpenDRIVE 地图")
async def upload_map(
    request: Request,
    _: str = Depends(require_admin),
) -> ApiResponse:
    """POST /api/v1/sim/maps/upload

    上传自定义 OpenDRIVE 地图到资源仓库 maps/custom 目录。
    主接口形式为 multipart/form-data（设计文档附录 B：file + map_id 表单字段），
    同时兼容 JSON body（{map_id, content}）传参。
    安全约束（§14.2/§14.3）：需管理员角色；入库前执行内容安全检查与格式解析审核，
    非法文件拒绝并返回错误详情。
    """
    from hunter_sim.engine.opendrive_parser import OpenDriveParser  # noqa: PLC0415

    map_id, content = await _parse_upload_request(request)

    if not _MAP_ID_PATTERN.fullmatch(map_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid map_id '{map_id}' (allowed: letters, digits, '_', '-')",
        )
    if len(content.encode("utf-8")) > _MAX_MAP_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Map file exceeds size limit ({_MAX_MAP_SIZE_BYTES // 1024 // 1024} MB)",
        )

    custom_dir = _custom_maps_dir(request)
    custom_dir.mkdir(parents=True, exist_ok=True)
    target = custom_dir / f"{map_id}.xodr"
    target.write_text(content, encoding="utf-8")

    summary = OpenDriveParser(target).parse()
    if not summary.valid:
        target.unlink(missing_ok=True)
        raise ValidationError(
            field="content",
            value=map_id,
            rule="; ".join(summary.errors) or "invalid OpenDRIVE file",
            module="resource_manager",
        )

    logger.info(f"Custom map uploaded: {map_id} ({summary.total_road_count} roads)")
    return ApiResponse(
        data={
            "map_id": map_id,
            "reference": f"custom/{map_id}",
            "is_custom": True,
            "review_status": "approved",
            "path": str(target),
            "road_count": summary.total_road_count,
            "junction_count": summary.total_junction_count,
            "total_length_m": summary.total_length_m,
        },
        message="map uploaded",
    )


async def _parse_upload_request(request: Request) -> tuple[str, str]:
    """解析地图上传请求，返回 (map_id, 文件内容字符串)。

    multipart/form-data 为主形式（附录 B）；JSON body 为兼容形式。

    Raises:
        HTTPException: 400 请求体缺失/格式错误；422 缺少必要字段。
    """
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()  # starlette FormData（MultiDict），支持 str 与 UploadFile 混合值
        upload = form.get("file")
        if upload is None or isinstance(upload, str) or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Missing multipart field 'file' (.xodr file upload)",
            )
        map_id = str(form.get("map_id") or Path(str(upload.filename or "")).stem)
        raw = await upload.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Map file is not valid UTF-8 text",
            ) from exc
        return map_id, content

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid upload payload (expected multipart/form-data or JSON): {exc}",
        ) from exc
    try:
        body = MapUploadRequest(**payload)
    except PydanticValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid upload payload: {exc}",
        ) from exc
    return body.map_id, body.content


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
