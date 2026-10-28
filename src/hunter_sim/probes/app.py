"""只读运维探针应用（health / metrics）。

设计取舍：
- 探针进程与业务进程（:8090）分离，探针本身不持有 CARLA 句柄、不写 SQLite；
  真实的 CARLA 连通性、活动实例、VIL 收帧与录制状态由管理面 ``/health``
  汇总后经 HTTP 拉取，避免两处各自探测导致结论不一致；
- 上游不可达时仍返回 **200** + ``status=degraded``（容器 liveness 不应因
  业务面短暂重启反复杀进程），失败原因写入 ``upstream.error``；
- ``/metrics`` 只导出本进程注册表（uptime + 上游可达性），业务指标由
  Prometheus 直接抓管理面的 ``/metrics``。
"""

from __future__ import annotations

import time

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


def _fetch_upstream(adapter_url: str) -> tuple[dict[str, object] | None, str | None]:
    """拉取管理面 ``/health`` 快照。

    Args:
        adapter_url: 管理面基础地址（如 ``http://127.0.0.1:8090``）。

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
        resp = httpx.get(url, timeout=_UPSTREAM_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001  连接失败/超时统一为不可达
        return None, f"{type(exc).__name__}: {exc}"
    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid json: {exc}"
    return payload if isinstance(payload, dict) else None, None


def _health_body(settings: HunterSimSettings) -> dict[str, object]:
    """构造探针健康响应体（上游可达时透传关键字段）。"""
    adapter_url = settings.probe.adapter_url
    snapshot, error = _fetch_upstream(adapter_url)
    PROBE_UPSTREAM_REACHABLE.set(1 if snapshot is not None else 0)

    body: dict[str, object] = {
        "status": "ok" if snapshot is not None else "degraded",
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

    Args:
        settings: 全局配置（None 时从环境加载）。

    Returns:
        仅注册 ``/health`` 与 ``/metrics`` 的 FastAPI 应用。
    """
    resolved = settings or HunterSimSettings()
    app = FastAPI(
        title="HunterSim Probes",
        version=_resolve_app_version(),
        description="Read-only health and metrics endpoints for HunterSim.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
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

    logger.info(f"Probes app created (adapter_url={resolved.probe.adapter_url})")
    return app


__all__ = ["create_probes_app"]
