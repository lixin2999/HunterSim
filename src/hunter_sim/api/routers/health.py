"""健康检查路由（PROMPT-API-001）。

提供综合健康、liveness、readiness 三个探针端点，供 K8s 使用；
另提供性能目标查询端点（设计文档 §13.1）与 Prometheus 指标扩展（§15.4）。
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

# 仿真性能指标目标值（设计文档 §13.1）
PERFORMANCE_TARGETS: dict[str, dict[str, Any]] = {
    "simulation_fps": {"target": ">= 30", "unit": "FPS", "note": "Medium 画质，保证视觉流畅"},
    "vil_end_to_end_latency": {"target": "< 300", "unit": "ms", "note": "实车数据到虚拟画面显示"},
    "scene_load_time": {"target": "< 30", "unit": "s", "note": "从创建到就绪"},
    "sensor_data_latency": {"target": "< 50", "unit": "ms", "note": "仿真传感器数据输出延迟"},
    "sil_control_loop_latency": {"target": "< 100", "unit": "ms", "note": "传感器→算法→控制→仿真"},
    "gpu_memory_per_instance": {"target": "< 8", "unit": "GB", "note": "Medium 画质单实例显存占用"},
}

# 监控告警阈值（设计文档 §15.4，与 docker/alerts.yml 保持一致）
ALERT_THRESHOLDS: dict[str, str] = {
    "carla_rpc_response_time": "> 5s",
    "gpu_utilization": "> 95% 持续 10min",
    "gpu_memory_usage": "> 90%",
    "simulation_fps": "< 20",
    "vil_latency": "> 500ms",
    "disk_usage": "> 85%",
}


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


@router.get("/health/performance-targets", response_model=ApiResponse, summary="性能目标与告警阈值")
async def performance_targets() -> ApiResponse:
    """GET /api/v1/sim/health/performance-targets

    返回设计文档 §13.1 仿真性能指标目标值与 §15.4 监控告警阈值，
    供运维巡检与监控配置比对。
    """
    return ApiResponse(
        data={
            "performance_targets": PERFORMANCE_TARGETS,
            "alert_thresholds": ALERT_THRESHOLDS,
        }
    )


@router.get("/metrics", summary="Prometheus 指标")
async def metrics(request: Request) -> Response:
    """GET /api/v1/sim/metrics

    暴露 Prometheus 文本格式指标，供 docker-compose / K8s 监控栈抓取。
    附带进程运行时长、活跃实例数，以及健康监控指标
    （RPC 延迟/FPS/VIL 延迟，对应设计文档 §15.4 告警项，由服务注册到 app.state）。
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
    # §15.4 告警监控项：仅在服务注册了实时值时输出
    monitored_gauges: tuple[tuple[str, str, str], ...] = (
        ("carla_rpc_latency_seconds", "huntersim_carla_rpc_latency_seconds", "CARLA RPC 响应时间"),
        ("simulation_fps", "huntersim_simulation_fps", "仿真平均帧率"),
        ("vil_latency_ms", "huntersim_vil_latency_ms", "VIL 端到端延迟"),
        ("disk_usage_ratio", "huntersim_disk_usage_ratio", "磁盘使用率（0-1）"),
    )
    for state_key, metric_name, help_text in monitored_gauges:
        value: Any = getattr(request.app.state, state_key, None)
        if value is not None:
            extra += (
                f"# HELP {metric_name} {help_text}.\n"
                f"# TYPE {metric_name} gauge\n"
                f"{metric_name} {float(value):.4f}\n"
            )
    payload = generate_latest() + extra.encode("utf-8")
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)
