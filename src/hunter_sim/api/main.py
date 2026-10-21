"""FastAPI 应用主入口（PROMPT-API-001）。

HunterSim REST API 服务，基础路径 /api/v1/sim。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from hunter_sim.api.deps import set_app_settings
from hunter_sim.api.error_handlers import register_exception_handlers
from hunter_sim.api.middleware.auth import JWTAuthMiddleware
from hunter_sim.api.middleware.logging import RequestLogMiddleware
from hunter_sim.api.routers import health, instances, resources, scenarios, scenes, websocket
from hunter_sim.common.models import HunterSimSettings, InstanceStatus, SceneStatus
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.map_manager import MapManager
from hunter_sim.resource_manager.gpu_resource_pool import GPUResourcePool
from hunter_sim.resource_manager.health_monitor import InstanceHealthMonitor, ResourceQuotaManager
from hunter_sim.resource_manager.instance_manager import SimInstanceManager

logger = get_logger(__name__)

# 探测失败摘要最大长度：异常文本可能很长，/health 只保留可读部分
_MAX_PROBE_ERROR_CHARS = 200


def _carla_target(settings: HunterSimSettings) -> str:
    """CARLA 探测目标（host:port），用于日志与 /health 诊断字段。"""
    return f"{settings.carla.host}:{settings.carla.rpc_port}"


def _clip_error(text: str) -> str:
    """截断过长的异常摘要，避免 /health 响应体被异常栈淹没。"""
    if len(text) <= _MAX_PROBE_ERROR_CHARS:
        return text
    return text[:_MAX_PROBE_ERROR_CHARS] + "..."


def _describe_unavailable(state: Any, settings: HunterSimSettings) -> str:
    """给 /health 输出 CARLA 不可用的原因（区分“未装 SDK”与“客户端初始化失败”）。"""
    if not getattr(state, "carla_sdk_installed", False):
        return (
            f"CARLA Python SDK 未安装（import carla 失败），目标 {_carla_target(settings)}；"
            "请安装到当前服务解释器后重启服务"
        )
    return f"CARLA 客户端初始化失败：{getattr(state, 'carla_client_error', None) or '未知原因'}"


def _probe_carla(client: Any) -> tuple[bool, str | None]:
    """CARLA 连通性探测（后台监控线程执行，阻塞受 set_timeout 限制）。

    Returns:
        (是否连通, 失败摘要)；探测成功时失败摘要为 None。
    """
    if client is None:
        return False, None
    try:
        client.get_server_version()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _refresh_carla_state(state: Any, settings: HunterSimSettings) -> None:
    """刷新 CARLA 连通状态（健康巡检周期调用）。

    仅在首次评估或状态翻转时记录日志，避免每周期刷屏；失败原因写入
    app.state.carla_probe_error，供 /health 与运维排查直接读取。
    """
    target = _carla_target(settings)
    connected, error = _probe_carla(getattr(state, "carla_client", None))
    previous: bool = getattr(state, "carla_connected", False)
    first_evaluation = not getattr(state, "carla_probe_evaluated", False)

    state.carla_connected = connected
    state.carla_probe_evaluated = True
    if connected:
        state.carla_probe_error = None
    else:
        state.carla_probe_error = (
            _clip_error(f"{target} 不可达：{error}") if error else _describe_unavailable(state, settings)
        )

    if connected and (first_evaluation or not previous):
        logger.info(f"CARLA connection established ({target})")
    elif not connected and (first_evaluation or previous):
        logger.warning(f"CARLA connection unavailable ({target}): {state.carla_probe_error}")


def _assemble_services(app: FastAPI, settings: HunterSimSettings) -> InstanceHealthMonitor | None:
    """生产运行组件装配（审查项 L：新增能力不能仅靠测试注入路径可达）。

    仅装配 app.state 中缺失的组件（only-if-absent），不覆盖测试/外部已注入的
    实例（如 conftest 注入的真实 SimInstanceManager），保证非破坏性。

    Returns:
        本次新建的 InstanceHealthMonitor（已 start）；若监控为外部注入或未创建则返回 None，
        由 lifespan 在关停时负责 stop()。
    """
    state = app.state

    # 诊断字段默认值（only-if-absent，不覆盖测试/外部注入）
    for attr, default in (
        ("carla_connected", False),
        ("carla_sdk_installed", False),
        ("carla_client_error", None),
        ("carla_probe_error", None),
        ("carla_probe_evaluated", False),
    ):
        if not hasattr(state, attr):
            setattr(state, attr, default)

    if getattr(state, "gpu_pool", None) is None:
        state.gpu_pool = GPUResourcePool()

    if getattr(state, "instance_manager", None) is None:
        state.instance_manager = SimInstanceManager(
            settings=settings.resource,
            gpu_pool=getattr(state, "gpu_pool", None),
            quota_manager=getattr(state, "quota_manager", None),
            carla=settings.carla,
        )

    if getattr(state, "scene_runners", None) is None:
        state.scene_runners = {}

    # CARLA 客户端与地图管理器：无 carla SDK 时降级（惰性导入规范，不阻断服务启动）
    if getattr(state, "map_manager", None) is None:
        try:
            import carla  # noqa: PLC0415  惰性导入：仅真实 CARLA 环境可用
        except ImportError:
            state.carla_client = None
            logger.warning(
                f"carla SDK 不可用，map_manager 装配降级（相关端点返回不可用状态）；"
                f"目标 {_carla_target(settings)}，如需连接 CARLA 请安装到当前服务解释器后重启"
            )
        else:
            state.carla_sdk_installed = True
            try:
                client = carla.Client(settings.carla.host, settings.carla.rpc_port)
                client.set_timeout(settings.carla.timeout_seconds)
                state.carla_client = client
                state.map_manager = MapManager(client)
            except Exception as exc:  # noqa: BLE001  SDK 存在但构建失败不应阻断服务启动
                state.carla_client = None
                state.carla_client_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    f"CARLA 客户端初始化失败（{_carla_target(settings)}）：{state.carla_client_error}"
                )

    # 启动期即给出可诊断原因（无需等首个巡检周期）
    if getattr(state, "carla_client", None) is None:
        state.carla_probe_error = _describe_unavailable(state, settings)

    monitor = getattr(state, "health_monitor", None)
    if monitor is None:

        def _check_instance(instance_id: str) -> bool:
            """实例健康探针：实例表中不存在视为健康（将由清理周期注销）。"""
            mgr = getattr(state, "instance_manager", None)
            inst = mgr.get_instance(instance_id) if mgr is not None else None
            if inst is None:
                return True
            if inst.status == InstanceStatus.FAILED:
                return False
            runner = getattr(state, "scene_runners", {}).get(instance_id)
            if runner is not None:
                snapshot = runner.get_state_snapshot()
                if snapshot is not None and snapshot.status == SceneStatus.FAILED:
                    return False
            return True

        def _cleanup_expired() -> list[str]:
            """超时回收 + app.state 指标刷新（/health、/metrics 的数据源）。"""
            mgr = getattr(state, "instance_manager", None)
            live_ids: set[str] = set()
            if mgr is not None:
                instances_list = mgr.list_instances()
                live_ids = {i.sim_instance_id for i in instances_list if i.status == InstanceStatus.RUNNING}
                # 同步监控集合：新增运行实例、注销已销毁实例（防 _watched 单调增长）
                mon = getattr(state, "health_monitor", None)
                if mon is not None:
                    for iid in live_ids:
                        mon.register_instance(iid)
                    for iid in mon.watched_ids():
                        if iid not in live_ids:
                            mon.unregister_instance(iid)
            state.active_instance_count = len(live_ids)
            _refresh_carla_state(state, settings)
            return list(mgr.check_expired_instances()) if mgr is not None else []

        monitor = InstanceHealthMonitor(
            settings=settings.resource,
            check_callback=_check_instance,
            expired_cleanup_callback=_cleanup_expired,
        )
        state.health_monitor = monitor
        monitor.start()

    return monitor


def create_app(settings: HunterSimSettings | None = None) -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    Args:
        settings: HunterSim 全局配置，None 时使用环境变量默认值。

    Returns:
        配置完成的 FastAPI 应用。
    """
    if settings is None:
        settings = HunterSimSettings()

    # 注入全局配置到 deps 模块
    set_app_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(f"HunterSim API starting (env={settings.env}, port={settings.api.port})")
        monitor = _assemble_services(app, settings)
        try:
            yield
        finally:
            if monitor is not None:
                monitor.stop()
            logger.info("HunterSim API shutting down")

    app = FastAPI(
        title="HunterSim API",
        version="2.1.2",
        description="HUNTER SE VIL 仿真平台 REST API",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url="/api/v1/sim/docs",
        redoc_url="/api/v1/sim/redoc",
        openapi_url="/api/v1/sim/openapi.json",
    )

    # ── 中间件（顺序：后添加的先执行）────────────────────────────────────────
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        JWTAuthMiddleware,
        api_settings=settings.api,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 异常处理 ──────────────────────────────────────────────────────────────
    register_exception_handlers(app)

    # 用户级并发配额（设计文档 §14.2：单用户默认 5 个实例）
    app.state.quota_manager = ResourceQuotaManager(
        max_per_user=settings.resource.max_instances_per_user,
        max_total=settings.resource.max_instances_total,
    )

    # ── 路由注册 ──────────────────────────────────────────────────────────────
    api_prefix = "/api/v1/sim"
    app.include_router(health.router, prefix=api_prefix, tags=["health"])
    app.include_router(instances.router, prefix=f"{api_prefix}/instances", tags=["instances"])
    app.include_router(scenes.router, prefix=f"{api_prefix}/scenes", tags=["scenes"])
    app.include_router(scenarios.router, prefix=f"{api_prefix}/scenarios", tags=["scenarios"])
    app.include_router(resources.router, prefix=api_prefix, tags=["resources"])
    app.include_router(websocket.router, prefix=api_prefix, tags=["websocket"])

    logger.info("HunterSim FastAPI application created")
    return app


# 应用实例（供 uvicorn 直接引用）
app = create_app()
