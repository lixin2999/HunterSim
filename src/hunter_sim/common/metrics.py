"""Prometheus 指标统一定义（V3.0.0 收敛）。

工程约定：
- 全部指标注册在 ``prometheus_client`` 默认 REGISTRY；进程内 ``/metrics`` 端点经
  ``generate_latest()`` 一次性导出（业务面 :8090 与只读探针 :8080 各持一份注册表）；
- 命名统一 ``huntersim_`` 前缀，与 ``docker/alerts.yml`` 告警规则保持兼容；
- 服务层/公共层只依赖本模块打点，不感知 API 层（避免反向依赖）；
- Gauge 值在抓取时刻由 ``refresh_runtime_gauges`` 刷新；无采样源的指标保持初始
  未设置状态（不产出样本，``absent`` 类告警可据 ``absent()`` 判定）。

指标清单（仿真生命周期与云端回传）：
- huntersim_http_request_duration_seconds  管理面 HTTP 请求延迟直方图
- huntersim_instance_lifecycle_total       实例生命周期计数（created/canceled）
- huntersim_carla_rpc_errors_total         CARLA RPC 探测失败计数
- huntersim_kafka_consume_errors_total     Kafka 消费轮询错误计数
- huntersim_kafka_consumer_rebuilds_total  Kafka 消费者自愈重建计数
- huntersim_kafka_consumed_total           VIL 遥测消息消费计数
- huntersim_kafka_consumer_lag             消费滞后估算 Gauge
- huntersim_cloud_dlq_published_total      坏消息进 DLQ 计数
- huntersim_cloud_upload_total             bag/mcap 上传结果计数（success/failed）
- huntersim_cloud_upload_dropped_total     上传队列溢出丢弃计数
- huntersim_carla_supervisor_events_total  托管 CARLA 进程事件计数

指标清单（VIL 链路）：
- huntersim_vil_message_dropped_total      VIL 乱序/重复消息丢弃计数
- huntersim_vil_latency_seconds            VIL 端到端延迟直方图（P50/P95/P99）
- huntersim_vil_state_consumed_total       carla.{sim_id}.vil_state 消费计数
- huntersim_vil_e2e_lag_ms                 VIL 端到端延迟（ms）Histogram
- huntersim_vil_frames_downsampled_total   VIL 栅格推进计数（mode 见下）
- huntersim_vil_align_skew_ms              VIL 栅格对齐误差（ms）Histogram

指标清单（平台接入 6 类消息）：
- huntersim_sim_status_published_total     sim_status 消息发布计数
- huntersim_sim_event_published_total      sim_event 消息发布计数
- huntersim_sim_metrics_published_total    sim_metrics 消息发布计数
- huntersim_sensor_file_published_total    sensor_file 消息发布计数
- huntersim_sim_data_producer_errors_total SimDataProducer 发送/DLQ 错误计数
- huntersim_sim_data_buffer_size           SimDataProducer 有界缓冲当前深度 Gauge
- huntersim_sim_data_dropped_total         有界缓冲溢出丢弃计数

指标清单（传感器模拟与画面回传，V3.0.0 新增）：
- huntersim_sensor_frames_total            传感器帧计数（stream=rgb|depth|lidar|imu）
- huntersim_sensor_record_bytes_total      mcap 写入累计字节数
- huntersim_stream_clients                 MJPEG 在线客户端数 Gauge
"""

from __future__ import annotations

import re
import time
from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

# 服务启动时间（uptime 指标基准）
SERVICE_START_TIME: float = time.time()

# 管理面路径前缀（:8090 的 /api/v1/instances/...）
_API_PREFIX = "/api/v1"

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
    "VIL telemetry messages dropped (reason: out_of_order / duplicate_seq / sim_id_mismatch).",
    labelnames=("reason",),
)

# VIL 端到端延迟直方图（毫秒→秒观测）：Grafana 用 histogram_quantile 求 P50/P95/P99
VIL_LATENCY_SECONDS = Histogram(
    "huntersim_vil_latency_seconds",
    "VIL end-to-end telemetry latency in seconds (P50/P95/P99 via histogram_quantile).",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0),
)

CARLA_SUPERVISOR_EVENTS_TOTAL = Counter(
    "huntersim_carla_supervisor_events_total",
    "Managed CARLA process events (event=start|exit|restart|giveup|stop).",
    labelnames=("event",),
)

# ─── V2.10.0 平台接入：SimDataProducer / VIL 数据链路 ───────────────────

SIM_STATUS_PUBLISHED_TOTAL = Counter(
    "huntersim_sim_status_published_total",
    "sim_status messages published to sensor_data topic.",
)

SIM_EVENT_PUBLISHED_TOTAL = Counter(
    "huntersim_sim_event_published_total",
    "sim_event messages published to sensor_data topic.",
)

SIM_METRICS_PUBLISHED_TOTAL = Counter(
    "huntersim_sim_metrics_published_total",
    "sim_metrics messages published to sensor_data topic.",
)

SENSOR_FILE_PUBLISHED_TOTAL = Counter(
    "huntersim_sensor_file_published_total",
    "sensor_file messages published after successful bag upload.",
)

SIM_DATA_PRODUCER_ERRORS_TOTAL = Counter(
    "huntersim_sim_data_producer_errors_total",
    "SimDataProducer send/DLQ/serialization errors.",
    labelnames=("sim_id", "op"),
)

SIM_DATA_BUFFER_SIZE = Gauge(
    "huntersim_sim_data_buffer_size",
    "Current depth of the SimDataProducer bounded in-memory buffer.",
    labelnames=("sim_id",),
)

SIM_DATA_DROPPED_TOTAL = Counter(
    "huntersim_sim_data_dropped_total",
    "Messages discarded by SimDataProducer (reason=buffer_overflow).",
    labelnames=("sim_id", "reason"),
)

VIL_STATE_CONSUMED_TOTAL = Counter(
    "huntersim_vil_state_consumed_total",
    "carla.{sim_id}.vil_state messages consumed by VILDataConsumer.",
)

VIL_E2E_LAG_MS = Histogram(
    "huntersim_vil_e2e_lag_ms",
    "VIL end-to-end message lag in milliseconds (publisher ts -> consumer ts).",
    buckets=(1, 5, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000),
)

# ─── V3.0.0：VIL 栅格对齐与传感器回传 ──────────────────────────────────────

# 10Hz 栅格推进计数：mode=interpolated|extrapolated|nearest，
# 三者之和即实际推进的栅格数（50Hz 输入下应稳定在 10/s）。
VIL_FRAMES_DOWNSAMPLED_TOTAL = Counter(
    "huntersim_vil_frames_downsampled_total",
    "VIL frames applied per alignment mode (interpolated/extrapolated/nearest).",
    labelnames=("mode",),
)

# 对齐误差（毫秒）：nearest 模式下的时间偏差，P95 用于判断时钟/插值质量
VIL_ALIGN_SKEW_MS = Histogram(
    "huntersim_vil_align_skew_ms",
    "VIL frame alignment skew in milliseconds (grid time vs data time).",
    buckets=(5, 10, 20, 30, 50, 60, 100, 150, 300, 500, 1000),
)

# 传感器帧计数：stream=rgb|depth|lidar|imu
SENSOR_FRAMES_TOTAL = Counter(
    "huntersim_sensor_frames_total",
    "Sensor frames produced by the emulator (stream=rgb|depth|lidar|imu).",
    labelnames=("stream",),
)

# 传感器帧丢弃计数：编码队列满时丢帧而不是阻塞 CARLA tick
SENSOR_FRAMES_DROPPED_TOTAL = Counter(
    "huntersim_sensor_frames_dropped_total",
    "Sensor frames dropped because the encoder queue was full (stream=rgb|depth|lidar|imu).",
    labelnames=("stream",),
)

# 录制累计字节数（mcap 写入量，用于磁盘容量告警）
SENSOR_RECORD_BYTES_TOTAL = Counter(
    "huntersim_sensor_record_bytes_total",
    "Bytes written by the mcap recorder.",
)

# MJPEG 在线客户端数（Gauge），用于带宽与连接泄漏观测
STREAM_CLIENTS = Gauge(
    "huntersim_stream_clients",
    "Active MJPEG stream clients.",
)

# ─── 抓取时刻刷新的运行时 Gauge ──────────────────────────────────────────

# (参数名, 指标名, 说明)：由 ``refresh_runtime_gauges`` 在 ``/metrics`` 抓取时刷新
_GAUGE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("active_instances", "huntersim_active_instances", "Number of active simulation instances."),
    (
        "carla_rpc_latency_seconds",
        "huntersim_carla_rpc_latency_seconds",
        "CARLA RPC response time.",
    ),
    ("simulation_fps", "huntersim_simulation_fps", "Simulation average frame rate."),
    ("vil_latency_ms", "huntersim_vil_latency_ms", "VIL end-to-end latency in ms."),
    ("disk_usage_ratio", "huntersim_disk_usage_ratio", "Disk usage ratio (0-1)."),
)

_RUNTIME_GAUGES: dict[str, Gauge] = {
    key: Gauge(name, help_text) for key, name, help_text in _GAUGE_SPECS
}

_UPTIME_GAUGE = Gauge("huntersim_uptime_seconds", "Service uptime in seconds.")
_CARLA_CONNECTED_GAUGE = Gauge(
    "huntersim_carla_connected", "Whether CARLA connection is up (1/0)."
)


def refresh_runtime_gauges(**values: float | bool | None) -> None:
    """抓取时刻刷新运行时 Gauge（V3.0.0：不再依赖 ``app.state``）。

    只写入本次采样到值的指标；传 ``None`` 或未知参数名的项保持上一次值不变
    （进程启动后从未采样的指标不产出样本，可用 ``absent()`` 写告警）。

    Args:
        **values: 指标参数名 → 采样值，支持 ``active_instances`` /
            ``carla_rpc_latency_seconds`` / ``simulation_fps`` /
            ``vil_latency_ms`` / ``disk_usage_ratio`` / ``carla_connected``。
    """
    _UPTIME_GAUGE.set(time.time() - SERVICE_START_TIME)
    for key, value in values.items():
        if value is None:
            continue
        if key == "carla_connected":
            _CARLA_CONNECTED_GAUGE.set(1 if value else 0)
            continue
        gauge = _RUNTIME_GAUGES.get(key)
        if gauge is not None:
            gauge.set(float(value))


def observe_http_request(method: str, path: str, status_code: int, elapsed_seconds: float) -> None:
    """记录一次管理面 HTTP 请求的延迟直方图样本（供请求中间件调用）。

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


def observe_vil_alignment(mode: str, skew_ms: float) -> None:
    """记录一次 VIL 栅格对齐结果（V3.0.0）。

    Args:
        mode: 对齐模式（``interpolated`` / ``extrapolated`` / ``nearest``）。
        skew_ms: 栅格时间与实际数据时间的偏差（毫秒）。
    """
    VIL_FRAMES_DOWNSAMPLED_TOTAL.labels(mode=mode).inc()
    if skew_ms >= 0.0:
        VIL_ALIGN_SKEW_MS.observe(skew_ms)


def observe_vil_e2e_lag(lag_ms: float) -> None:
    """记录 VIL 端到端消息延迟（毫秒直方图，V2.10.0 平台接入链路）。

    Args:
        lag_ms: 发布端 timestamp 到消费端本地时间的差值（毫秒）。
    """
    if lag_ms >= 0.0:
        VIL_E2E_LAG_MS.observe(lag_ms)


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
    "CARLA_SUPERVISOR_EVENTS_TOTAL",
    "CLOUD_DLQ_PUBLISHED_TOTAL",
    "CLOUD_UPLOAD_DROPPED_TOTAL",
    "CLOUD_UPLOAD_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "INSTANCE_LIFECYCLE_TOTAL",
    "KAFKA_CONSUME_ERRORS_TOTAL",
    "KAFKA_CONSUMER_LAG",
    "KAFKA_CONSUMER_REBUILDS_TOTAL",
    "KAFKA_CONSUMED_TOTAL",
    "SENSOR_FRAMES_DROPPED_TOTAL",
    "SENSOR_FRAMES_TOTAL",
    "SENSOR_FILE_PUBLISHED_TOTAL",
    "SENSOR_RECORD_BYTES_TOTAL",
    "SERVICE_START_TIME",
    "SIM_DATA_BUFFER_SIZE",
    "SIM_DATA_DROPPED_TOTAL",
    "SIM_DATA_PRODUCER_ERRORS_TOTAL",
    "SIM_EVENT_PUBLISHED_TOTAL",
    "SIM_METRICS_PUBLISHED_TOTAL",
    "SIM_STATUS_PUBLISHED_TOTAL",
    "STREAM_CLIENTS",
    "VIL_ALIGN_SKEW_MS",
    "VIL_E2E_LAG_MS",
    "VIL_FRAMES_DOWNSAMPLED_TOTAL",
    "VIL_LATENCY_SECONDS",
    "VIL_MESSAGE_DROPPED_TOTAL",
    "VIL_STATE_CONSUMED_TOTAL",
    "counter_value",
    "histogram_count",
    "metric_exists",
    "normalize_api_path",
    "observe_http_request",
    "observe_vil_alignment",
    "observe_vil_e2e_lag",
    "observe_vil_latency",
    "refresh_runtime_gauges",
]
