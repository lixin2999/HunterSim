"""执行面观测路由（V2.4.0）。

``GET /api/v1/sim/execution`` 返回引擎库存、租约（哪个实例占用哪个引擎/地图）、
运行器列表与持久化状态，用于多引擎容量核对与重启恢复排障。仅管理员可见。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from hunter_sim.api.deps import require_admin
from hunter_sim.api.models import ApiResponse
from hunter_sim.api.runtime import describe_execution

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
