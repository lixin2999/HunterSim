"""执行面观测与运维路由（V2.4.0，V2.7.0 P1-7 租约回收，V2.8.0 P2-15 引擎自愈）。

``GET /api/v1/sim/execution`` 返回引擎库存、租约（哪个实例占用哪个引擎/地图）、
运行器列表、持久化状态与 CARLA 进程托管情况，用于多引擎容量核对与重启恢复排障。
仅管理员可见。``POST /api/v1/sim/execution/engines/restart`` 可手动重启托管的本地 CARLA。
V2.9.0：强制回收仅限僵尸租约（心跳超时）并联动实例状态，重启不再阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from hunter_sim.api.deps import require_admin
from hunter_sim.api.middleware.logging import audit_log
from hunter_sim.api.models import ApiResponse
from hunter_sim.api.runtime import (
    describe_execution,
    force_release_execution_lease,
    mark_instance_failed,
)
from hunter_sim.common.metrics import EXECUTION_LEASE_RECLAIMS_TOTAL

router = APIRouter()


def _supervisor(request: Request) -> Any | None:
    """获取 CARLA 进程托管器（未启用时为 None）。"""
    return getattr(request.app.state, "carla_supervisor", None)


@router.get("/execution", response_model=ApiResponse, summary="执行面状态")
async def get_execution_status(request: Request, _: str = Depends(require_admin)) -> ApiResponse:
    """GET /api/v1/sim/execution

    返回：
    - engines: 声明的 CARLA 端点及当前 holder/map_id（P2-14：空闲端点也展示最后加载的地图）；
    - active_leases: 正在运行场景的引擎数；
    - runners: 已装配运行器与所在引擎；
    - persistence: 持久化开关、数据库路径与启动恢复的实例数；
    - carla_supervisor: 本地 CARLA 进程托管状态与最近事件（P2-15，未启用时为 None）。
    """
    payload = describe_execution(request.app)
    supervisor = _supervisor(request)
    payload["carla_supervisor"] = supervisor.snapshot() if supervisor is not None else None
    return ApiResponse(data=payload)


@router.delete(
    "/execution/leases/{instance_id}",
    response_model=ApiResponse,
    summary="强制回收僵尸引擎租约",
    description=(
        "V2.7.0 P1-7：当 ``/execution`` 显示某端点持有者已僵（心跳超时、运行器无法推进）"
        "时，管理员可强制释放其实例运行器与引擎租约，使端点立即可被新实例复用。"
        "V2.9.0：仅允许回收心跳超时（stale=true）的租约，活跃租约返回 409；"
        "回收后实例同步置为 failed（终态），否则端点仍会被控制面计为已分配。"
    ),
)
async def reclaim_execution_lease(
    instance_id: str,
    request: Request,
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """DELETE /api/v1/sim/execution/leases/{instance_id}"""
    plane = getattr(request.app.state, "execution_plane", None)
    known_ids: set[str] = set()
    if plane is not None:
        snapshot = describe_execution(request.app)
        known_ids = {lease.get("instance_id", "") for lease in snapshot.get("leases", [])}
        known_ids |= {runner.get("instance_id", "") for runner in snapshot.get("runners", [])}
    if instance_id not in known_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No execution lease/runner for instance '{instance_id}'",
        )
    if plane is not None and plane.lease_of(instance_id) is not None:
        if not plane.is_stale_lease(instance_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Lease of instance '{instance_id}' is still alive (heartbeat within TTL); "
                    "stop the scene or destroy the instance instead of force-reclaiming a live lease"
                ),
            )
    released = force_release_execution_lease(request.app, instance_id)
    # V2.9.0：租约回收后必须把实例置为终态，否则 _assign_endpoint 仍把端点算作已分配
    instance_failed = mark_instance_failed(
        request.app, instance_id, "execution lease reclaimed by admin (stale holder)"
    )
    EXECUTION_LEASE_RECLAIMS_TOTAL.inc()
    audit_log(
        "execution.lease.reclaim",
        admin_id,
        instance_id,
        detail=f"released={released}, instance_failed={instance_failed}",
    )
    return ApiResponse(
        data={
            "instance_id": instance_id,
            "released": released,
            "reclaimed": True,
            "instance_failed": instance_failed,
        }
    )


@router.post(
    "/execution/engines/restart",
    response_model=ApiResponse,
    summary="重启本地 CARLA 引擎进程",
    description=(
        "V2.8.0 P2-15：手动重启由本服务托管的本地 CARLA 进程（仅回环端点）。"
        "重启会重置世界：该端点上的场景需重新装配。未启用托管或端点不受管时返回 409/404。"
    ),
)
async def restart_engine(
    request: Request,
    endpoint: str = Query(..., description="引擎端点 key（形如 127.0.0.1:2000）"),
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """POST /api/v1/sim/execution/engines/restart?endpoint=127.0.0.1:2000"""
    supervisor = _supervisor(request)
    if supervisor is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="CARLA process supervisor is disabled (set CARLA_SUPERVISOR_ENABLED=true)",
        )
    if not supervisor.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Supervisor is observe-only: set CARLA_SUPERVISOR_BINARY_PATH to enable restart",
        )
    if endpoint not in supervisor.managed_endpoints:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Endpoint '{endpoint}' is not process-managed "
                f"(managed: {supervisor.managed_endpoints or 'none'}); "
                "remote engines are restarted by their own orchestrator"
            ),
        )
    restarted = bool(await asyncio.to_thread(supervisor.restart, endpoint))
    audit_log(
        "engine.carla.manual_restart",
        admin_id,
        endpoint,
        detail=f"restarted={restarted}",
    )
    return ApiResponse(
        data={"endpoint": endpoint, "restarted": restarted}
    )
