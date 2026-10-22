"""实例管理路由（PROMPT-API-001）。

路由组：/instances，对应设计文档 §12.1 接口表：
创建/列表/详情/销毁、start/stop/pause/resume、实时状态、
场景下发（§12.4）、天气（§12.1）、VIL 标定（§12.3）、截图/视频流地址。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from hunter_sim.api.deps import (
    Principal,
    current_principal,
    get_current_user,
    get_principal,
    get_settings,
    require_admin,
    require_operator,
)
from hunter_sim.api.middleware.logging import audit_log
from hunter_sim.api.models import (
    ApiResponse,
    CalibrationRequest,
    CreateInstanceRequest,
    InstanceResponse,
    InstanceSceneRequest,
    SetWeatherRequest,
)
from hunter_sim.api.runtime import (
    InstanceNotFound,
    SceneRuntimeUnavailable,
    ensure_scene_runner,
    get_scene_runner,
    persisted_scene_state,
    release_scene_runner,
)
from hunter_sim.api.routers.scenes import (
    calibrate_impl,
    load_scene_impl,
    screenshot_impl,
    set_weather_impl,
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


def _assert_owner(request: Request, instance_id: str, user_id: str) -> Any:
    """获取实例并校验归属与租户（V2.5.0）。

    - 实例不存在 → 404；
    - 跨租户且非 admin → 404（不泄露其他租户实例存在性）；
    - 同租户但非本人且非 admin → 403。
    """
    mgr = _get_instance_manager(request)
    inst = mgr.get_instance(instance_id)
    if inst is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance '{instance_id}' not found",
        )
    principal = current_principal(request)
    if inst.tenant != principal.tenant and not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance '{instance_id}' not found",
        )
    if inst.user_id and inst.user_id != user_id and not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Instance '{instance_id}' belongs to another user",
        )
    return inst


async def _ensure_runner_if_carla_ready(request: Request, instance_id: str) -> Any | None:
    """CARLA 就绪时装配场景运行器；SDK 缺失/未连接时返回 None（降级为纯状态流转）。"""
    if getattr(request.app.state, "map_manager", None) is None:
        logger.warning(
            f"CARLA runtime not ready; instance {instance_id} falls back to state-only mode "
            f"({getattr(request.app.state, 'carla_probe_error', None) or 'no carla client'})"
        )
        return None
    try:
        return await asyncio.to_thread(ensure_scene_runner, request.app, instance_id)
    except InstanceNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance '{instance_id}' not found",
        ) from exc
    except SceneRuntimeUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"CARLA runtime unavailable: {exc}",
        ) from exc


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
        tenant=inst.tenant,
        create_time=inst.create_time,
        start_time=inst.start_time,
        error_message=inst.error_message,
    )


def _resolve_scene_config(request: Request, body: InstanceSceneRequest) -> dict[str, Any]:
    """解析场景下发请求体：直传配置或从场景库读取（V2.5.0）。

    Raises:
        HTTPException: 场景库未启用（503）或条目不存在（404）。
    """
    if body.scene_config:
        return body.scene_config
    store = getattr(request.app.state, "state_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scene library requires state persistence; set STATE_ENABLED=true",
        )
    principal = current_principal(request)
    # admin 可跨租户引用场景库条目；普通用户仅限本租户
    item = store.get_library_scene(
        body.library_scene_id, tenant="" if principal.is_admin else principal.tenant
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Library scene '{body.library_scene_id}' not found",
        )
    config = dict(item.get("config") or {})
    scene_id = body.scene_id or str(item.get("scene_id", ""))
    if scene_id:
        config["scene_id"] = scene_id
    return config


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED, summary="创建仿真实例")
async def create_instance(
    body: CreateInstanceRequest,
    request: Request,
    principal: Principal = Depends(require_operator),
) -> ApiResponse:
    """POST /api/v1/sim/instances（设计文档 §12.2）

    创建一个新的 CARLA 仿真实例，分配 GPU 资源并启动容器。
    安全约束：需 operator 及以上角色（V2.5.0 由 admin 下沉），
    并受用户级与租户级并发配额限制；自车模型需在平台审核白名单内（§14.3）。
    响应含 carla_server 连接信息与 WebRTC 视频流地址。
    """
    mgr = _get_instance_manager(request)
    user_id = principal.user_id
    target_tenant = body.tenant or principal.tenant
    if target_tenant != principal.tenant and not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin can create instances in another tenant",
        )

    # 资源白名单：仅允许平台审核通过的车辆模型（§14.3）
    from hunter_sim.engine.vehicle_blueprint_generator import SUPPORTED_VEHICLES  # noqa: PLC0415

    allowed_models = {v["blueprint_id"] for v in SUPPORTED_VEHICLES} | {"hunter.se"}
    if body.vehicle_model not in allowed_models:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Vehicle model '{body.vehicle_model}' is not in the platform-approved whitelist. "
                f"Allowed: {sorted(allowed_models)}"
            ),
        )

    # 单用户并发配额（§14.2 默认 5 个）：预占名额，创建链路任意异常均须归还，
    # 销毁/超时自动回收则由 SimInstanceManager.destroy_instance() 统一释放
    quota_mgr = getattr(request.app.state, "quota_manager", None)
    if quota_mgr is not None:
        try:
            quota_mgr.check_and_reserve(user_id)
        except ResourceError as exc:
            logger.error(f"Quota check failed for user '{user_id}': {exc}")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    try:
        inst = mgr.create_instance(
            mode=body.mode,
            map_id=body.map,
            quality=body.quality,
            user_id=user_id,
            vehicle_id=body.vehicle_id,
            scene_id=body.scene_id,
            vehicle_model=body.vehicle_model,
            replay_config=body.replay_config.model_dump() if body.replay_config else None,
            tenant=target_tenant,
        )

        audit_log(
            "instance.create", user_id, inst.sim_instance_id,
            f"map={body.map} mode={body.mode.value} quality={body.quality.value} tenant={target_tenant}",
            tenant=target_tenant,
            auth_type=principal.auth_type,
        )

        # 文档 §12.2 创建响应结构 + 下一步提示（避免对接方卡在 404 上）
        settings = get_settings()
        data = {
            "sim_instance_id": inst.sim_instance_id,
            "status": inst.status.value,
            "carla_server": {
                "host": inst.carla_host,
                "rpc_port": inst.carla_rpc_port,
                "stream_port": inst.carla_stream_port,
            },
            "stream_url": f"{settings.api.stream_base_url}/sim/{inst.sim_instance_id}",
            "next_step": [
                f"POST /api/v1/sim/instances/{inst.sim_instance_id}/scene（下发场景：加载地图并生成自车）",
                f"POST /api/v1/sim/instances/{inst.sim_instance_id}/start（启动场景运行）",
            ],
        }
        return ApiResponse(data=data)
    except ResourceError as exc:
        # GPU 等资源不足：释放预占配额并返回 503
        if quota_mgr is not None:
            quota_mgr.release(user_id)
        logger.error(f"Resource allocation failed: {exc}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception:
        # 非 ResourceError 的任何失败（含审计/响应构造异常）同样归还配额名额，防名额永久泄漏
        if quota_mgr is not None:
            quota_mgr.release(user_id)
        raise


@router.get("", response_model=ApiResponse, summary="列出实例")
async def list_instances(
    request: Request,
    principal: Principal = Depends(get_principal),
    tenant: str = Query("", description="租户过滤（仅 admin 可跨租户查询）"),
    status_filter: Optional[InstanceStatus] = Query(None, alias="status", description="按状态过滤"),
) -> ApiResponse:
    """GET /api/v1/sim/instances

    列出当前用户（admin 为本租户全部）的仿真实例，可按状态过滤。
    """
    mgr = _get_instance_manager(request)
    query_tenant = principal.tenant
    if tenant and tenant != principal.tenant:
        if not principal.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin can query another tenant's instances",
            )
        query_tenant = tenant
    user_scope = "" if principal.is_admin else principal.user_id
    instances = mgr.list_instances(user_id=user_scope, tenant=query_tenant)
    if status_filter:
        instances = [i for i in instances if i.status == status_filter]
    items = [_instance_to_response(i).model_dump() for i in instances]
    return ApiResponse(data={"instances": items, "total": len(items)})


@router.get("/{instance_id}", response_model=ApiResponse, summary="查询实例详情")
async def get_instance(
    instance_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/instances/{instance_id}"""
    inst = _assert_owner(request, instance_id, user_id)
    return ApiResponse(data=_instance_to_response(inst).model_dump())


@router.post("/{instance_id}/start", response_model=ApiResponse, summary="启动实例")
async def start_instance(
    instance_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/start

    将实例从 READY 状态切换到 RUNNING，并启动场景运行循环（首次调用会按需装配运行器）。
    CARLA 未就绪时降级为纯状态流转，响应中 ``scene_runner=unavailable``。
    """
    mgr = _get_instance_manager(request)
    _assert_owner(request, instance_id, user_id)
    runner = await _ensure_runner_if_carla_ready(request, instance_id)

    if runner is not None:
        if not getattr(runner, "has_scene", True):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "No scene loaded; POST /api/v1/sim/instances/"
                    f"{instance_id}/scene first"
                ),
            )
        runner.start()

    try:
        mgr.transition(instance_id, InstanceStatus.RUNNING)
    except InstanceStateError as exc:
        if runner is not None:
            runner.stop()  # 回滚：状态机拒绝时不留孤儿仿真循环
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit_log("instance.start", user_id, instance_id)
    inst = mgr.get_instance(instance_id)
    data = _instance_to_response(inst).model_dump()
    data["scene_runner"] = "started" if runner is not None else "unavailable"
    return ApiResponse(data=data)


@router.post("/{instance_id}/stop", response_model=ApiResponse, summary="停止实例")
async def stop_instance(
    instance_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/stop

    停止场景运行循环并释放场景 Actor，实例进入 COMPLETED 状态。
    """
    mgr = _get_instance_manager(request)
    _assert_owner(request, instance_id, user_id)
    # 释放运行器（stop + 清理 Actor）在后台线程执行，避免 join 阻塞事件循环
    if get_scene_runner(request.app, instance_id) is not None:
        await asyncio.to_thread(release_scene_runner, request.app, instance_id)
    try:
        mgr.transition(instance_id, InstanceStatus.COMPLETED)
    except InstanceStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    audit_log("instance.stop", user_id, instance_id)
    inst = mgr.get_instance(instance_id)
    return ApiResponse(data=_instance_to_response(inst).model_dump())


@router.post("/{instance_id}/pause", response_model=ApiResponse, summary="暂停实例")
async def pause_instance(
    instance_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/pause"""
    mgr = _get_instance_manager(request)
    _assert_owner(request, instance_id, user_id)
    runner = get_scene_runner(request.app, instance_id)
    if runner is not None:
        runner.pause()
    try:
        mgr.transition(instance_id, InstanceStatus.PAUSED)
    except InstanceStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    inst = mgr.get_instance(instance_id)
    return ApiResponse(data=_instance_to_response(inst).model_dump())


@router.post("/{instance_id}/resume", response_model=ApiResponse, summary="恢复实例")
async def resume_instance(
    instance_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/resume"""
    mgr = _get_instance_manager(request)
    _assert_owner(request, instance_id, user_id)
    runner = get_scene_runner(request.app, instance_id)
    if runner is not None and getattr(runner, "has_scene", True):
        runner.resume()
    try:
        mgr.transition(instance_id, InstanceStatus.RUNNING)
    except InstanceStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    inst = mgr.get_instance(instance_id)
    return ApiResponse(data=_instance_to_response(inst).model_dump())


@router.delete("/{instance_id}", response_model=ApiResponse, summary="销毁实例")
async def destroy_instance(
    instance_id: str,
    request: Request,
    user_id: str = Depends(require_admin),
) -> ApiResponse:
    """DELETE /api/v1/sim/instances/{instance_id}

    销毁实例并释放 GPU 资源与用户配额（§14.2 需管理员权限）；
    同时停止场景循环并销毁场景 Actor，避免 CARLA 中残留静态 Actor。
    """
    mgr = _get_instance_manager(request)
    inst = mgr.get_instance(instance_id)
    if inst is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Instance '{instance_id}' not found")
    owner_id = inst.user_id
    # 先释放场景运行器（join 运行线程最长 5s），再释放 GPU/配额，避免僵尸 tick 线程
    if get_scene_runner(request.app, instance_id) is not None:
        await asyncio.to_thread(release_scene_runner, request.app, instance_id)
    # destroy_instance 内部统一释放 GPU 与用户配额（§14.2，含超时自动回收路径）
    mgr.destroy_instance(instance_id)
    caller = current_principal(request)
    audit_log(
        "instance.destroy",
        user_id,
        instance_id,
        f"owner={owner_id or user_id} tenant={inst.tenant}",
        tenant=inst.tenant,
        auth_type=caller.auth_type,
    )
    return ApiResponse(data={"sim_instance_id": instance_id, "destroyed": True})


@router.get("/{instance_id}/status", response_model=ApiResponse, summary="实例实时状态")
async def get_instance_status(
    instance_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/instances/{instance_id}/status

    返回实例实时状态，数据结构遵循设计文档 §10.2.2 实例数据模型；
    ``scene`` 字段附加当前场景进度（未下发场景时为 null）。
    """
    inst = _assert_owner(request, instance_id, user_id)
    data = inst.to_dict()
    runner = get_scene_runner(request.app, instance_id)
    if runner is not None:
        data["scene"] = runner.get_scene_state()
    else:
        # 运行器未装配（服务重启/未下发）时回落持久化快照，避免查询断崖
        data["scene"] = persisted_scene_state(request.app, instance_id)
    return ApiResponse(data=data)


@router.post("/{instance_id}/scene", response_model=ApiResponse, summary="下发场景配置")
async def assign_scene(
    instance_id: str,
    body: InstanceSceneRequest,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/scene（设计文档 §12.4）

    向指定实例下发场景配置：``scene_config`` 直传，或 ``library_scene_id``
    引用场景库条目（V2.5.0）。
    审查项 O：复用 load_scene_impl（秒级 CARLA 操作），走 to_thread 避免阻塞事件循环。
    """
    scene_config = _resolve_scene_config(request, body)
    data = await asyncio.to_thread(load_scene_impl, request, instance_id, scene_config)
    if body.scene_id and data["scene_id"] != body.scene_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"scene_id mismatch: request '{body.scene_id}' vs config '{data['scene_id']}'",
        )
    return ApiResponse(data=data)


@router.post("/{instance_id}/weather", response_model=ApiResponse, summary="设置天气")
async def set_instance_weather(
    instance_id: str,
    body: SetWeatherRequest,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/weather（设计文档 §12.1）"""
    data = await asyncio.to_thread(set_weather_impl, request, instance_id, body)
    return ApiResponse(data=data)


@router.post("/{instance_id}/vil/calibrate", response_model=ApiResponse, summary="VIL 初始位置标定")
async def vil_calibrate(
    instance_id: str,
    body: CalibrationRequest,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/instances/{instance_id}/vil/calibrate（设计文档 §12.3）

    标定实车 odom 原点在仿真地图中的对应位置和朝向。
    """
    resp = calibrate_impl(request, instance_id, body)
    return ApiResponse(data=resp.model_dump())


@router.get("/{instance_id}/screenshot", response_model=ApiResponse, summary="获取仿真截图")
async def get_instance_screenshot(
    instance_id: str,
    request: Request,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/instances/{instance_id}/screenshot（设计文档 §12.1）"""
    data = await asyncio.to_thread(screenshot_impl, request, instance_id)
    return ApiResponse(data=data)


@router.get("/{instance_id}/stream", response_model=ApiResponse, summary="获取视频流地址")
async def get_instance_stream(
    instance_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/instances/{instance_id}/stream（设计文档 §12.1）

    返回 WebRTC 仿真画面视频流地址。
    """
    inst = _assert_owner(request, instance_id, user_id)
    settings = get_settings()
    return ApiResponse(
        data={
            "sim_instance_id": instance_id,
            "stream_url": f"{settings.api.stream_base_url}/sim/{instance_id}",
            "protocol": "webrtc",
            "carla_server": {
                "host": inst.carla_host,
                "rpc_port": inst.carla_rpc_port,
                "stream_port": inst.carla_stream_port,
            },
        }
    )
