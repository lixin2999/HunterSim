"""Prometheus 指标层单测（V2.6.0 P0-5）。

覆盖路径归一化、HTTP 直方图打点、app.state Gauge 刷新（含 NaN 缺值语义）、
测试辅助函数，以及 /metrics 端点输出（经共享 module 级 app/client 验证）。
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

from prometheus_client import REGISTRY

from hunter_sim.common.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    KAFKA_CONSUME_ERRORS_TOTAL,
    RATE_LIMITED_REJECTIONS_TOTAL,
    counter_value,
    histogram_count,
    metric_exists,
    normalize_api_path,
    observe_http_request,
    update_app_state_gauges,
)

# ─── normalize_api_path ──────────────────────────────────────────────────────


class TestNormalizeApiPath:
    def test_strips_prefix_and_replaces_id_segments(self) -> None:
        path = normalize_api_path(
            "/api/v1/sim/instances/3f2a1b9c-4d5e-6f70-8a9b-c0d1e2f3a4b5/status"
        )
        assert path == "instances/{id}/status"

    def test_short_or_plain_segments_kept(self) -> None:
        assert normalize_api_path("/api/v1/sim/scenarios/list") == "scenarios/list"

    def test_long_segment_without_dash_kept(self) -> None:
        assert normalize_api_path("/api/v1/sim/jobs/abcdefghijklmnopq") == "jobs/abcdefghijklmnopq"

    def test_no_prefix_paths_passthrough(self) -> None:
        assert normalize_api_path("/healthz") == "healthz"

    def test_root_paths(self) -> None:
        assert normalize_api_path("/api/v1/sim") == "/"
        assert normalize_api_path("/") == "/"


# ─── 打点与 Gauge 刷新 ───────────────────────────────────────────────────────


def _sample_values(prefix: str = "huntersim_") -> dict[str, float]:
    """抓取默认 REGISTRY 中 huntersim_* 指标族的首样本值。"""
    values: dict[str, float] = {}
    for collector in REGISTRY.collect():
        for sample in collector.samples:
            if sample.name.startswith(prefix) and sample.name not in values:
                values[sample.name] = sample.value
    return values


class TestObservability:
    def test_observe_http_request_feeds_normalized_histogram(self) -> None:
        observe_http_request("GET", "/api/v1/sim/instances/a-probe-id-0123456789ab", 204, 0.05)
        assert (
            histogram_count(
                HTTP_REQUEST_DURATION_SECONDS,
                method="GET",
                path="instances/{id}",
                status_class="2XX",
            )
            >= 1
        )

    def test_status_class_buckets(self) -> None:
        observe_http_request("POST", "/api/v1/sim/probe-429", 429, 0.001)
        observe_http_request("POST", "/api/v1/sim/probe-503", 503, 0.001)
        assert (
            histogram_count(
                HTTP_REQUEST_DURATION_SECONDS, method="POST", path="probe-429", status_class="4XX"
            )
            >= 1
        )
        assert (
            histogram_count(
                HTTP_REQUEST_DURATION_SECONDS, method="POST", path="probe-503", status_class="5XX"
            )
            >= 1
        )

    def test_update_app_state_gauges_refreshes_values(self) -> None:
        state = SimpleNamespace(
            carla_connected=True,
            active_instance_count=3,
            carla_rpc_latency_seconds=0.012,
            simulation_fps=30.0,
            vil_latency_ms=42.0,
            disk_usage_ratio=0.5,
        )
        update_app_state_gauges(state)
        values = _sample_values()
        assert values["huntersim_carla_connected"] == 1
        assert values["huntersim_active_instances"] == 3
        assert values["huntersim_disk_usage_ratio"] == 0.5

    def test_missing_state_values_become_nan(self) -> None:
        update_app_state_gauges(SimpleNamespace())
        values = _sample_values()
        assert math.isnan(values["huntersim_simulation_fps"])
        assert values["huntersim_carla_connected"] == 0

    def test_counter_and_metric_helpers(self) -> None:
        RATE_LIMITED_REJECTIONS_TOTAL.inc()
        assert counter_value(RATE_LIMITED_REJECTIONS_TOTAL) >= 1
        assert counter_value(KAFKA_CONSUME_ERRORS_TOTAL) >= 0
        assert metric_exists("huntersim_cloud_upload_total") is True
        assert metric_exists("huntersim_kafka_consume_errors") is True
        assert metric_exists("huntersim_no_such_metric") is False


class TestMetricsEndpoint:
    def test_metrics_expose_new_families(self, client: Any) -> None:
        resp = client.get("/api/v1/sim/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        for family in (
            "huntersim_http_request_duration_seconds",
            "huntersim_rate_limited_rejections_total",
            "huntersim_login_rejections_total",
            "huntersim_instance_lifecycle_total",
            "huntersim_carla_rpc_errors_total",
            "huntersim_kafka_consume_errors_total",
            "huntersim_cloud_upload_total",
            "huntersim_cloud_upload_dropped_total",
            "huntersim_uptime_seconds",
            "huntersim_carla_connected",
        ):
            assert family in body, f"missing metric family: {family}"

    def test_metrics_use_standard_exporter(self, client: Any) -> None:
        body = client.get("/api/v1/sim/metrics").text
        assert "python_info" in body or "process_" in body


# ─── V2.10.0 平台接入新指标 ──────────────────────────────────────────────────


class TestPlatformIntegrationMetrics:
    """验证 V2.10.0 新注册的 8 个指标存在且可正常打点。"""

    def test_sim_status_published_total(self) -> None:
        from hunter_sim.common.metrics import SIM_STATUS_PUBLISHED_TOTAL
        assert metric_exists("huntersim_sim_status_published_total")
        before = counter_value(SIM_STATUS_PUBLISHED_TOTAL)
        SIM_STATUS_PUBLISHED_TOTAL.inc()
        assert counter_value(SIM_STATUS_PUBLISHED_TOTAL) == before + 1

    def test_sim_event_published_total(self) -> None:
        from hunter_sim.common.metrics import SIM_EVENT_PUBLISHED_TOTAL
        assert metric_exists("huntersim_sim_event_published_total")
        before = counter_value(SIM_EVENT_PUBLISHED_TOTAL)
        SIM_EVENT_PUBLISHED_TOTAL.inc()
        assert counter_value(SIM_EVENT_PUBLISHED_TOTAL) == before + 1

    def test_sim_metrics_published_total(self) -> None:
        from hunter_sim.common.metrics import SIM_METRICS_PUBLISHED_TOTAL
        assert metric_exists("huntersim_sim_metrics_published_total")
        before = counter_value(SIM_METRICS_PUBLISHED_TOTAL)
        SIM_METRICS_PUBLISHED_TOTAL.inc()
        assert counter_value(SIM_METRICS_PUBLISHED_TOTAL) == before + 1

    def test_sensor_file_published_total(self) -> None:
        from hunter_sim.common.metrics import SENSOR_FILE_PUBLISHED_TOTAL
        assert metric_exists("huntersim_sensor_file_published_total")
        before = counter_value(SENSOR_FILE_PUBLISHED_TOTAL)
        SENSOR_FILE_PUBLISHED_TOTAL.inc()
        assert counter_value(SENSOR_FILE_PUBLISHED_TOTAL) == before + 1

    def test_vil_state_consumed_total(self) -> None:
        from hunter_sim.common.metrics import VIL_STATE_CONSUMED_TOTAL
        assert metric_exists("huntersim_vil_state_consumed_total")
        before = counter_value(VIL_STATE_CONSUMED_TOTAL)
        VIL_STATE_CONSUMED_TOTAL.inc()
        assert counter_value(VIL_STATE_CONSUMED_TOTAL) == before + 1

    def test_sim_data_producer_errors_total_labels(self) -> None:
        from hunter_sim.common.metrics import SIM_DATA_PRODUCER_ERRORS_TOTAL
        assert metric_exists("huntersim_sim_data_producer_errors_total")
        # 带标签
        SIM_DATA_PRODUCER_ERRORS_TOTAL.labels(sim_id="sim-test", op="send").inc()

    def test_sim_data_buffer_size_labeled(self) -> None:
        from hunter_sim.common.metrics import SIM_DATA_BUFFER_SIZE
        assert metric_exists("huntersim_sim_data_buffer_size")
        SIM_DATA_BUFFER_SIZE.labels(sim_id="sim-test").set(5)

    def test_vil_e2e_lag_ms_histogram(self) -> None:
        from hunter_sim.common.metrics import VIL_E2E_LAG_MS, observe_vil_e2e_lag
        assert metric_exists("huntersim_vil_e2e_lag_ms")
        before = histogram_count(VIL_E2E_LAG_MS)
        observe_vil_e2e_lag(15.0)
        assert histogram_count(VIL_E2E_LAG_MS) >= before + 1

    def test_observe_vil_e2e_lag_ignores_negative(self) -> None:
        from hunter_sim.common.metrics import VIL_E2E_LAG_MS, observe_vil_e2e_lag
        before = histogram_count(VIL_E2E_LAG_MS)
        observe_vil_e2e_lag(-10.0)  # 负值不记录
        assert histogram_count(VIL_E2E_LAG_MS) == before
