"""健康检查路由（PROMPT-API-001）。

提供综合健康、liveness、readiness 三个端点，供 K8s 探针使用。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from hunter_sim.api.models import ApiResponse, HealthResponse
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()

_START_TIME: float = time.time()


@router.get("/health", response_model=ApiResponse, summary="综合健康检查")
async def health_check(request: Request) -> ApiResponse:
    """GET /api/v1/sim/health

    返回系统综合健康状态：
    - healthy: 所有服务正常
    - degraded: 部分服务降级（CARLA 连接异常等）
    - unhealthy: 核心服务不可用
    """
    # 从 app.state 读取各服务健康状态（由各服务在启动时注册）
    carla_connected: bool = getattr(request.app.state, "carla_connected", False)
    active_instances: int = getattr(request.app.state, "active_instance_count", 0)

    status = "healthy" if carla_connected else "degraded"
    payload = HealthResponse(
        status=status,
        uptime_seconds=round(time.time() - _START_TIME, 1),
        carla_connected=carla_connected,
        active_instances=active_instances,
    )
    return ApiResponse(data=payload.model_dump())


@router.get("/health/live", summary="Liveness 探针")
async def liveness() -> dict[str, str]:
    """GET /api/v1/sim/health/live

    K8s LivenessProbe：进程存活即返回 200。
    """
    return {"status": "alive"}


@router.get("/health/ready", summary="Readiness 探针")
async def readiness(request: Request) -> ApiResponse:
    """GET /api/v1/sim/health/ready

    K8s ReadinessProbe：检查核心依赖是否就绪（CARLA 连接可用）。
    """
    carla_connected: bool = getattr(request.app.state, "carla_connected", False)
    if not carla_connected:
        return ApiResponse(code=503, message="CARLA not connected", data={"ready": False})
    return ApiResponse(data={"ready": True})


@router.get("/metrics", summary="Prometheus 指标")
async def metrics(request: Request) -> Response:
    """GET /api/v1/sim/metrics

    暴露 Prometheus 文本格式指标，供 docker-compose / K8s 监控栈抓取。
    附带进程运行时长与活跃实例数等仿真服务自定义指标。
    """
    active_instances: int = getattr(request.app.state, "active_instance_count", 0)
    carla_connected: bool = getattr(request.app.state, "carla_connected", False)
    extra = (
        "# HELP huntersim_uptime_seconds Service uptime in seconds.\n"
        "# TYPE huntersim_uptime_seconds gauge\n"
        f"huntersim_uptime_seconds {time.time() - _START_TIME:.1f}\n"
        "# HELP huntersim_active_instances Number of active simulation instances.\n"
        "# TYPE huntersim_active_instances gauge\n"
        f"huntersim_active_instances {active_instances}\n"
        "# HELP huntersim_carla_connected Whether CARLA connection is up.\n"
        "# TYPE huntersim_carla_connected gauge\n"
        f"huntersim_carla_connected {1 if carla_connected else 0}\n"
    )
    payload = generate_latest() + extra.encode("utf-8")
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)
