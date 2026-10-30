"""可视化客户端只读路由（挂在控制面 :8090 的 ``/viz`` 下）。

设计口径（与 V3.0.0 §16.1「单一控制面」一致）：

- **不是第二套业务面**：``/viz/api/*`` 全部是只读视图，不创建/修改实例、
  不写数据库、不持有 CARLA 句柄，数据来自控制面已有的 ``FrameHub``（帧）、
  ``EventDetector``（事件）与进程内 ``LogBus``（日志）；
- **平台契约零改动**：平台消费的 10 个 ``/api/v1/*`` 端点保持原样，可视化
  端点前缀独立，且 ``include_in_schema=False`` 不进 OpenAPI 文档；
- **同源播放**：画面直接复用既有 ``/api/v1/instances/{id}/stream.mjpg``
  （MJPEG 图像流）与 ``/screenshot``，浏览器无需跨域或额外代理。

页面资产为纯静态文件（无构建步骤），由 ``/viz/assets/*`` 以白名单方式提供。
"""

from __future__ import annotations

import asyncio
import json
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, AsyncGenerator, Optional, cast

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from hunter_sim.common.models import HunterSimSettings, VizSettings
from hunter_sim.common.utils import get_logger
from hunter_sim.platform_adapter.models import ACTIVE_STATUSES
from hunter_sim.sensor_sim.frame_hub import KNOWN_STREAMS, STREAM_RGB
from hunter_sim.visualization.logbus import LogBus, LogRecord, normalize_level
from hunter_sim.visualization.sensor_view import render_stream

logger = get_logger(__name__)

#: 静态资产目录
ASSETS_DIR = Path(__file__).parent / "assets"

#: 允许直出的静态资产（白名单，天然阻断路径穿越）
ASSETS: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}

#: SSE 增量队列容量（浏览器消费慢时丢最旧，不反压日志线程）
_LOG_STREAM_QUEUE_SIZE = 512
#: SSE 心跳间隔（秒）：避免反向代理空闲断连
_LOG_STREAM_KEEPALIVE_S = 15.0

#: 未显式配置时的可视化默认值（lifespan 未装配时的兜底）
_DEFAULT_VIZ = VizSettings()


def viz_url(settings: HunterSimSettings) -> str:
    """可视化页面地址（监听通配地址时回落到 127.0.0.1）。

    Args:
        settings: 全局配置（读取 ``adapter.host/port``）。

    Returns:
        ``http://<host>:<port>/viz``。
    """
    host = (settings.adapter.host or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return f"http://{host}:{settings.adapter.port}/viz"


def register_viz_routes(app: FastAPI) -> None:
    """注册 ``/viz`` 页面与 ``/viz/api/*`` 只读端点。

    Args:
        app: 控制面 FastAPI 应用（lifespan 中装配 ``adapter_service`` /
            ``viz_settings`` / ``log_bus`` / ``started_at``）。
    """

    @app.get("/viz", response_class=HTMLResponse, include_in_schema=False)
    def viz_page() -> HTMLResponse:
        """可视化客户端页面（单页应用）。"""
        return HTMLResponse(_asset_text("index.html"))

    @app.get("/viz/assets/{name}", include_in_schema=False)
    def viz_asset(name: str) -> Response:
        """静态资产（白名单外的名字一律 404）。"""
        media_type = ASSETS.get(name)
        if media_type is None:
            raise HTTPException(status_code=404, detail=f"asset not found: {name}")
        return Response(content=_asset_bytes(name), media_type=media_type)

    @app.get("/viz/api/status", include_in_schema=False)
    def viz_status(request: Request) -> dict[str, Any]:
        """仿真端运行态汇总（顶栏状态灯 + 模块连通性）。"""
        settings = _settings(request)
        service = _service(request, required=False)
        started_at = _started_at(request)
        payload: dict[str, Any] = {
            "version": _version_text(),
            "env": settings.env if settings is not None else None,
            "uptime_s": round(max(0.0, time.time() - started_at), 1),
            "manager": {
                "host": settings.adapter.host if settings is not None else "",
                "port": settings.adapter.port if settings is not None else 0,
            },
            "probes": {
                "url": (
                    f"http://{_local_host(settings.probe.host)}:{settings.probe.port}"
                    if settings is not None
                    else ""
                ),
                "adapter_url": settings.probe.adapter_url if settings is not None else "",
            },
            "log": _log_stats(request),
            "instances": {"total": 0, "active": 0},
            "active_instance": None,
            "carla": {"endpoint": "", "connected": None},
            "platform": {"enabled": False, "gateway_url": "", "kafka_bootstrap": ""},
            "sensor": {"attached": False, "counts": {}, "streams": {}},
            "ros": {"enabled": False, "topics": {}, "mappings": []},
        }
        snapshot = getattr(service, "runtime_status", None)
        if callable(snapshot):
            try:
                payload.update(snapshot())
            except Exception as exc:  # noqa: BLE001  只读面板不得因瞬时异常 500
                logger.warning(f"Viz runtime status snapshot failed: {exc}")
        elif service is not None:
            lists = getattr(service, "list", None)
            records = list(lists() if callable(lists) else [])
            active = [item for item in records if item.status in ACTIVE_STATUSES]
            payload["instances"] = {"total": len(records), "active": len(active)}
            payload["active_instance"] = active[0].to_view() if active else None
        return payload

    @app.get("/viz/api/instances/{instance_id}/events", include_in_schema=False)
    def viz_events(
        instance_id: str,
        request: Request,
        limit: int = Query(0, ge=0, le=2000, description="只返回最近 N 条；0 表示全部"),
    ) -> dict[str, Any]:
        """场景事件（碰撞/车道偏离/超速/通过等，按类型计数）。"""
        service = _require_instance(request, instance_id)
        getter = getattr(service, "events", None)
        events: list[dict[str, Any]] = []
        if callable(getter):
            try:
                events = [dict(item) for item in getter(instance_id) or []]
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Viz events read failed for {instance_id}: {exc}")
        counts: dict[str, int] = {}
        for event in events:
            key = str(event.get("event_type") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        items = events[-limit:] if limit > 0 else events
        return {"instance_id": instance_id, "count": len(events), "counts": counts, "items": items}

    @app.get("/viz/api/instances/{instance_id}/sensors", include_in_schema=False)
    def viz_sensors(instance_id: str, request: Request) -> dict[str, Any]:
        """各传感器流的帧数/帧率/最新帧摘要 + ROS2 话题状态。"""
        service = _require_instance(request, instance_id)
        body: dict[str, Any] = {"instance_id": instance_id, "attached": False, "streams": {}}
        getter = getattr(service, "sensors", None)
        if callable(getter):
            try:
                body.update(getter(instance_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Viz sensors read failed for {instance_id}: {exc}")
        ros_getter = getattr(service, "ros", None)
        ros: dict[str, Any] = {"enabled": False, "topics": {}, "mappings": []}
        if callable(ros_getter):
            try:
                ros = dict(ros_getter(instance_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Viz ROS status read failed for {instance_id}: {exc}")
        body["ros"] = ros
        return body

    @app.get("/viz/api/instances/{instance_id}/sensors/{stream}", include_in_schema=False)
    def viz_sensor_frame(
        instance_id: str,
        stream: str,
        request: Request,
        max_points: int = Query(0, ge=0, le=50000, description="点云抽样上限；0 取配置值"),
    ) -> Response:
        """单流最新帧：图像流回原始图片字节，点云/IMU 回 JSON。"""
        service = _require_instance(request, instance_id)
        name = stream.strip().lower()
        if name not in KNOWN_STREAMS:
            raise HTTPException(status_code=404, detail=f"unknown sensor stream: {stream}")
        frame = _read_frame(service, instance_id, name)
        if frame is None:
            raise HTTPException(status_code=409, detail=f"sensor_frame_unavailable:{name}")
        viz = _viz(request)
        payload, media_type = render_stream(
            frame,
            max_points=max_points or viz.lidar_max_points,
            max_width=viz.depth_max_width,
        )
        if not payload:
            raise HTTPException(status_code=503, detail=f"sensor_render_unavailable:{name}")
        return Response(
            content=payload,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/viz/api/logs", include_in_schema=False)
    def viz_logs(
        request: Request,
        limit: int = Query(200, ge=1, le=5000, description="返回条数上限"),
        level: str = Query("INFO", description="最低级别：DEBUG/INFO/WARNING/ERROR/CRITICAL"),
        contains: str = Query("", description="关键字过滤（大小写不敏感，空表示不过滤）"),
        since_seq: int = Query(0, ge=0, description="只返回序号大于该值的记录（断线续传）"),
    ) -> dict[str, Any]:
        """进程内日志快照（历史回放 / 轮询兜底）。"""
        bus = _bus(request)
        records = bus.snapshot(
            limit=limit, level=level, contains=contains, since_seq=since_seq
        )
        items = [record.to_dict() for record in records]
        return {
            "items": items,
            "counts": bus.counts(),
            "next_since": items[-1]["seq"] if items else since_seq,
            "min_level": level.upper(),
        }

    @app.get("/viz/api/logs/stream", include_in_schema=False)
    async def viz_logs_stream(
        request: Request,
        level: str = Query("INFO", description="最低级别过滤"),
        contains: str = Query("", description="关键字过滤"),
        history: int = Query(200, ge=0, le=2000, description="首屏回放条数"),
    ) -> StreamingResponse:
        """日志增量推送（SSE：``text/event-stream``）。

        异步生成器 + ``asyncio.Queue``：日志由任意线程写入，经
        ``call_soon_threadsafe`` 转投事件循环，不占用工作线程。
        """
        bus = _bus(request)
        min_level = normalize_level(level)
        queue: asyncio.Queue[LogRecord] = asyncio.Queue(maxsize=_LOG_STREAM_QUEUE_SIZE)
        loop = asyncio.get_running_loop()

        def _push(record: LogRecord) -> None:
            if not record.passes(min_level, contains):
                return
            if queue.full():
                try:  # 消费慢：丢最旧，保持低延迟
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:  # pragma: no cover  仅在极高并发下可能
                return

        def _sink(record: LogRecord) -> None:
            try:
                loop.call_soon_threadsafe(_push, record)
            except RuntimeError:  # noqa: BLE001  事件循环已关闭（客户端已断开）
                return

        subscription = bus.subscribe(_sink)

        async def _events() -> AsyncGenerator[bytes, None]:
            try:
                yield b": connected\n\n"
                for record in bus.snapshot(limit=history, level=min_level, contains=contains):
                    yield _sse(record)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        record = await asyncio.wait_for(
                            queue.get(), timeout=_LOG_STREAM_KEEPALIVE_S
                        )
                    except asyncio.TimeoutError:
                        yield b": keep-alive\n\n"
                        continue
                    yield _sse(record)
            finally:
                subscription.close()

        return StreamingResponse(
            _events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )


# ── 内部辅助 ─────────────────────────────────────────────────────────────────


def _asset_bytes(name: str) -> bytes:
    """读取静态资产（缺失时 500，说明安装不完整）。"""
    path = ASSETS_DIR / name
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.error(f"Viz asset missing: {path}: {exc}")
        raise HTTPException(status_code=500, detail=f"asset unavailable: {name}") from exc


def _asset_text(name: str) -> str:
    """读取静态文本资产。"""
    return _asset_bytes(name).decode("utf-8")


def _service(request: Request, *, required: bool = True) -> Any:
    """取控制面编排对象（未装配时 503 / None）。"""
    service = getattr(request.app.state, "adapter_service", None)
    if service is None and required:
        raise HTTPException(status_code=503, detail="adapter not ready")
    return service


def _require_instance(request: Request, instance_id: str) -> Any:
    """校验实例存在并返回编排对象（不存在 → 404）。"""
    service = _service(request)
    getter = getattr(service, "get", None)
    record = getter(instance_id) if callable(getter) else None
    if record is None:
        raise HTTPException(status_code=404, detail=f"instance not found: {instance_id}")
    return service


def _settings(request: Request) -> Optional[HunterSimSettings]:
    """取全局配置（lifespan 未完成时为 None）。"""
    return cast(Optional[HunterSimSettings], getattr(request.app.state, "settings", None))


def _viz(request: Request) -> VizSettings:
    """取可视化配置（lifespan 未装配时用默认值）。"""
    settings = getattr(request.app.state, "viz_settings", None)
    return cast(VizSettings, settings if settings is not None else _DEFAULT_VIZ)


def _bus(request: Request) -> LogBus:
    """取日志总线（lifespan 未装配时 503）。"""
    bus = getattr(request.app.state, "log_bus", None)
    if bus is None:
        raise HTTPException(status_code=503, detail="log bus not ready")
    return cast(LogBus, bus)


def _started_at(request: Request) -> float:
    """应用启动时刻（Unix 秒）。"""
    return float(getattr(request.app.state, "started_at", time.time()))


def _log_stats(request: Request) -> dict[str, Any]:
    """日志总线容量与计数（顶栏展示）。"""
    bus = getattr(request.app.state, "log_bus", None)
    if bus is None:
        return {"capacity": 0, "records": 0, "counts": {}}
    return {"capacity": bus.capacity, "records": bus.size, "counts": bus.counts()}


def _read_frame(service: Any, instance_id: str, stream: str) -> Any:
    """读取指定流最新帧（兼容只有 ``screenshot`` 的测试替身）。"""
    getter = getattr(service, "sensor_frame", None)
    if callable(getter):
        try:
            frame = getter(instance_id, stream)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"Viz sensor frame read failed for {instance_id}/{stream}: {exc}")
            frame = None
        if frame is not None:
            return frame
    if stream != STREAM_RGB:
        return None
    fallback = getattr(service, "screenshot", None)
    if not callable(fallback):
        return None
    try:
        return fallback(instance_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Viz screenshot fallback failed for {instance_id}: {exc}")
        return None


def _local_host(host: str) -> str:
    """通配监听地址回落为 127.0.0.1（供浏览器访问）。"""
    text = (host or "").strip()
    if text in ("", "0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return text


def _sse(record: LogRecord) -> bytes:
    """单条 SSE 事件（``data: <json>``）。"""
    payload = json.dumps(record.to_dict(), ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


def _version_text() -> str:
    """包版本（源码直跑无元数据时降级）。"""
    try:
        return version("hunter-sim")
    except Exception:  # noqa: BLE001
        return "0.0.0+unknown"


__all__ = ["ASSETS", "ASSETS_DIR", "register_viz_routes", "viz_url"]
