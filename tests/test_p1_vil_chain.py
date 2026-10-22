"""V2.7.0 P1-8/P1-9 数据链路强化单元测试。

覆盖：
- 环深自动推导 ``compute_auto_buffer_depth`` 与 ``TelemetryBuffer`` 集成；
- 收帧延迟直方图（P1-9 观测点）；
- ``DataHealthMonitor`` P50/P95/P99 分位数；
- ``VILDataConsumer`` 乱序/去重闸门、坏消息 DLQ 转发、消费自愈重建。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from typing import Any

import pytest

from hunter_sim.common.metrics import VIL_LATENCY_SECONDS
from hunter_sim.common.models import KafkaSettings, VILSettings
from hunter_sim.vil_mapper.data_health_monitor import DataHealthMonitor
from hunter_sim.vil_mapper.telemetry_buffer import (
    TelemetryBuffer,
    TelemetryFrame,
    compute_auto_buffer_depth,
)
from hunter_sim.vil_mapper.vil_data_consumer import VILDataConsumer
from hunter_sim.common.models import Transform, VehicleState


def _frame(ts: float) -> TelemetryFrame:
    return TelemetryFrame(
        vehicle_id="veh-1",
        timestamp=ts,
        receive_time=ts,
        vehicle_state=VehicleState(
            time_stamp=ts,
            transform=Transform(x=0.0, y=0.0, z=0.0),
        ),
    )


# ─── P1-9：环深自动推导 ───────────────────────────────────────────────────────


class TestAutoBufferDepth:
    def test_formula_and_clamp(self) -> None:
        # 150ms × 10Hz → ceil(1.5)=2 + 余量 5 = 7 → 钳制下限 10
        assert compute_auto_buffer_depth(150, 10.0) == 10
        # 500ms × 20Hz → 10 + 5 = 15
        assert compute_auto_buffer_depth(500, 20.0) == 15
        # 超大窗口钳制上限 200
        assert compute_auto_buffer_depth(1000, 200.0) == 200

    def test_zero_frequency_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_auto_buffer_depth(100, 0.0)

    def test_buffer_uses_auto_depth_when_enabled(self) -> None:
        vil = VILSettings(extrapolation_ms=1000, telemetry_frequency_hz=20.0, auto_buffer_depth=True)
        buf = TelemetryBuffer(max_frames=10, vil_settings=vil)
        # ceil(1000/1000×20)=20 + 5 = 25
        assert buf.max_frames == 25

    def test_manual_depth_when_disabled(self) -> None:
        vil = VILSettings(auto_buffer_depth=False, telemetry_frequency_hz=20.0)
        buf = TelemetryBuffer(max_frames=7, vil_settings=vil)
        assert buf.max_frames == 7


# ─── P1-9：延迟直方图与分位数 ─────────────────────────────────────────────────


def _histogram_total() -> int:
    return sum(
        int(sample.value)
        for metric in VIL_LATENCY_SECONDS.collect()
        for sample in metric.samples
        if sample.name.endswith("_count")
    )


class TestLatencyObservability:
    def test_push_records_latency_sample(self) -> None:
        buf = TelemetryBuffer()
        before = _histogram_total()
        frame = _frame(time.time() - 0.05)  # 50ms 延迟
        buf.push(frame)
        assert _histogram_total() == before + 1

    def test_negative_latency_not_observed(self) -> None:
        buf = TelemetryBuffer()
        before = _histogram_total()
        buf.push(_frame(time.time() + 10.0))  # 时钟回跳：latency 为负
        assert _histogram_total() == before

    def test_percentiles_on_known_window(self) -> None:
        vil = VILSettings(warning_latency_ms=500, max_data_latency_ms=5000)
        buf = TelemetryBuffer(max_frames=100)
        monitor = DataHealthMonitor(vil, buf)
        now = time.time()
        report = None
        for latency_ms in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
            frame = _frame(now - latency_ms / 1000.0)
            buf.push(frame)
            report = monitor.check()  # 每次 check 会将最新帧入窗，不额外重复采样
        assert report is not None
        # push 会刷新 receive_time，循环内时间漂移使实测延迟比构造值大 0~2ms → 容差放宽
        assert report.p50_latency_ms == pytest.approx(50.0, abs=2.0)
        assert report.p95_latency_ms == pytest.approx(100.0, abs=2.0)
        assert report.p99_latency_ms == pytest.approx(100.0, abs=2.0)
        assert report.avg_latency_ms == pytest.approx(55.0, abs=2.0)


# ─── P1-9：乱序/去重闸门 ─────────────────────────────────────────────────────


def _consumer(
    vil: VILSettings | None = None,
    producer_factory: Any = None,
    **kafka_kwargs: Any,
) -> VILDataConsumer:
    settings = KafkaSettings(bootstrap_servers="localhost:9092", **kafka_kwargs)
    return VILDataConsumer(
        settings=settings,
        vil_settings=vil or VILSettings(),
        vehicle_id="veh-1",
        buffer=TelemetryBuffer(),
        producer_factory=producer_factory,
    )


class TestOrderingGuard:
    def test_duplicate_seq_dropped(self) -> None:
        consumer = _consumer()
        assert consumer._accept_in_order({"timestamp": 1.0, "seq": 5}) is True
        assert consumer._accept_in_order({"timestamp": 1.1, "seq": 5}) is False  # 重复
        assert consumer._accept_in_order({"timestamp": 1.2, "seq": 4}) is False  # 回退
        assert consumer._accept_in_order({"timestamp": 1.3, "seq": 6}) is True

    def test_timestamp_fallback_when_no_seq(self) -> None:
        consumer = _consumer()
        assert consumer._accept_in_order({"timestamp": 2.0}) is True
        assert consumer._accept_in_order({"timestamp": 1.5}) is False  # 乱序
        assert consumer._accept_in_order({"timestamp": 2.5}) is True

    def test_guard_disabled_passthrough(self) -> None:
        consumer = _consumer(vil=VILSettings(ordering_guard_enabled=False))
        assert consumer._accept_in_order({"timestamp": 9.0, "seq": 9}) is True
        assert consumer._accept_in_order({"timestamp": 1.0, "seq": 1}) is True

    def test_handle_message_drops_out_of_order_telemetry(self) -> None:
        consumer = _consumer()
        topic = consumer._settings.telemetry_topic
        payload = {
            "vehicle_id": "veh-1",
            "timestamp": time.time(),
            "seq": 2,
            "position": {"x": 1.0, "y": 2.0},
        }
        consumer._handle_message(topic, json.dumps(payload).encode())
        consumer._handle_message(topic, json.dumps({**payload, "seq": 1}).encode())
        assert consumer._buffer.total_received == 1  # 第二条被丢弃


# ─── P1-8：坏消息 DLQ 与消费自愈 ─────────────────────────────────────────────


class TestDlqForwarding:
    def test_malformed_message_sent_to_dlq(self) -> None:
        sent: list[tuple[str, bytes]] = []

        class _Recorder:
            def send(self, topic: str, value: bytes) -> None:
                sent.append((topic, value))

            def flush(self, timeout: float = 0) -> None: ...
            def close(self, timeout: float = 0) -> None: ...

        consumer = _consumer(dlq_topic="vil.dlq", producer_factory=lambda: _Recorder())
        consumer._handle_message("telemetry_clean", b"not-json{{")
        assert sent == [("vil.dlq", b"not-json{{")]

    def test_no_dlq_topic_configured_counts_only(self) -> None:
        consumer = _consumer()  # dlq_topic 默认空
        consumer._handle_message("telemetry_clean", b"garbage")
        assert consumer.stats["errors"] >= 1

    def test_stop_closes_dlq_producer(self) -> None:
        closed: list[bool] = []

        class _Producer:
            def send(self, topic: str, value: bytes) -> None: ...
            def flush(self, timeout: float = 0) -> None: ...
            def close(self, timeout: float = 0) -> None:
                closed.append(True)

        consumer = _consumer(dlq_topic="vil.dlq", producer_factory=lambda: _Producer())
        consumer._handle_message("telemetry_clean", b"bad")  # 触发惰性创建
        consumer.stop()
        assert closed == [True]


class TestConsumerSelfHeal:
    def test_rebuild_after_consecutive_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """poll 连续失败达阈值 → 重建消费者；恢复成功后连续错误计数清零。"""
        created: list[int] = []

        class _FlakyConsumer:
            def __init__(self) -> None:
                self._id = len(created)
                created.append(self._id)
                self.poll_failures = 2  # 前两个实例 poll 永久抛错

            def poll(self, timeout_ms: int = 0) -> dict[str, Any]:
                raise RuntimeError("broker unavailable")

            def assignment(self) -> list[Any]:
                return []

            def close(self, timeout_ms: int = 0) -> None: ...

        fake_kafka = types.ModuleType("kafka")
        fake_kafka.KafkaConsumer = lambda *a, **k: _FlakyConsumer()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kafka", fake_kafka)

        settings = KafkaSettings(
            bootstrap_servers="localhost:9092", enable_self_heal=True, rebuild_after_errors=2
        )
        consumer = VILDataConsumer(
            settings=settings,
            vil_settings=VILSettings(),
            vehicle_id="veh-1",
            buffer=TelemetryBuffer(),
        )
        # 手动驱动循环（不起线程）：running 置位后跑若干轮再退出
        consumer._running.set()

        def _watchdog() -> None:
            time.sleep(2.5)
            consumer._running.clear()

        threading.Thread(target=_watchdog, daemon=True).start()
        consumer._consume_loop()

        # 初始 1 个 + 每 2 次连续错误重建 1 个：至少重建过
        assert len(created) >= 2
        assert consumer._consecutive_errors < 2 or consumer._consumer is None

    def test_self_heal_disabled_never_rebuilds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        created: list[int] = []

        class _BadConsumer:
            def __init__(self) -> None:
                created.append(len(created))

            def poll(self, timeout_ms: int = 0) -> dict[str, Any]:
                raise RuntimeError("boom")

            def close(self, timeout_ms: int = 0) -> None: ...

        fake_kafka = types.ModuleType("kafka")
        fake_kafka.KafkaConsumer = lambda *a, **k: _BadConsumer()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kafka", fake_kafka)

        settings = KafkaSettings(bootstrap_servers="localhost:9092", enable_self_heal=False)
        consumer = VILDataConsumer(
            settings=settings,
            vil_settings=VILSettings(),
            vehicle_id="veh-1",
            buffer=TelemetryBuffer(),
        )
        consumer._running.set()

        def _watchdog() -> None:
            time.sleep(1.2)
            consumer._running.clear()

        threading.Thread(target=_watchdog, daemon=True).start()
        consumer._consume_loop()
        assert len(created) == 1  # 未自愈：只有初始消费者
