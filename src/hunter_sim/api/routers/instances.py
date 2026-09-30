"""实例管理路由（PROMPT-API-001）。

路由组：/instances
提供仿真实例的创建、查询、启动、停止、暂停、恢复、销毁操作。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from hunter_sim.api.deps import get_current_user
from hunter_sim.api.models import (
    ApiResponse,
    CreateInstanceRequest,
    InstanceResponse,
)
from hunter_sim.common.exceptions import InstanceStateError, ResourceError
from hunter_sim.common.models import InstanceStatus
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


# ─── 内部工具 ─────────────────────────────────────────────────────────────────


def _get_instance_manager(request: Request) -> Any:
    """从 app.state 获取 SimInstanceManager 实例。"""
    mgr = getattr(request.app.state, "instance_manager", None)
    if mgr is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Instance manager not initialized",
        )
    return mgr


def _instance_to_response(inst: Any) -> InstanceResponse:
    return InstanceResponse(
        sim_instance_id=inst.sim_instance_id,
        status=inst.status,
        mode=inst.mode,
        map_id=inst.map_id,
        quality=inst.quality,
        gpu_id=inst.gpu_id,
        carla_host=inst.carla_host,
        carla_rpc_port=inst.carla_rpc_port,
        scene_id=inst.scene_id,
        vehicle_id=inst.vehicle_id,
        user_id=inst.user_id,
        create_time=inst.create_time,
        start_time=inst.start_time,
        error_message=inst.error_message,
    )


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED, summary="创建仿真实例")
async def create_instance(
    body: CreateInstanceRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances

    创建一个新的 CARLA 仿真实例，分配 GPU 资源并启动容器。

    - **mode**: 仿真模式（vil/sil/replay）
    - **map_id**: 地图 ID（如 Town03）
    - **quality**: 画质等级（low/medium/epic）
    """
    mgr = _get_instance_manager(request)
    try:
        inst = mgr.create_instance(
            mode=body.mode,
            map_id=body.map_id,
            quality=body.quality,
            user_id=user_id,
            vehicle_id=body.vehicle_id,
            scene_id=body.scene_id,
        )
    except ResourceError as exc:
        logger.error(f"Resource allocation failed: {exc}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return ApiResponse(data=_instance_to_response(inst).model_dump())


@router.get("", response_model=ApiResponse, summary="列出实例")
async def list_instances(
    request: Request,
    user_id: str = Depends(get_current_user),
    status_filter: Optional[InstanceStatus] = Query(None, alias="status", description="按状态过滤"),
) -> ApiResponse:
    """GET /api/v1/sim/instances

    列出当前用户的所有仿真实例，可按状态过滤。
    """
    mgr = _get_instance_manager(request)
    instances = mgr.list_instances(user_id=user_id)
    if status_filter:
        instances = [i for i in instances if i.status == status_filter]
    items = [_instance_to_response(i).model_dump() for i in instances]
    return ApiResponse(data={"instances": items, "total": len(items)})


@router.get("/{instance_id}", response_model=ApiResponse, summary="查询实例详情")
async def get_instance(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/instances/{instance_id}"""
    mgr = _get_instance_manager(request)
    inst = mgr.get_instance(instance_id)
    if inst is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Instance '{instance_id}' not found")
    return ApiResponse(data=_instance_to_response(inst).model_dump())


@router.post("/{instance_id}/start", response_model=ApiResponse, summary="启动实例")
async def start_instance(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/start

    将实例从 READY 状态切换到 RUNNING 状态。
    """
    mgr = _get_instance_manager(request)
    try:
        mgr.transition(instance_id, InstanceStatus.RUNNING)
    except InstanceStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    inst = mgr.get_instance(instance_id)
    return ApiResponse(data=_instance_to_response(inst).model_dump())  # type: ignore[union-attr]


@router.post("/{instance_id}/stop", response_model=ApiResponse, summary="停止实例")
async def stop_instance(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/stop

    停止运行中的实例（进入 COMPLETED 状态）。
    """
    mgr = _get_instance_manager(request)
    try:
        mgr.transition(instance_id, InstanceStatus.COMPLETED)
    except InstanceStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    inst = mgr.get_instance(instance_id)
    return ApiResponse(data=_instance_to_response(inst).model_dump())  # type: ignore[union-attr]


@router.post("/{instance_id}/pause", response_model=ApiResponse, summary="暂停实例")
async def pause_instance(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/pause"""
    mgr = _get_instance_manager(request)
    try:
        mgr.transition(instance_id, InstanceStatus.PAUSED)
    except InstanceStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    inst = mgr.get_instance(instance_id)
    return ApiResponse(data=_instance_to_response(inst).model_dump())  # type: ignore[union-attr]


@router.post("/{instance_id}/resume", response_model=ApiResponse, summary="恢复实例")
async def resume_instance(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/resume"""
    mgr = _get_instance_manager(request)
    try:
        mgr.transition(instance_id, InstanceStatus.RUNNING)
    except InstanceStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    inst = mgr.get_instance(instance_id)
    return ApiResponse(data=_instance_to_response(inst).model_dump())  # type: ignore[union-attr]


@router.delete("/{instance_id}", response_model=ApiResponse, summary="销毁实例")
async def destroy_instance(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """DELETE /api/v1/sim/instances/{instance_id}

    销毁实例并释放 GPU 资源。
    """
    mgr = _get_instance_manager(request)
    inst = mgr.get_instance(instance_id)
    if inst is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Instance '{instance_id}' not found")
    mgr.destroy_instance(instance_id)
    return ApiResponse(data={"sim_instance_id": instance_id, "destroyed": True})
