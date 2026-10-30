"""CARLA 管理面控制平面（HunterCore 侧 ``carla-manager``）。

10 个端点（端口默认 8090，仅内网，无鉴权头 / 无 Ingress）：

==========================================  ======================================
``POST   /api/v1/instances``                创建实例（立即返回，世界在后台加载）
``GET    /api/v1/instances``                按 ``scene_id`` 查询（空查询返回空列表）
``POST   /api/v1/instances/{id}/scenario``  下发/重发场景配置
``GET    /api/v1/instances/{id}``           进度快照
``GET    /api/v1/instances/{id}/result``    终态结果
``POST   /api/v1/instances/{id}/pause``     暂停
``POST   /api/v1/instances/{id}/resume``    恢复
``POST   /api/v1/instances/{id}/stop``      停止并回收
``GET    /api/v1/instances/{id}/screenshot`` 单帧截图（image/jpeg | image/png）
``GET    /api/v1/instances/{id}/stream.mjpg`` MJPEG 实时画面
==========================================  ======================================

响应中的 ``status`` 使用平台词汇（pending/running/succeeded/failed/canceled），
``carla_status`` 保留 CARLA 原始状态（CREATED/LOADING/READY/RUNNING/PAUSED/...）。

运维端点：``GET /health``（聚合快照，供 ``huntersim probes`` 代理）与
``GET /metrics``（本进程 Prometheus 注册表）。

V3.1.0 追加可视化客户端（只读、独立前缀，不进 OpenAPI）：``GET /viz`` 页面、
``GET /viz/api/status|logs|logs/stream`` 与 ``/viz/api/instances/{id}/events|sensors``；
页面复用既有 ``/screenshot`` 与 ``/stream.mjpg`` 回传画面。
"""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, AsyncIterator, Callable, Optional, cast

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from prometheus_client import generate_latest

from hunter_sim.common.metrics import observe_http_request, refresh_runtime_gauges
from hunter_sim.common.models import HunterSimSettings, VizSettings
from hunter_sim.common.utils import get_logger
from hunter_sim.execution.engine_pool import EnginePool, EngineEndpoint, parse_engine_endpoints
from hunter_sim.execution.plane import ExecutionPlane
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.executor import SimulationExecutor
from hunter_sim.platform_adapter.models import (
    CreateInstanceRequest,
    InstanceRecord,
    SubmitScenarioRequest,
)
from hunter_sim.platform_adapter.service import AdapterService, StreamLimitExceeded
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.sensor_sim.frame_hub import Frame, FrameHub
from hunter_sim.visualization.logbus import LogBus
from hunter_sim.visualization.routes import register_viz_routes

logger = get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090


def build_execution_plane(settings: HunterSimSettings) -> ExecutionPlane:
    """构造适配器专用执行平面（单世界串行，惰性创建 MapManager）。"""
    endpoints = parse_engine_endpoints(
        settings.execution.engine_endpoints,
        settings.carla,
        settings.execution.max_engines,
    )
    lease_ttl = (
        float(settings.execution.lease_ttl_seconds)
        if settings.execution.lease_heartbeat_enabled
        else 0.0
    )
    pool = EnginePool(endpoints, lease_ttl_seconds=lease_ttl)
    managers: dict[str, Any] = {}
    managers_lock = threading.RLock()

    def resolve(endpoint: EngineEndpoint) -> Optional[Any]:
        """按端点惰性创建 MapManager（无 CARLA SDK 时返回 None → 执行面不可用）。"""
        with managers_lock:
            cached = managers.get(endpoint.key)
            if cached is not None:
                return cached
        try:
            import carla  # noqa: PLC0415  惰性导入：仅真实 CARLA 环境可用
            from hunter_sim.engine.map_manager import MapManager  # noqa: PLC0415

            client = carla.Client(endpoint.host, endpoint.rpc_port)
            client.set_timeout(float(settings.carla.timeout_seconds))
            manager = MapManager(client)
        except Exception as exc:  # noqa: BLE001  端点不可达 / SDK 缺失
            logger.warning(f"Adapter map manager unavailable for {endpoint.key}: {exc}")
            return None
        with managers_lock:
            managers[endpoint.key] = manager
        return manager

    return ExecutionPlane(
        pool,
        resolve,
        fixed_delta_seconds=float(settings.carla.fixed_delta_seconds),
        cloud_settings=settings.cloud,
    )


def create_app(
    settings: Optional[HunterSimSettings] = None,
    adapter_settings: Optional[AdapterSettings] = None,
    *,
    plane: Optional[ExecutionPlane] = None,
    store: Optional[InstanceStore] = None,
    executor: Optional[SimulationExecutor] = None,
    service: Optional[AdapterService] = None,
    frame_hub_provider: Optional[Callable[[str], Optional[FrameHub]]] = None,
) -> FastAPI:
    """构造控制平面应用。

    Args:
        settings: 全局配置（None 时从环境加载）。
        adapter_settings: 适配器配置（None 时从 ``ADAPTER_*`` 环境加载）。
        plane/store/executor/service: 测试注入点；注入 ``service`` 时应用不接管生命周期。
        frame_hub_provider: 帧汇聚中心查询器（测试注入用；默认委托执行器）。
    """
    owned: dict[str, Any] = {
        "store": None,
        "executor": None,
        "service": service,
    }
    # 可视化客户端依赖：全局配置（状态面板）+ 进程内日志总线（日志面板）
    resolved: dict[str, Any] = {
        "settings": settings,
        "viz": None,
        "bus": None,
    }

    def _resolve_settings() -> Optional[HunterSimSettings]:
        """取全局配置（已加载则复用；否则尝试加载，失败降级为 None）。"""
        if resolved["settings"] is not None:
            return cast(HunterSimSettings, resolved["settings"])
        try:
            resolved["settings"] = HunterSimSettings()
        except Exception as exc:  # noqa: BLE001  可视化不得因配置异常阻断服务启动
            logger.warning(f"Viz settings unavailable, using defaults: {exc}")
            resolved["settings"] = None
        return cast(Optional[HunterSimSettings], resolved["settings"])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime_settings = _resolve_settings()
        viz = runtime_settings.viz if runtime_settings is not None else VizSettings()
        bus = LogBus(max_records=viz.log_buffer_size)
        if viz.enabled:
            bus.attach()
        resolved["viz"], resolved["bus"] = viz, bus
        app.state.settings = runtime_settings
        app.state.viz_settings = viz
        app.state.log_bus = bus
        app.state.started_at = time.time()
        if owned["service"] is None:
            if runtime_settings is None:
                # 可视化可以降级，但真正接管 CARLA 的执行链必须有配置
                raise RuntimeError(
                    "HunterSimSettings 加载失败，无法启动控制面；请检查 .env 配置"
                )
            runtime_adapter = adapter_settings or AdapterSettings()
            runtime_store = store or InstanceStore(
                runtime_adapter.db_path,
                runtime_adapter.busy_timeout_seconds,
            )
            runtime_store.connect()
            # 重启恢复：上次进程遗留的 pending/running 一律 failed，避免平台永久轮询
            runtime_store.mark_restarted_instances_failed("adapter restarted")
            runtime_plane = plane or build_execution_plane(runtime_settings)
            runtime_executor = executor or SimulationExecutor(
                runtime_settings,
                runtime_adapter,
                runtime_store,
                runtime_plane,
            )
            owned["store"] = runtime_store
            owned["executor"] = runtime_executor
            owned["service"] = AdapterService(
                runtime_settings,
                runtime_adapter,
                runtime_store,
                runtime_executor,
                frame_hub_provider=frame_hub_provider,
            )
            runtime_executor.start()
            logger.info(
                f"CARLA adapter control plane ready on "
                f"{runtime_adapter.host}:{runtime_adapter.port}"
            )
        app.state.adapter_service = owned["service"]
        try:
            yield
        finally:
            if owned["executor"] is not None:
                owned["executor"].stop()
            if owned["store"] is not None:
                owned["store"].close()
            owned["service"] = None
            bus.detach()
            app.state.log_bus = None

    app = FastAPI(
        title="HunterSim CARLA Manager",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    _register_routes(app)
    register_viz_routes(app)
    app.add_middleware(_LatencyMiddleware)
    return app


class _LatencyMiddleware:
    """记录管理面请求延迟直方图（ASGI 中间件，低基数路径标签）。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500

        async def _send(message: Any) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            observe_http_request(
                scope.get("method", "GET"),
                scope.get("path", "/"),
                status_code,
                time.perf_counter() - started,
            )


def _register_routes(app: FastAPI) -> None:
    """注册 10 个平台控制面端点与运维端点（health/metrics）。"""

    def _service(request: Request) -> AdapterService:
        service = getattr(request.app.state, "adapter_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="adapter not ready")
        return cast(AdapterService, service)

    def _not_found(instance_id: str) -> HTTPException:
        return HTTPException(status_code=404, detail=f"instance not found: {instance_id}")

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        """聚合健康快照（容器 liveness/readiness 与 ``huntersim probes`` 上游）。"""
        service = getattr(request.app.state, "adapter_service", None)
        if service is None:
            return {"status": "degraded", "carla_connected": None}
        body: dict[str, Any] = {"status": "ok"}
        snapshot = getattr(service, "health_snapshot", None)
        if callable(snapshot):
            try:
                body.update(snapshot())
            except Exception as exc:  # noqa: BLE001  健康检查不得因瞬时异常 500
                logger.warning(f"Health snapshot failed: {exc}")
                body["status"] = "degraded"
        return body

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(request: Request) -> PlainTextResponse:
        """Prometheus 指标（抓取时刷新运行时 Gauge）。"""
        service = getattr(request.app.state, "adapter_service", None)
        gauges: dict[str, Any] = {}
        sampler = getattr(service, "runtime_gauges", None)
        if callable(sampler):
            try:
                gauges = sampler()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Runtime gauge sampling failed: {exc}")
        refresh_runtime_gauges(**gauges)
        return PlainTextResponse(
            generate_latest().decode("utf-8"),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.post("/api/v1/instances")
    def create_instance(
        payload: CreateInstanceRequest, request: Request
    ) -> dict[str, Any]:
        """创建仿真实例（立即返回；地图加载在后台完成）。"""
        record = _service(request).create(payload)
        return _instance_view(record)

    @app.get("/api/v1/instances")
    def list_instances(
        request: Request,
        scene_id: Optional[str] = Query(None, description="按场景过滤；空表示全部"),
    ) -> dict[str, Any]:
        """查询实例列表（平台 ``find_active_instance`` 消费）。"""
        records = _service(request).list(scene_id)
        return {"items": [_instance_view(record) for record in records]}

    @app.post("/api/v1/instances/{instance_id}/scenario")
    def submit_scenario(
        instance_id: str, payload: SubmitScenarioRequest, request: Request
    ) -> dict[str, Any]:
        """下发/重发场景配置（``param_overrides`` 已由平台合并，此处不再应用）。"""
        record = _service(request).submit(instance_id, payload)
        if record is None:
            raise _not_found(instance_id)
        return _instance_view(record)

    @app.get("/api/v1/instances/{instance_id}")
    def get_instance(instance_id: str, request: Request) -> dict[str, Any]:
        """进度快照（404 → 平台 3001）。"""
        record = _service(request).get(instance_id)
        if record is None:
            raise _not_found(instance_id)
        return _instance_view(record)

    @app.get("/api/v1/instances/{instance_id}/result")
    def get_result(instance_id: str, request: Request) -> dict[str, Any]:
        """结果视图（非终态也返回 200 + 当前状态，由平台映射为 3003）。"""
        record = _service(request).result(instance_id)
        if record is None:
            raise _not_found(instance_id)
        return record.to_result_view()

    @app.post("/api/v1/instances/{instance_id}/pause")
    def pause_instance(instance_id: str, request: Request) -> dict[str, Any]:
        """暂停实例（幂等）。"""
        return _control(_service(request).pause(instance_id), instance_id)

    @app.post("/api/v1/instances/{instance_id}/resume")
    def resume_instance(instance_id: str, request: Request) -> dict[str, Any]:
        """恢复实例（幂等）。"""
        return _control(_service(request).resume(instance_id), instance_id)

    @app.post("/api/v1/instances/{instance_id}/stop")
    def stop_instance(instance_id: str, request: Request) -> dict[str, Any]:
        """停止实例并回收 CARLA 资源（幂等）。"""
        return _control(_service(request).stop(instance_id), instance_id)

    @app.get("/api/v1/instances/{instance_id}/screenshot")
    def screenshot(instance_id: str, request: Request) -> Response:
        """当前 RGB 相机单帧截图（未挂相机→409，实例不存在→404）。"""
        service = _service(request)
        if service.get(instance_id) is None:
            raise _not_found(instance_id)
        frame = service.screenshot(instance_id)
        if frame is None:
            raise HTTPException(status_code=409, detail="camera_not_attached")
        media_type = str(frame.meta.get("content_type") or "image/jpeg")
        return Response(
            content=frame.payload,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/instances/{instance_id}/stream.mjpg")
    async def stream_mjpeg(instance_id: str, request: Request) -> StreamingResponse:
        """MJPEG 实时画面（``multipart/x-mixed-replace``，客户端断开即结束）。

        异步端点：等待用 ``asyncio.sleep``，不占用 anyio 工作线程。
        并发客户端超 ``ADAPTER_STREAM_MAX_CLIENTS`` 时返回 503（而不是把控制面拖满）。
        """
        service = _service(request)
        if service.get(instance_id) is None:
            raise _not_found(instance_id)
        if service.screenshot(instance_id) is None:
            raise HTTPException(status_code=409, detail="camera_not_attached")
        frames = service.stream_frames(instance_id)
        # 先拉首帧：名额超限（StreamLimitExceeded）只能在首次迭代抛出，
        # 此时返回 503 比“先 200 再中途断流”对平台友好
        try:
            first = await anext(frames)
        except StopAsyncIteration:
            await frames.aclose()
            raise HTTPException(status_code=409, detail="camera_not_attached") from None
        except StreamLimitExceeded as exc:
            raise HTTPException(status_code=503, detail="stream_limit_reached") from exc
        return StreamingResponse(
            _mjpeg_chunks(frames, first),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store", "Connection": "close"},
        )


async def _mjpeg_chunks(
    frames: AsyncGenerator[Frame, None], first: Frame
) -> AsyncGenerator[bytes, None]:
    """把帧包装为 MJPEG 分片（Content-Type 跟随实际编码 JPEG/PNG）。

    Args:
        frames: 已占用推流名额的异步帧流。
        first: 路由层预先拉取的首帧（避免重复等待）。

    Yields:
        单个 MJPEG 分片字节串。
    """
    try:
        yield _mjpeg_chunk(first)
        async for frame in frames:
            yield _mjpeg_chunk(frame)
    finally:
        # 客户端断开时关闭上游生成器，保证推流名额立即释放
        await frames.aclose()


def _mjpeg_chunk(frame: Frame) -> bytes:
    """单帧 MJPEG 分片（边界 + Content-Type/Length + 原始字节）。"""
    payload = frame.payload
    content_type = str(frame.meta.get("content_type") or "image/jpeg")
    return (
        b"--frame\r\n"
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n"
        b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n"
        + payload
        + b"\r\n"
    )


def _instance_view(record: InstanceRecord) -> dict[str, Any]:
    """实例视图（状态为平台词汇，附带 ``carla_status``）。"""
    return record.to_view()


def _control(record: Optional[InstanceRecord], instance_id: str) -> dict[str, Any]:
    """控制端点统一响应。"""
    if record is None:
        raise HTTPException(status_code=404, detail=f"instance not found: {instance_id}")
    return {
        "sim_instance_id": record.instance_id,
        "instance_id": record.instance_id,
        "id": record.instance_id,
        "status": record.status,
        "carla_status": record.carla_status,
        "message": record.message,
    }


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "build_execution_plane", "create_app"]
