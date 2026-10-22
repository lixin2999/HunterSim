"""Prometheus 指标统一定义（V2.6.0 P0-5）。

工程约定：
- 全部指标注册在 ``prometheus_client`` 默认 REGISTRY，``/metrics`` 端点经
  ``generate_latest()`` 一次性导出（``api/routers/health.py``）；
- 命名统一 ``huntersim_`` 前缀，与 ``docker/alerts.yml`` 告警规则保持兼容；
- 服务层/公共层只依赖本模块打点，不感知 API 层（避免反向依赖）；
- Gauge 值在抓取时刻从 ``app.state`` 同步（``update_app_state_gauges``），
  探测不可用时写入 NaN（NaN 不满足任何 ``>`` 阈值，不会误告警）。

指标清单（V2.6.0 新增）：
- huntersim_http_request_duration_seconds  HTTP 请求延迟直方图
- huntersim_rate_limited_rejections_total  限流拒绝计数（P0-3）
- huntersim_login_rejections_total         登录拒绝计数（凭据/退避，P0-3）
- huntersim_instance_lifecycle_total       实例生命周期计数
- huntersim_carla_rpc_errors_total         CARLA RPC 探测失败计数
- huntersim_kafka_consume_errors_total     Kafka 消费轮询错误计数
- huntersim_cloud_upload_total             bag 上传结果计数（success/failed）
- huntersim_cloud_upload_dropped_total     上传队列溢出丢弃计数（P0-4）
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

# 服务启动时间（uptime 指标基准；与 api/routers/health.py 原语义一致）
SERVICE_START_TIME: float = time.time()

_API_PREFIX = "/api/v1/sim"

# 路径段中视为资源 ID 的形态：长度 ≥16 且含分隔符（uuid/实例 ID/任务 ID 等）
_ID_SEGMENT_RE = re.compile(r"^[0-9A-Za-z_-]{16,}$")


def normalize_api_path(path: str) -> str:
    """把请求路径归一化为低基数指标标签。

    ``/api/v1/sim/instances/<uuid>/status`` → ``instances/{id}/status``，
    避免实例 ID/任务 ID 打爆 Prometheus 时间线。

    Args:
        path: 原始请求路径。

    Returns:
        去掉 API 前缀并替换 ID 段后的路径字符串。
    """
    stripped = path[len(_API_PREFIX):] if path.startswith(_API_PREFIX) else path
    stripped = stripped.strip("/")
    if not stripped:
        return "/"
    parts = stripped.split("/")
    normalized = [
        "{id}" if (len(part) >= 16 and "-" in part and _ID_SEGMENT_RE.match(part)) else part
        for part in parts
    ]
    return "/".join(normalized)


# ─── HTTP / 安全治理 ─────────────────────────────────────────────────────────

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "huntersim_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("method", "path", "status_class"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

RATE_LIMITED_REJECTIONS_TOTAL = Counter(
    "huntersim_rate_limited_rejections_total",
    "Requests rejected by the per-client rate limiter.",
)

LOGIN_REJECTIONS_TOTAL = Counter(
    "huntersim_login_rejections_total",
    "Token requests rejected (reason=credential|backoff).",
    labelnames=("reason",),
)

# ─── 业务链路 ────────────────────────────────────────────────────────────────

INSTANCE_LIFECYCLE_TOTAL = Counter(
    "huntersim_instance_lifecycle_total",
    "Simulation instance lifecycle events.",
    labelnames=("event",),
)

CARLA_RPC_ERRORS_TOTAL = Counter(
    "huntersim_carla_rpc_errors_total",
    "CARLA RPC connectivity probe failures.",
)

KAFKA_CONSUME_ERRORS_TOTAL = Counter(
    "huntersim_kafka_consume_errors_total",
    "Kafka consumer poll errors (VIL telemetry chain).",
)

CLOUD_UPLOAD_TOTAL = Counter(
    "huntersim_cloud_upload_total",
    "Bag upload outcomes on the cloud connector.",
    labelnames=("result",),
)

CLOUD_UPLOAD_DROPPED_TOTAL = Counter(
    "huntersim_cloud_upload_dropped_total",
    "Bag upload jobs dropped due to a full upload queue.",
)

# ─── app.state 派生 Gauge（抓取时刻刷新）────────────────────────────────────

_GAUGE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("active_instance_count", "huntersim_active_instances", "Number of active simulation instances."),
    ("carla_rpc_latency_seconds", "huntersim_carla_rpc_latency_seconds", "CARLA RPC response time."),
    ("simulation_fps", "huntersim_simulation_fps", "Simulation average frame rate."),
    ("vil_latency_ms", "huntersim_vil_latency_ms", "VIL end-to-end latency in ms."),
    ("disk_usage_ratio", "huntersim_disk_usage_ratio", "Disk usage ratio (0-1)."),
)

_UPTIME_GAUGE = Gauge("huntersim_uptime_seconds", "Service uptime in seconds.")
_CARLA_CONNECTED_GAUGE = Gauge(
    "huntersim_carla_connected", "Whether CARLA connection is up (1/0)."
)
# Gauge 名 → Gauge 对象（抓取时缺值写 NaN，避免旧值滞留导致误判）
_STATE_GAUGES: dict[str, Gauge] = {
    name: Gauge(name, help_text) for _, name, help_text in _GAUGE_SPECS
}
# state 属性名 → Gauge 名
_STATE_ATTR_TO_GAUGE: dict[str, str] = {state_key: name for state_key, name, _ in _GAUGE_SPECS}


def update_app_state_gauges(state: Any) -> None:
    """抓取时刻把 app.state 实时值刷入 Prometheus Gauge。

    Args:
        state: ``FastAPI.app.state``（或任意具备同名属性的对象）。
    """
    _UPTIME_GAUGE.set(time.time() - SERVICE_START_TIME)
    connected = getattr(state, "carla_connected", False)
    _CARLA_CONNECTED_GAUGE.set(1 if connected else 0)
    for attr, gauge_name in _STATE_ATTR_TO_GAUGE.items():
        value: Any = getattr(state, attr, None)
        gauge = _STATE_GAUGES[gauge_name]
        if value is None:
            gauge.set(math.nan)
        else:
            gauge.set(float(value))


def observe_http_request(method: str, path: str, status_code: int, elapsed_seconds: float) -> None:
    """记录一次 HTTP 请求的延迟直方图样本（供请求日志中间件调用）。

    Args:
        method: HTTP 方法。
        path: 原始请求路径。
        status_code: 响应状态码。
        elapsed_seconds: 处理耗时（秒）。
    """
    status_class = f"{status_code // 100}XX"
    HTTP_REQUEST_DURATION_SECONDS.labels(
        method=method, path=normalize_api_path(path), status_class=status_class
    ).observe(elapsed_seconds)


def counter_value(metric: Any, **labels: str) -> float:
    """读取 Counter 当前累计值（测试断言辅助；未初始化返回 0）。"""
    try:
        return float(metric.labels(**labels)._value.get()) if labels else float(metric._value.get())
    except (AttributeError, KeyError):
        return 0.0


def histogram_count(metric: Any, **labels: str) -> float:
    """读取 Histogram（或其 label 子样本）的累计观测数（测试断言辅助）。

    Histogram 样本木不含直接的 ``_count`` 属性，但 ``collect()`` 会输出
    ``<name>_count`` 样本；优先取之，无则回退累加各 bucket 计数。
    """
    try:
        if labels:
            child = metric.labels(**labels)
            return float(sum(b.get() for b in child._buckets))
        for collected in metric.collect():
            for sample in collected.samples:
                if sample.name.endswith("_count") and not sample.name.endswith("_created"):
                    return float(sample.value)
        return 0.0
    except (AttributeError, KeyError, TypeError):
        return 0.0


def metric_exists(name: str) -> bool:
    """默认 REGISTRY 中是否已注册指定指标（测试辅助）。

    通过公开 API ``collect()`` 判断：Counter ``x_total`` 的族名为 ``x``，
    因此同时接受带/不带 ``_total`` 后缀的查询名。
    """
    candidates = {name, name.removesuffix("_total")}
    for collected in REGISTRY.collect():
        if collected.name in candidates:
            return True
    return False


__all__ = [
    "CARLA_RPC_ERRORS_TOTAL",
    "CLOUD_UPLOAD_DROPPED_TOTAL",
    "CLOUD_UPLOAD_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "INSTANCE_LIFECYCLE_TOTAL",
    "KAFKA_CONSUME_ERRORS_TOTAL",
    "LOGIN_REJECTIONS_TOTAL",
    "RATE_LIMITED_REJECTIONS_TOTAL",
    "SERVICE_START_TIME",
    "counter_value",
    "histogram_count",
    "metric_exists",
    "normalize_api_path",
    "observe_http_request",
    "update_app_state_gauges",
]
