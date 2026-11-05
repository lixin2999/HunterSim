"""只读运维探针应用（health / metrics / viz 代理）。

设计取舍：
- 探针进程与业务进程（:8090）分离，探针本身不持有 CARLA 句柄、不写 SQLite；
  真实的 CARLA 连通性、活动实例、VIL 收帧与录制状态由管理面 ``/health``
  汇总后经 HTTP 拉取，避免两处各自探测导致结论不一致；
- 上游不可达时仍返回 **200** + ``status=degraded``（容器 liveness 不应因
  业务面短暂重启反复杀进程），失败原因写入 ``upstream.error``；
- ``/metrics`` 只导出本进程注册表（uptime + 上游可达性），业务指标由
  Prometheus 直接抓管理面的 ``/metrics``；
- 当管理面启用 TLS/mTLS 时，本应用额外注册 viz 反向代理路由（:8080 HTTP），
  让浏览器无需客户端证书即可通过 ``http://<IP>:8080/viz`` 访问可视化页面。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from prometheus_client import Gauge, generate_latest

from hunter_sim.common.metrics import SERVICE_START_TIME
from hunter_sim.common.models import HunterSimSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 上游探测超时（秒）：探针应快速返回，避免被编排器判定为假死
_UPSTREAM_TIMEOUT_S = 2.0

PROBE_UPSTREAM_REACHABLE = Gauge(
    "huntersim_probe_upstream_reachable",
    "Whether the carla-manager control plane answered the probe (1/0).",
)


def _resolve_app_version() -> str:
    """读取已安装包版本（未安装时返回 ``unknown``，不抛）。"""
    try:
        from importlib.metadata import version as _pkg_version  # noqa: PLC0415

        return _pkg_version("hunter-sim")
    except Exception:  # noqa: BLE001  源码直跑/未安装时降级
        return "unknown"


def _fetch_upstream(
    adapter_url: str, ssl_kwargs: dict[str, Any] | None = None
) -> tuple[dict[str, object] | None, str | None]:
    """拉取管理面 ``/health`` 快照。

    Args:
        adapter_url: 管理面基础地址（如 ``http://127.0.0.1:8090``）。
        ssl_kwargs: httpx SSL 参数（TLS 启用时传入 cert/verify）。

    Returns:
        ``(快照, 失败原因)``；成功时失败原因为 None。
    """
    if not adapter_url:
        return None, "adapter_url not configured"
    try:
        import httpx  # noqa: PLC0415  惰性导入：探针无 httpx 时仍可启动
    except ImportError:
        return None, "httpx not installed"
    url = adapter_url.rstrip("/") + "/health"
    try:
        kwargs: dict[str, Any] = {"timeout": _UPSTREAM_TIMEOUT_S}
        if ssl_kwargs:
            kwargs.update(ssl_kwargs)
        resp = httpx.get(url, **kwargs)
    except Exception as exc:  # noqa: BLE001  连接失败/超时统一为不可达
        return None, f"{type(exc).__name__}: {exc}"
    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid json: {exc}"
    return payload if isinstance(payload, dict) else None, None


def _derive_status(snapshot: dict[str, object] | None) -> str:
    """由上游快照推导探针状态（不把上游的 degraded 当成 ok）。

    Args:
        snapshot: 管理面 ``/health`` 快照；不可达时为 None。

    Returns:
        ``ok`` / ``degraded`` 或上游自报的其他状态值。
    """
    if snapshot is None:
        return "degraded"
    status = str(snapshot.get("status") or "ok")
    if status != "ok":
        return status
    # 上游可达但自报 CARLA 探测异常：同样是降级（否则 readiness 不会摘流量）
    if snapshot.get("carla_probe_error"):
        return "degraded"
    return "ok"


def _health_body(settings: HunterSimSettings) -> dict[str, object]:
    """构造探针健康响应体（上游可达时透传关键字段）。"""
    from hunter_sim.probes.viz_proxy import build_ssl_kwargs, resolve_upstream_base

    # TLS 启用时自动计算 https upstream；非 TLS 保持原有 adapter_url 配置
    if settings.adapter.tls_enabled:
        adapter_url = resolve_upstream_base(settings)
        ssl_kwargs = build_ssl_kwargs(settings) or None
    else:
        adapter_url = settings.probe.adapter_url
        ssl_kwargs = None
    snapshot, error = _fetch_upstream(adapter_url, ssl_kwargs)
    PROBE_UPSTREAM_REACHABLE.set(1 if snapshot is not None else 0)

    body: dict[str, object] = {
        "status": _derive_status(snapshot),
        "version": _resolve_app_version(),
        "uptime_seconds": round(time.time() - SERVICE_START_TIME, 3),
        "upstream": {
            "url": adapter_url,
            "reachable": snapshot is not None,
            "error": error,
        },
    }
    # 上游不可达时字段显式置空：让排障者一眼看出是"没数据"而非"数据为假"
    for key in (
        "carla_connected",
        "carla_probe_error",
        "active_instance",
        "vil_state",
        "sensor",
        "cloud",
    ):
        body[key] = snapshot.get(key) if snapshot is not None else None
    return body


def create_probes_app(settings: HunterSimSettings | None = None) -> FastAPI:
    """构造探针应用。

    当管理面启用 TLS/mTLS 时，额外注册 viz 反向代理路由，
    让浏览器通过 ``http://<host>:<probe_port>/viz`` 直接访问可视化页面。

    Args:
        settings: 全局配置（None 时从环境加载）。

    Returns:
        FastAPI 应用。
    """
    import httpx

    from hunter_sim.probes.viz_proxy import (
        _PROXY_TIMEOUT,
        build_ssl_kwargs,
        register_viz_proxy,
        resolve_upstream_base,
    )

    resolved = settings or HunterSimSettings()
    tls_enabled = resolved.adapter.tls_enabled

    # 构建 lifespan：TLS 时管理 httpx client 生命周期
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        client: httpx.AsyncClient | None = None
        if tls_enabled:
            ssl_kwargs = build_ssl_kwargs(resolved)
            try:
                client = httpx.AsyncClient(timeout=_PROXY_TIMEOUT, **ssl_kwargs)
            except (FileNotFoundError, OSError) as exc:
                logger.warning(f"Viz proxy client init failed (cert issue): {exc}")
            if client is not None:
                app.state.viz_proxy_client = client
                app.state.viz_proxy_upstream = resolve_upstream_base(resolved)
        try:
            yield
        finally:
            if client is not None:
                await client.aclose()

    app = FastAPI(
        title="HunterSim Probes",
        version=_resolve_app_version(),
        description="Read-only health, metrics, and viz proxy endpoints for HunterSim.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health", summary="健康检查（含 CARLA / VIL / 传感器 / 云端聚合状态）")
    def health() -> dict[str, object]:
        """聚合健康快照；上游不可达时返回 200 + degraded。"""
        return _health_body(resolved)

    @app.get("/health/live", summary="存活探针")
    def health_live() -> dict[str, object]:
        """进程存活（永远 200，供 livenessProbe）。"""
        return {"status": "ok"}

    @app.get("/health/ready", summary="就绪探针")
    def health_ready() -> dict[str, object]:
        """上游可达才为 ready（供 readinessProbe）。"""
        body = _health_body(resolved)
        return {"status": body["status"], "upstream": body["upstream"]}

    @app.get("/metrics", response_class=PlainTextResponse, summary="Prometheus 指标")
    def metrics() -> PlainTextResponse:
        """导出本进程注册表（业务指标由管理面 ``/metrics`` 提供）。"""
        return PlainTextResponse(
            generate_latest().decode("utf-8"),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ── mTLS 场景：注册 viz 反向代理路由 ────────────────────────────────
    if tls_enabled:
        # 代理路由内部延迟从 app.state 取 client（lifespan 在路由注册后执行）。
        register_viz_proxy(app, resolved)

    logger.info(
        f"Probes app created (adapter_url={resolved.probe.adapter_url}, "
        f"viz_proxy={'enabled' if tls_enabled else 'disabled'})"
    )
    return app


__all__ = ["create_probes_app"]
