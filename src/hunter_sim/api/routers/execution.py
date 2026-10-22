"""执行面观测路由（V2.4.0）。

``GET /api/v1/sim/execution`` 返回引擎库存、租约（哪个实例占用哪个引擎/地图）、
运行器列表与持久化状态，用于多引擎容量核对与重启恢复排障。仅管理员可见。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from hunter_sim.api.deps import require_admin
from hunter_sim.api.models import ApiResponse
from hunter_sim.api.runtime import describe_execution, force_release_execution_lease
from hunter_sim.common.metrics import EXECUTION_LEASE_RECLAIMS_TOTAL

router = APIRouter()


@router.get("/execution", response_model=ApiResponse, summary="执行面状态")
async def get_execution_status(request: Request, _: str = Depends(require_admin)) -> ApiResponse:
    """GET /api/v1/sim/execution

    返回：
    - engines: 声明的 CARLA 端点及当前 holder/map_id；
    - active_leases: 正在运行场景的引擎数；
    - runners: 已装配运行器与所在引擎；
    - persistence: 持久化开关、数据库路径与启动恢复的实例数。
    """
    return ApiResponse(data=describe_execution(request.app))


@router.delete(
    "/execution/leases/{instance_id}",
    response_model=ApiResponse,
    summary="强制回收僵尸引擎租约",
    description=(
        "V2.7.0 P1-7：当 ``/execution`` 显示某端点持有者已僵（心跳超时、运行器无法推进）"
        "时，管理员可强制释放其实例运行器与引擎租约，使端点立即可被新实例复用。"
    ),
)
async def reclaim_execution_lease(
    instance_id: str,
    request: Request,
    _: str = Depends(require_admin),
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
    released = force_release_execution_lease(request.app, instance_id)
    EXECUTION_LEASE_RECLAIMS_TOTAL.inc()
    return ApiResponse(
        data={"instance_id": instance_id, "released": released, "reclaimed": True}
    )
