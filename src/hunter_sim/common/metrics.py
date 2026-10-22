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

指标清单（V2.7.0 P1 新增）：
- huntersim_kafka_consumer_rebuilds_total  Kafka 消费者自愈重建计数（P1-8）
- huntersim_cloud_dlq_published_total      坏消息进 DLQ 计数（P1-8）
- huntersim_vil_message_dropped_total      VIL 乱序/重复消息丢弃计数（P1-9）
- huntersim_vil_latency_seconds            VIL 端到端延迟直方图（P50/P95/P99，P1-9）
- huntersim_execution_lease_reclaims_total 僵尸租约强制回收计数（P1-7）
- huntersim_auth_token_events_total        Token 续期/吊销计数（P1-6）

指标清单（V2.8.0 P2 新增）：
- huntersim_user_events_total              平台用户管理/登录计数（P2-13）
- huntersim_scheduling_decisions_total     地图感知调度决策计数（P2-14）
- huntersim_carla_supervisor_events_total  托管 CARLA 进程事件计数（P2-15）
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

# ─── V2.7.0 P1：数据链路韧性与可观测 ────────────────────────────────────────

KAFKA_CONSUMER_REBUILDS_TOTAL = Counter(
    "huntersim_kafka_consumer_rebuilds_total",
    "VIL Kafka consumer self-heal rebuilds after consecutive poll errors.",
)

KAFKA_CONSUMED_TOTAL = Counter(
    "huntersim_kafka_consumed_total",
    "VIL telemetry messages consumed from Kafka (self-heal observability).",
)

# 消费滞后（最新 offset - 当前 position）估算：无 partition 分配时为 0
KAFKA_CONSUMER_LAG = Gauge(
    "huntersim_kafka_consumer_lag",
    "Approximate VIL Kafka consumer lag (end offset - position), best-effort.",
)

CLOUD_DLQ_PUBLISHED_TOTAL = Counter(
    "huntersim_cloud_dlq_published_total",
    "Malformed VIL telemetry messages forwarded to the dead-letter topic.",
)

VIL_MESSAGE_DROPPED_TOTAL = Counter(
    "huntersim_vil_message_dropped_total",
    "Out-of-order or duplicate VIL telemetry messages dropped by the ordering guard.",
    labelnames=("reason",),
)

# VIL 端到端延迟直方图（毫秒→秒观测）：Grafana 用 histogram_quantile 求 P50/P95/P99
VIL_LATENCY_SECONDS = Histogram(
    "huntersim_vil_latency_seconds",
    "VIL end-to-end telemetry latency in seconds (P50/P95/P99 via histogram_quantile).",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0),
)

EXECUTION_LEASE_RECLAIMS_TOTAL = Counter(
    "huntersim_execution_lease_reclaims_total",
    "Zombie engine leases force-reclaimed by admin (stale heartbeat).",
)

AUTH_TOKEN_EVENTS_TOTAL = Counter(
    "huntersim_auth_token_events_total",
    "JWT token lifecycle events (event=refresh|revoke|revoked_hit).",
    labelnames=("event",),
)

# ─── V2.8.0 P2：多用户 / 地图感知调度 / 引擎自愈 ────────────────────────────

USER_EVENTS_TOTAL = Counter(
    "huntersim_user_events_total",
    "Platform user management events (event=create|update|delete|password_change|"
    "password_change_denied|login|login_denied).",
    labelnames=("event",),
)

SCHEDULING_DECISIONS_TOTAL = Counter(
    "huntersim_scheduling_decisions_total",
    "Engine endpoint scheduling outcomes (kind=affinity_reuse|fresh_endpoint|fallback).",
    labelnames=("kind",),
)

CARLA_SUPERVISOR_EVENTS_TOTAL = Counter(
    "huntersim_carla_supervisor_events_total",
    "Managed CARLA process events (event=start|exit|restart|giveup|stop).",
    labelnames=("event",),
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


def observe_vil_latency(latency_ms: float) -> None:
    """记录一帧 VIL 端到端延迟样本（毫秒入参，按秒观测）。

    供 ``TelemetryBuffer`` / 健康监控在收帧时调用，直方图支持在 Grafana
    用 ``histogram_quantile`` 求 P50/P95/P99（P1-9）。
    """
    if latency_ms >= 0.0:
        VIL_LATENCY_SECONDS.observe(latency_ms / 1000.0)


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
    "AUTH_TOKEN_EVENTS_TOTAL",
    "CARLA_RPC_ERRORS_TOTAL",
    "CARLA_SUPERVISOR_EVENTS_TOTAL",
    "CLOUD_DLQ_PUBLISHED_TOTAL",
    "CLOUD_UPLOAD_DROPPED_TOTAL",
    "CLOUD_UPLOAD_TOTAL",
    "EXECUTION_LEASE_RECLAIMS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "INSTANCE_LIFECYCLE_TOTAL",
    "KAFKA_CONSUME_ERRORS_TOTAL",
    "KAFKA_CONSUMER_LAG",
    "KAFKA_CONSUMER_REBUILDS_TOTAL",
    "KAFKA_CONSUMED_TOTAL",
    "LOGIN_REJECTIONS_TOTAL",
    "RATE_LIMITED_REJECTIONS_TOTAL",
    "SCHEDULING_DECISIONS_TOTAL",
    "SERVICE_START_TIME",
    "USER_EVENTS_TOTAL",
    "VIL_LATENCY_SECONDS",
    "VIL_MESSAGE_DROPPED_TOTAL",
    "counter_value",
    "histogram_count",
    "metric_exists",
    "normalize_api_path",
    "observe_http_request",
    "observe_vil_latency",
    "update_app_state_gauges",
]
