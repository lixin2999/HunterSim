"""FastAPI 应用主入口（PROMPT-API-001）。

HunterSim REST API 服务，基础路径 /api/v1/sim。
"""

from __future__ import annotations

import shutil
import time
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from hunter_sim.api.deps import set_app_settings
from hunter_sim.api.error_handlers import register_exception_handlers
from hunter_sim.api.middleware.auth import JWTAuthMiddleware
from hunter_sim.api.middleware.logging import RequestLogMiddleware, set_audit_sink
from hunter_sim.api.middleware.rate_limit import (
    RateLimitMiddleware,
    LoginFailureGuard,
)
from hunter_sim.api.routers import (
    audit,
    auth,
    execution,
    health,
    instances,
    platform,
    resources,
    scene_library,
    scenarios,
    scenes,
    websocket,
)
from hunter_sim.api.runtime import (
    init_runtime_state,
    release_all_scene_runners,
    release_scene_runner,
    renew_execution_heartbeats,
)
from hunter_sim.common.metrics import CARLA_RPC_ERRORS_TOTAL
from hunter_sim.common.models import HunterSimSettings, InstanceStatus, SceneStatus
from hunter_sim.common.utils import configure_logging, get_logger
from hunter_sim.engine.map_manager import MapManager
from hunter_sim.execution.engine_pool import parse_engine_endpoints
from hunter_sim.persistence.state_store import StateStore
from hunter_sim.resource_manager.gpu_resource_pool import GPUResourcePool
from hunter_sim.resource_manager.health_monitor import InstanceHealthMonitor, ResourceQuotaManager
from hunter_sim.resource_manager.instance_manager import SimInstanceManager

logger = get_logger(__name__)


def _resolve_app_version() -> str:
    """从已安装包元数据读取版本，避免与 pyproject.toml 硬编码漂移；未安装时降级。"""
    try:
        return _pkg_version("hunter-sim")
    except PackageNotFoundError:  # 源码直跑/未 pip install 时的兜底
        return "0.0.0+unknown"


_APP_VERSION = _resolve_app_version()

# 探测失败摘要最大长度：异常文本可能很长，/health 只保留可读部分
_MAX_PROBE_ERROR_CHARS = 200

# 生产环境必须覆盖的默认值（与 APISettings / docker-compose / .env.example 默认值一致）
_INSECURE_JWT_SECRETS = frozenset(
    {
        "CHANGE_ME_IN_PRODUCTION",
        "dev-only-change-me-at-least-32-characters",
    }
)
_MIN_PROD_SECRET_LEN = 32


def _enforce_production_guards(settings: HunterSimSettings) -> None:
    """生产环境启动闸门：默认密钥 / 通配 CORS 直接拒绝启动。

    dev/staging 保持宽松，便于本地与联调；prod 下与默认值共存等同公开鉴权密钥。

    Raises:
        RuntimeError: 配置不满足生产安全基线。
    """
    if settings.env != "prod":
        return
    secret = settings.api.jwt_secret.get_secret_value()
    if secret in _INSECURE_JWT_SECRETS or len(secret) < _MIN_PROD_SECRET_LEN:
        raise RuntimeError(
            "HUNTER_SIM_ENV=prod 必须通过 API_JWT_SECRET 注入至少 "
            f"{_MIN_PROD_SECRET_LEN} 位的随机密钥（当前使用默认值或长度不足）；"
            "参考 docs/单机部署指南.md 的密钥生成方法"
        )
    if "*" in settings.api.cors_allow_origins:
        raise RuntimeError(
            "HUNTER_SIM_ENV=prod 不允许 API_CORS_ALLOW_ORIGINS 包含通配符 '*'，"
            "请显式配置前端域名"
        )
    if settings.api.admin_password.get_secret_value() == "admin":
        raise RuntimeError(
            "HUNTER_SIM_ENV=prod 必须通过 API_ADMIN_PASSWORD 覆盖默认管理员口令 'admin'"
        )


def _configure_logging(settings: HunterSimSettings) -> int:
    """P1-11 日志统一：级别 + JSON 结构化开关 + 可选轮转文件，request_id 由中间件贯穿。"""
    level = configure_logging(
        log_level=settings.log_level,
        json_logs=settings.log_json,
        log_file=settings.log_file,
        log_max_bytes=settings.log_max_bytes,
        log_backup_count=settings.log_backup_count,
    )
    logger.info(
        f"Logging configured: level={settings.log_level} "
        f"format={'json' if settings.log_json else 'text'} "
        f"file={settings.log_file or 'stdout-only'}"
    )
    return level


def _refresh_disk_usage(state: Any) -> None:
    """刷新磁盘使用率指标（/metrics 的 huntersim_disk_usage_ratio 数据源）。"""
    try:
        usage = shutil.disk_usage(".")
    except OSError as exc:  # noqa: BLE001  磁盘不可读时指标置空而不是崩溃
        logger.debug(f"disk_usage probe failed: {exc}")
        state.disk_usage_ratio = None
        return
    state.disk_usage_ratio = (usage.used / usage.total) if usage.total else 0.0


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
    """刷新 CARLA 连通状态与 RPC 延迟（健康巡检周期调用）。

    仅在首次评估或状态翻转时记录日志，避免每周期刷屏；失败原因写入
    app.state.carla_probe_error，供 /health 与运维排查直接读取。
    """
    target = _carla_target(settings)
    started = time.perf_counter()
    connected, error = _probe_carla(getattr(state, "carla_client", None))
    elapsed = time.perf_counter() - started
    previous: bool = getattr(state, "carla_connected", False)
    first_evaluation = not getattr(state, "carla_probe_evaluated", False)

    state.carla_connected = connected
    state.carla_probe_evaluated = True
    state.carla_rpc_latency_seconds = elapsed if connected else None
    if connected:
        state.carla_probe_error = None
    else:
        CARLA_RPC_ERRORS_TOTAL.inc()
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
    # 执行面/持久化配置供 runtime 惰性装配读取
    state.settings = settings
    # V2.6.0：提前创建运行器注册表与唯一一把装配锁（消除首次并发惰性建锁竞态）
    init_runtime_state(app)

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

    # 状态持久化（V2.4.0）：实例表与场景绑定落 SQLite；失败降级为内存态
    if getattr(state, "state_store", None) is None and settings.state.enabled:
        try:
            state.state_store = StateStore(
                settings.state.db_path, settings.state.busy_timeout_seconds
            )
            state.state_store.normalize_scene_statuses()
            # V2.5.0：重启不保留执行器，未完成的批量任务统一标记 failed
            interrupted = state.state_store.normalize_batch_tasks()
            if interrupted:
                logger.warning(f"Marked {interrupted} interrupted batch task(s) as failed")
        except Exception as exc:  # noqa: BLE001  存储不可用时服务仍可启动
            state.state_store = None
            logger.error(f"State persistence disabled (init failed): {exc}")

    # V2.5.0：审计落库（可选）；sink 只依赖 state_store，不阻断启动
    store = getattr(state, "state_store", None)
    if store is not None and settings.platform.audit_enabled:
        set_audit_sink(store.append_audit)
    else:
        set_audit_sink(None)

    if getattr(state, "instance_manager", None) is None:
        endpoints = parse_engine_endpoints(
            settings.execution.engine_endpoints,
            settings.carla,
            settings.execution.max_engines,
        )
        state.execution_endpoints = endpoints
        state.instance_manager = SimInstanceManager(
            settings=settings.resource,
            gpu_pool=getattr(state, "gpu_pool", None),
            quota_manager=getattr(state, "quota_manager", None),
            carla=settings.carla,
            engine_endpoints=endpoints,
            state_store=getattr(state, "state_store", None),
            platform=settings.platform,
        )
        if settings.state.recover_instances:
            restored = state.instance_manager.restore()
            state.restored_instance_count = restored
            if restored:
                logger.info(
                    f"Recovered {restored} instance(s) from {settings.state.db_path}"
                )

    if getattr(state, "scene_runners", None) is None:
        state.scene_runners = {}  # init_runtime_state 已预建；保留兼容外部注入路径

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
            _refresh_disk_usage(state)
            # V2.7.0 P1-7：对存活运行器续引擎心跳（僵尸持有者因无存活循环而追旧）
            renew_execution_heartbeats(app)
            expired = list(mgr.check_expired_instances()) if mgr is not None else []
            # 超时实例同样要回收场景运行器（否则 tick 线程与 Actor 残留）
            for instance_id in expired:
                release_scene_runner(app, instance_id)
            return expired

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

    # 启动闸门与日志级别：必须在任何组件日志输出前生效
    _enforce_production_guards(settings)
    _configure_logging(settings)

    # 注入全局配置到 deps 模块
    set_app_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(f"HunterSim API starting (env={settings.env}, port={settings.api.port})")
        monitor = _assemble_services(app, settings)
        try:
            yield
        finally:
            # 先停场景循环（停止 tick、销毁 Actor），再停巡检线程
            released = release_all_scene_runners(app)
            if released:
                logger.info(f"Released {released} scene runtime(s) on shutdown")
            if monitor is not None:
                monitor.stop()
            set_audit_sink(None)
            # 先释放执行面/停监控，再关闭持久化连接
            store = getattr(app.state, "state_store", None)
            if store is not None:
                try:
                    store.close()
                except Exception as exc:  # noqa: BLE001  关闭失败不影响退出
                    logger.warning(f"State store close failed: {exc}")
            logger.info("HunterSim API shutting down")

    app = FastAPI(
        title="HunterSim API",
        version=_APP_VERSION,
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
        api_keys_enabled=settings.platform.api_keys_enabled,
    )
    # V2.6.0：按客户端 IP 的 API 限流（消费 API_RATE_LIMIT_PER_MINUTE）；
    # 健康探针/Prometheus 抓取/WebSocket 握手豁免。先于 CORS 添加 → 位于
    # CORS 内层，429 响应仍会附加 CORS 头
    app.add_middleware(
        RateLimitMiddleware,
        rate_per_minute=settings.api.rate_limit_per_minute,
        enabled=settings.api.rate_limit_enabled,
        exempt_prefixes=(
            "/api/v1/sim/health",
            "/api/v1/sim/metrics",
            "/api/v1/sim/ws/",
        ),
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
    # V2.6.0：/auth/token 失败退避守卫（按客户端 IP，进程内计数）
    app.state.login_failure_guard = LoginFailureGuard(
        max_failures=settings.api.login_max_failed_attempts,
        base_seconds=settings.api.login_backoff_base_seconds,
        max_seconds=settings.api.login_backoff_max_seconds,
    )

    # ── 路由注册 ──────────────────────────────────────────────────────────────
    api_prefix = "/api/v1/sim"
    app.include_router(health.router, prefix=api_prefix, tags=["health"])
    app.include_router(auth.router, prefix=api_prefix, tags=["auth"])
    app.include_router(instances.router, prefix=f"{api_prefix}/instances", tags=["instances"])
    app.include_router(scenes.router, prefix=f"{api_prefix}/scenes", tags=["scenes"])
    app.include_router(
        scene_library.router, prefix=f"{api_prefix}/scenes", tags=["scene-library"]
    )
    app.include_router(scenarios.router, prefix=f"{api_prefix}/scenarios", tags=["scenarios"])
    app.include_router(execution.router, prefix=api_prefix, tags=["execution"])
    app.include_router(platform.router, prefix=api_prefix, tags=["platform"])
    app.include_router(audit.router, prefix=api_prefix, tags=["audit"])
    app.include_router(resources.router, prefix=api_prefix, tags=["resources"])
    app.include_router(websocket.router, prefix=api_prefix, tags=["websocket"])

    logger.info("HunterSim FastAPI application created")
    return app


# 应用实例（供 uvicorn 直接引用）
app = create_app()
