"""V3.9.0 真 SIL 链路可观测与三方互联专项单测（B4）。

覆盖：
- 限频浮点容差：epoch 量级（~1.76e9）下按标称周期馈送不得误丢（旧判据会让
  10Hz 静默退化成 ~5Hz，正是面板「馈送速率」失真的根因）；
- 速率滑窗：与 FrameHub.fps 同口径 (n-1)/span，但窗口右端锚 now，断流衰减到 0；
- EdgeLinkRuntime.stats / summary()：馈送速率、控制回传频率、链路年龄、看门狗态、
  标称 fps 表；平台契约只拿精简段，完整诊断走 viz；
- 看门狗可观测：断流 → watchdog_tripped + PAUSED 原因，恢复后清零；
- payload 增段：sim_status 的 edge_link（未装配为 null，与既有 message 同风格）、
  sim_telemetry 未装配时整段省略；
- runner `_edge_link_summary()` 降级：未装配/异常/非 dict 均返回 None，不阻断上报；
- AdapterService.edge_link() 与 runtime_status()：透传、异常降级、无活动实例兜底；
- viz `/viz/api/status` 骨架含 edge_link（服务替身缺字段时前端仍有确定形状）。

用桩件隔离 Kafka/CARLA，时钟由 _Clock 完全掌控，不启动消费线程。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest
from fastapi.testclient import TestClient

from hunter_sim.cloud import edge_link as edge_link_module
from hunter_sim.cloud.edge_link import (
    _FPS_WINDOW_S,
    EdgeLinkRuntime,
    _rate_hz,
    _rate_limited,
)
from hunter_sim.cloud.sim_data_mapper import (
    SimStatusPayload,
    build_sim_status,
    build_sim_telemetry,
)
from hunter_sim.common.exceptions import KafkaConnectionError
from hunter_sim.common.models import EdgeLinkSettings, HunterSimSettings, KafkaSettings
from hunter_sim.platform_adapter.app import create_app
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.models import InstanceRecord
from hunter_sim.platform_adapter.service import AdapterService
from hunter_sim.platform_adapter.store import InstanceStore
from hunter_sim.scene_runner.scene_runner_service import SceneRunnerService

#: epoch 量级基准时刻：float64 在此量级的表示误差约 2.4e-7 s，足以让
#: 「恰好一个标称周期」的差值算出来略小于周期，从而触发旧判据误丢。
_EPOCH_BASE = 1_760_000_000.0

_SUMMARY_KEYS = {
    "enabled",
    "control_source",
    "session_booted",
    "waiting_first_control",
    "watchdog_tripped",
    "watchdog_ms",
    "control_age_ms",
    "control_hz",
    "sensor_hz",
    "send_errors",
    "uptime_s",
}


# ─── 桩件 ────────────────────────────────────────────────────────────────────


class _Clock:
    """可控时钟（替换 edge_link 模块的 time，不污染全局 time 模块）。"""

    def __init__(self, start: float = _EPOCH_BASE) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        return None

    def advance(self, delta: float) -> None:
        self.now += delta


class _Producer:
    """桩 Kafka 生产者：记录 (topic, value) 与 flush/close 调用，不连接 broker。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self.flushed = False
        self.closed = False

    def send(self, topic: str, key: Any = None, value: bytes = b"") -> None:
        self.sent.append((topic, value))

    def flush(self) -> None:
        self.flushed = True

    def close(self) -> None:
        self.closed = True


def _settings(**edge_overrides: Any) -> HunterSimSettings:
    """显式 PLAINTEXT，避免测试依赖 SASL 凭据/证书文件。"""
    return HunterSimSettings(
        env="dev",
        kafka=KafkaSettings(
            bootstrap_servers="localhost:9092", security_protocol="PLAINTEXT"
        ),
        edge_link=EdgeLinkSettings(enabled=True, **edge_overrides),
    )


def _make_link(
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ego: Any = None,
    producer: Optional[Any] = None,
    **edge_overrides: Any,
) -> tuple[EdgeLinkRuntime, Any]:
    """构造链路运行时并注入生产者桩。

    刻意不调用 ``start()``：消费线程会引入真实调度抖动，而本文件要断言的是
    速率/年龄/看门狗这些完全由时钟决定的量。控制帧直接用 ``_handle_control``
    喂入，等价于消费线程收到消息后的处理路径。
    """
    monkeypatch.setattr(edge_link_module, "time", clock)
    prod = producer if producer is not None else _Producer()
    runner = None if ego is None else SimpleNamespace(ego_vehicle=ego)
    link = EdgeLinkRuntime(
        _settings(**edge_overrides),
        sim_id="sim-b4-1",
        runner_provider=lambda: runner,
        scene_id="scene-b4",
        map_id="Town03",
        producer_factory=lambda: prod,
        consumer_factory=lambda _topics: SimpleNamespace(poll=lambda _timeout: {}),
    )
    link._producer = prod  # noqa: SLF001  桩件注入，跳过 start() 的线程启动
    return link, prod


def _control(seq: int, linear_x: float = 1.5, angular_z: float = 0.2) -> bytes:
    return json.dumps(
        {"linear_x": linear_x, "angular_z": angular_z, "seq": seq, "wall_ts_ns": seq}
    ).encode("utf-8")


def _status_payload(**overrides: Any) -> SimStatusPayload:
    base: dict[str, Any] = {
        "status": "RUNNING",
        "progress_percent": 10.0,
        "elapsed_time": 3.0,
        "total_time": 30.0,
        "actors_count": 2,
        "collisions": 0,
        "lane_invasions": 0,
        "ego_state": {"x": 0.0, "y": 0.0, "velocity": 1.0},
    }
    base.update(overrides)
    return SimStatusPayload(**base)


def _telemetry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sim_id": "sim-b4-1",
        "vehicle_id": None,
        "scene_id": "scene-b4",
        "seq": 1,
        "ts": _EPOCH_BASE,
        "pose": {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
        "twist": {"vx": 1.0, "vy": 0.0, "vz": 0.0, "wz": 0.0},
        "velocity": 1.0,
        "steering": 0.0,
        "acceleration": 0.0,
    }
    base.update(overrides)
    return build_sim_telemetry(**base)


# ─── 限频浮点容差（B4 修复的仿真侧镜像缺陷）────────────────────────────────────


class TestRateLimiterTolerance:
    def test_nominal_period_not_dropped_at_epoch_scale(self) -> None:
        assert _rate_limited(_EPOCH_BASE, _EPOCH_BASE - 0.1, 10.0) is False

    def test_nominal_cadence_survives_all_rounding_phases(self) -> None:
        """扫描一个浮点间距内的所有相位：标称周期馈送一律不得判为超限。"""
        for phase in range(24):
            last = _EPOCH_BASE + phase * 1e-8
            assert _rate_limited(last + 0.1, last, 10.0) is False

    def test_half_period_still_dropped(self) -> None:
        assert _rate_limited(_EPOCH_BASE, _EPOCH_BASE - 0.05, 10.0) is True

    def test_imu_50hz_nominal_period(self) -> None:
        assert _rate_limited(_EPOCH_BASE, _EPOCH_BASE - 0.02, 50.0) is False

    def test_first_frame_never_dropped(self) -> None:
        assert _rate_limited(_EPOCH_BASE, 0.0, 10.0) is False

    def test_non_positive_fps_means_unlimited(self) -> None:
        assert _rate_limited(100.0, 100.0, 0.0) is False
        assert _rate_limited(100.0, 100.0, -5.0) is False

    def test_small_magnitude_clock_still_limits(self) -> None:
        assert _rate_limited(100.0, 99.9, 10.0) is False
        assert _rate_limited(100.0, 99.95, 10.0) is True


# ─── 速率滑窗 ────────────────────────────────────────────────────────────────


class TestRateHz:
    def test_empty_and_single_sample(self) -> None:
        assert _rate_hz(deque(), 100.0) == 0.0
        assert _rate_hz(deque([100.0]), 100.0) == 0.0

    def test_nominal_10hz(self) -> None:
        stamps = deque([100.0 + i * 0.1 for i in range(11)])
        assert _rate_hz(stamps, 101.0) == pytest.approx(10.0, abs=0.01)

    def test_stalled_stream_decays_to_zero(self) -> None:
        """断流必须衰减到 0：窗口右端锚 now，不能沿用最后一个样本。"""
        stamps = deque([100.0 + i * 0.1 for i in range(11)])
        assert _rate_hz(stamps, 101.0 + _FPS_WINDOW_S) == 0.0

    def test_zero_span_returns_zero(self) -> None:
        assert _rate_hz(deque([100.0, 100.0]), 100.0) == 0.0

    def test_window_trims_old_samples(self) -> None:
        stamps = deque([90.0, 100.0, 100.5, 101.0])
        # 90.0 落在 5s 窗口外：剩 3 个样本 / 1.0s 跨度 → 2Hz
        assert _rate_hz(stamps, 101.0) == pytest.approx(2.0, abs=0.01)


# ─── 链路诊断（stats / summary）──────────────────────────────────────────────


class TestLinkStats:
    def test_initial_stats_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, _ = _make_link(_Clock(), monkeypatch)
        stats = link.stats
        assert stats["enabled"] is True
        assert stats["sim_id"] == "sim-b4-1"
        assert stats["sensor_topic"] == "carla.sim-b4-1.edge_sensor"
        assert stats["control_topic"] == "carla.sim-b4-1.edge_control"
        assert stats["control_source"] == "edge_link"
        assert stats["control_age_ms"] is None
        assert stats["control_hz"] == 0.0
        assert stats["sensor_hz"] == {}
        assert stats["sensor_sent"] == {}
        assert stats["uptime_s"] == 0.0
        assert stats["watchdog_tripped"] is False
        assert stats["waiting_first_control"] is True

    def test_nominal_fps_table_covers_all_streams(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link, _ = _make_link(_Clock(), monkeypatch)
        nominal = link.stats["sensor_nominal_fps"]
        assert set(nominal) == {"rgb", "depth", "lidar", "imu", "ego_state"}
        assert nominal["rgb"] == 10.0
        assert nominal["imu"] == 50.0
        assert nominal["ego_state"] == 20.0

    def test_nominal_fps_follows_settings_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link, _ = _make_link(_Clock(), monkeypatch, rgb_fps=5.0)
        assert link.stats["sensor_nominal_fps"]["rgb"] == 5.0

    def test_nominal_cadence_publishes_every_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归：按标称 10Hz 馈送 10 帧必须全部上行（旧判据会丢掉约一半）。"""
        clock = _Clock()
        link, producer = _make_link(clock, monkeypatch)
        for index in range(10):
            assert link.publish_sensor("rgb", index * 0.1, int(clock.now * 1e9), b"jpeg")
            clock.advance(0.1)
        stats = link.stats
        assert stats["sensor_sent"]["rgb"] == 10
        assert stats["sensor_rate_dropped"].get("rgb", 0) == 0
        assert len(producer.sent) == 10
        assert stats["sensor_hz"]["rgb"] == pytest.approx(10.0, abs=0.2)

    def test_over_rate_frame_counted_as_drop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        link, producer = _make_link(clock, monkeypatch)
        assert link.publish_sensor("rgb", 0.0, 0, b"jpeg") is True
        clock.advance(0.01)
        assert link.publish_sensor("rgb", 0.01, 0, b"jpeg") is False
        stats = link.stats
        assert stats["sensor_rate_dropped"]["rgb"] == 1
        assert stats["sensor_sent"]["rgb"] == 1
        assert len(producer.sent) == 1

    def test_oversize_frame_counted_as_drop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link, _ = _make_link(_Clock(), monkeypatch, max_payload_bytes=1024)
        assert link.publish_sensor("lidar", 0.0, 0, b"x" * 2048) is False
        stats = link.stats
        assert stats["sensor_size_dropped"]["lidar"] == 1
        assert stats["sensor_sent"] == {}

    def test_empty_payload_not_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, producer = _make_link(_Clock(), monkeypatch)
        assert link.publish_sensor("rgb", 0.0, 0, b"") is False
        assert producer.sent == []

    def test_control_rate_and_age(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        link, _ = _make_link(clock, monkeypatch)
        for seq in range(5):
            link._handle_control(_control(seq))  # noqa: SLF001
            clock.advance(0.1)
        stats = link.stats
        assert stats["control_received"] == 5
        assert stats["control_hz"] == pytest.approx(10.0, abs=0.2)
        assert stats["last_linear_x"] == 1.5
        assert stats["last_angular_z"] == 0.2
        # 最后一帧在 base+0.4，当前 base+0.5 → 链路年龄 100ms
        assert stats["control_age_ms"] == pytest.approx(100.0, abs=1.0)

    def test_out_of_order_seq_not_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link, _ = _make_link(_Clock(), monkeypatch)
        link._handle_control(_control(5))  # noqa: SLF001
        link._handle_control(_control(3))  # noqa: SLF001
        assert link.stats["control_received"] == 1

    def test_invalid_control_counted_as_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link, _ = _make_link(_Clock(), monkeypatch)
        link._handle_control(b"not-json")  # noqa: SLF001
        link._handle_control(b"[]")  # noqa: SLF001
        stats = link.stats
        assert stats["control_errors"] == 2
        assert stats["control_received"] == 0
        assert stats["control_hz"] == 0.0

    def test_uptime_advances_with_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        link, _ = _make_link(clock, monkeypatch)
        clock.advance(12.5)
        assert link.stats["uptime_s"] == pytest.approx(12.5, abs=0.01)

    def test_stalled_sensor_rate_decays_to_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """断流后卡片必须显示 0Hz，而不是沿用最后一次速率掩盖断流。"""
        clock = _Clock()
        link, _ = _make_link(clock, monkeypatch)
        for index in range(6):
            link.publish_sensor("rgb", index * 0.1, 0, b"jpeg")
            clock.advance(0.1)
        assert link.stats["sensor_hz"]["rgb"] > 0.0
        clock.advance(_FPS_WINDOW_S + 1.0)
        assert link.stats["sensor_hz"]["rgb"] == 0.0


class TestWatchdogObservability:
    def test_waiting_first_control_reports_paused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link, _ = _make_link(_Clock(), monkeypatch, ego=SimpleNamespace())
        control = link.step(0.02)
        assert control is not None
        assert control.stale is True
        assert link.status_override() == ("PAUSED", "edge_control_waiting")
        stats = link.stats
        assert stats["waiting_first_control"] is True
        assert stats["watchdog_tripped"] is False
        assert stats["boot_sent"] is True

    def test_watchdog_trips_after_gap_and_recovers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        link, _ = _make_link(clock, monkeypatch, ego=SimpleNamespace())
        link._handle_control(_control(1))  # noqa: SLF001
        assert link.step(0.02) is not None
        assert link.stats["watchdog_tripped"] is False

        clock.advance(0.6)  # > watchdog_ms(500)
        control = link.step(0.02)
        assert control is not None
        assert control.stale is True
        assert (control.linear_x, control.angular_z) == (0.0, 0.0)
        assert link.stats["watchdog_tripped"] is True
        assert link.status_override() == ("PAUSED", "edge_control_watchdog")

        link._handle_control(_control(2, linear_x=2.0))  # noqa: SLF001
        recovered = link.step(0.02)
        assert recovered is not None
        assert recovered.stale is False
        assert recovered.linear_x == 2.0
        assert link.stats["watchdog_tripped"] is False
        assert link.status_override() is None

    def test_ego_state_feed_rate_limited_with_tolerance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ego_state 走同一限频判据：20Hz 标称周期不得误丢。"""
        clock = _Clock()
        ego = SimpleNamespace(
            get_transform=lambda: SimpleNamespace(
                location=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                rotation=SimpleNamespace(pitch=0.0, yaw=0.0, roll=0.0),
            ),
            get_velocity=lambda: SimpleNamespace(x=1.0, y=0.0, z=0.0),
            get_angular_velocity=lambda: SimpleNamespace(x=0.0, y=0.0, z=0.0),
        )
        link, producer = _make_link(clock, monkeypatch, ego=ego)
        link._handle_control(_control(1))  # noqa: SLF001
        for _ in range(5):
            link.step(0.05)
            clock.advance(0.05)  # 恰好 20Hz 周期
        stats = link.stats
        assert stats["sensor_sent"].get("ego_state", 0) == 5
        assert stats["sensor_hz"]["ego_state"] == pytest.approx(20.0, abs=0.5)
        assert len(producer.sent) >= 5


class TestFirstControlTimeout:
    """V3.9.2：首帧控制超时 → fatal（结束场景），不再零速空跑满时长。

    实机一轮：车端 sil_bridge 未参与，仿真端仍拉满 duration、录 1.87 GB mcap、
    最后才判 fail。超时即 fatal 可把一次配置缺失压到 30s 内收尾。
    """

    def test_fatal_after_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        link, _ = _make_link(
            clock, monkeypatch, ego=SimpleNamespace(), first_control_timeout_s=2.0
        )
        first = link.step(0.02)
        assert first is not None
        assert first.stale is True
        assert first.fatal is False

        clock.advance(1.9)
        early = link.step(0.02)
        assert early is not None
        assert early.fatal is False

        clock.advance(0.2)  # 累计 2.1s ≥ 2.0s
        late = link.step(0.02)
        assert late is not None
        assert late.fatal is True and late.stale is True
        assert (late.linear_x, late.angular_z) == (0.0, 0.0)
        assert late.reason.startswith("edge_control_unavailable:")
        assert "EDGE_LINK_FIRST_CONTROL_TIMEOUT_S" in late.reason

    def test_zero_disables_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        link, _ = _make_link(
            clock, monkeypatch, ego=SimpleNamespace(), first_control_timeout_s=0.0
        )
        link.step(0.02)
        clock.advance(600.0)
        control = link.step(0.02)
        assert control is not None
        assert control.fatal is False

    def test_control_arrival_never_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超时只针对「整场无 /cmd_vel」：先到控制帧则永不 fatal。"""
        clock = _Clock()
        link, _ = _make_link(
            clock, monkeypatch, ego=SimpleNamespace(), first_control_timeout_s=1.0
        )
        link.step(0.02)
        clock.advance(5.0)
        link._handle_control(_control(1, linear_x=2.0))  # noqa: SLF001
        control = link.step(0.02)
        assert control is not None
        assert control.fatal is False and control.stale is False
        assert control.linear_x == 2.0

    def test_timeout_logged_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """每 tick 都返回 fatal，但 ERROR 只落一条（否则面板被同因告警刷屏）。"""
        clock = _Clock()
        link, _ = _make_link(
            clock, monkeypatch, ego=SimpleNamespace(), first_control_timeout_s=1.0
        )
        link.step(0.02)
        clock.advance(1.5)
        with caplog.at_level(logging.ERROR):
            for _ in range(3):
                control = link.step(0.02)
                assert control is not None and control.fatal is True
        records = [
            record
            for record in caplog.records
            if "edge_control_unavailable" in record.getMessage()
        ]
        assert len(records) == 1

    def test_stats_expose_timeout_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, _ = _make_link(_Clock(), monkeypatch, first_control_timeout_s=45.0)
        assert link.stats["first_control_timeout_s"] == 45.0


class TestRecordSizeGate:
    """V3.9.2：按**序列化后**记录尺寸判限，先于 send() 丢弃并计数。

    kafka-python 超 ``max_request_size`` 时在 ``send()`` 同步抛
    ``MessageSizeTooLargeError``，异常栈裸奔到面板且不经 ``_send_errors``
    （实机面板上行错误 0 的假象）。
    """

    def test_over_limit_record_dropped_before_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link, producer = _make_link(
            _Clock(), monkeypatch, max_payload_bytes=8192, max_record_bytes=1024
        )
        # payload 未超 max_payload_bytes，但 base64 + JSON 信封后超 max_record_bytes
        assert link.publish_sensor("lidar", 0.0, 0, b"x" * 2048) is False
        stats = link.stats
        assert stats["sensor_record_dropped"] == 1
        assert stats["sensor_size_dropped"]["lidar"] == 1
        assert stats["send_errors"] == 0  # 丢弃发生在 send() 之前，不算发送失败
        assert producer.sent == []

    def test_within_limit_still_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, producer = _make_link(
            _Clock(), monkeypatch, max_payload_bytes=8192, max_record_bytes=8192
        )
        assert link.publish_sensor("lidar", 0.0, 0, b"x" * 2048) is True
        assert link.stats["sensor_record_dropped"] == 0
        assert len(producer.sent) == 1

    def test_drop_log_rate_limited(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = _Clock()
        link, _ = _make_link(
            clock, monkeypatch, max_payload_bytes=8192, max_record_bytes=1024
        )
        with caplog.at_level(logging.WARNING):
            link.publish_sensor("lidar", 0.0, 0, b"x" * 2048)
            clock.advance(1.0)
            link.publish_sensor("lidar", 1.0, 0, b"x" * 2048)
        assert link.stats["sensor_record_dropped"] == 2
        records = [
            record for record in caplog.records if "记录超限丢弃" in record.getMessage()
        ]
        assert len(records) == 1

    def test_default_record_limit_is_one_mib(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认值须对齐 Kafka 的 ``message.max.bytes``（1 MiB），不得沿用 4 MiB。"""
        link, _ = _make_link(_Clock(), monkeypatch)
        assert link.stats["record_limit_bytes"] == 1_048_576


class TestSummaryContract:
    def test_summary_key_set_is_minimal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, _ = _make_link(_Clock(), monkeypatch)
        summary = link.summary()
        assert set(summary) == _SUMMARY_KEYS
        # 完整诊断（Topic 名/累计计数/丢弃明细）不进平台契约，避免上行报文膨胀
        assert "sensor_topic" not in summary
        assert "sensor_sent" not in summary
        assert "sensor_nominal_fps" not in summary

    def test_summary_values_track_stats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        link, _ = _make_link(clock, monkeypatch, ego=SimpleNamespace())
        link.publish_sensor("rgb", 0.0, 0, b"jpeg")
        link._handle_control(_control(1))  # noqa: SLF001
        link.step(0.02)
        clock.advance(0.05)
        stats = link.stats
        summary = link.summary()
        assert summary["enabled"] is True
        assert summary["control_source"] == "edge_link"
        assert summary["session_booted"] == stats["boot_sent"]
        assert summary["watchdog_ms"] == stats["watchdog_ms"]
        assert summary["control_hz"] == stats["control_hz"]
        assert summary["sensor_hz"] == stats["sensor_hz"]
        assert summary["uptime_s"] == stats["uptime_s"]


# ─── 平台 payload 增段 ───────────────────────────────────────────────────────


class TestPayloadEdgeLink:
    def test_sim_status_without_edge_link_is_null(self) -> None:
        message = build_sim_status("sim-b4-1", "scene-b4", _status_payload(), seq=1)
        # 与既有 message 字段同风格：未装配为 null，不改变其余字段
        assert message["payload"]["edge_link"] is None
        assert message["payload"]["status"] == "RUNNING"

    def test_sim_status_carries_edge_link(self) -> None:
        segment = {"enabled": True, "control_source": "edge_link", "control_hz": 9.8}
        message = build_sim_status(
            "sim-b4-1", "scene-b4", _status_payload(edge_link=segment), seq=2
        )
        assert message["payload"]["edge_link"] == segment

    def test_telemetry_omits_segment_when_not_attached(self) -> None:
        assert "edge_link" not in _telemetry()["payload"]

    def test_telemetry_carries_segment_copy(self) -> None:
        segment = {"enabled": True, "control_age_ms": 42.0}
        payload = _telemetry(edge_link=segment)["payload"]
        assert payload["edge_link"] == segment
        # 必须是副本：后续链路刷新不得回写已发布报文
        assert payload["edge_link"] is not segment
        segment["control_age_ms"] = 999.0
        assert payload["edge_link"]["control_age_ms"] == 42.0

    def test_telemetry_sim_time_still_present(self) -> None:
        payload = _telemetry(sim_time=12.5, edge_link={"enabled": True})["payload"]
        assert payload["sim_time"] == 12.5
        assert payload["edge_link"] == {"enabled": True}


# ─── runner 侧降级 ───────────────────────────────────────────────────────────


def _runner_with_stats(stats: Any) -> SceneRunnerService:
    """只装配 `_edge_link_summary` 依赖的唯一属性。

    该方法是纯读取 + 降级逻辑，不触碰 CARLA/信箱；用 __new__ 避免完整构造
    引入的引擎副作用，让断言聚焦在降级语义本身。
    """
    runner = SceneRunnerService.__new__(SceneRunnerService)
    runner._edge_link_stats = stats  # noqa: SLF001
    return runner


class TestRunnerEdgeLinkSummary:
    def test_not_attached_returns_none(self) -> None:
        assert _runner_with_stats(None)._edge_link_summary() is None  # noqa: SLF001

    def test_returns_copy_of_stats(self) -> None:
        segment = {"enabled": True, "control_hz": 10.0}
        result = _runner_with_stats(lambda: segment)._edge_link_summary()  # noqa: SLF001
        assert result == segment
        assert result is not segment

    def test_provider_error_degrades_to_none(self) -> None:
        def _boom() -> dict[str, Any]:
            raise RuntimeError("edge link released")

        assert _runner_with_stats(_boom)._edge_link_summary() is None  # noqa: SLF001

    def test_non_dict_result_degrades_to_none(self) -> None:
        assert _runner_with_stats(lambda: None)._edge_link_summary() is None  # noqa: SLF001
        assert _runner_with_stats(lambda: "bad")._edge_link_summary() is None  # noqa: SLF001


# ─── 服务只读视图 ────────────────────────────────────────────────────────────


def _service(executor: Any, store: Optional[InstanceStore] = None) -> AdapterService:
    return AdapterService(
        HunterSimSettings(),
        AdapterSettings(db_path="unused.db", artifacts_dir="unused"),
        store,
        executor,
    )


class TestServiceEdgeLink:
    def test_fallback_when_executor_lacks_getter(self) -> None:
        assert _service(SimpleNamespace()).edge_link("inst-1") == {
            "enabled": False,
            "control_source": "script",
        }

    def test_passthrough_returns_copy(self) -> None:
        segment = {"enabled": True, "control_source": "edge_link", "control_hz": 9.5}
        executor = SimpleNamespace(edge_link_status=lambda _id: segment)
        result = _service(executor).edge_link("inst-1")
        assert result == segment
        assert result is not segment

    def test_getter_error_degrades(self) -> None:
        def _boom(_instance_id: str) -> dict[str, Any]:
            raise RuntimeError("context released")

        assert _service(SimpleNamespace(edge_link_status=_boom)).edge_link("inst-1") == {
            "enabled": False,
            "control_source": "script",
        }

    def test_non_dict_result_degrades(self) -> None:
        executor = SimpleNamespace(edge_link_status=lambda _id: None)
        assert _service(executor).edge_link("inst-1")["enabled"] is False

    def test_runtime_status_without_active_instance(self, tmp_path: Path) -> None:
        store = InstanceStore(str(tmp_path / "adapter.db")).connect()
        try:
            status = _service(SimpleNamespace(), store).runtime_status()
            assert status["edge_link"] == {"enabled": False, "control_source": "script"}
        finally:
            store.close()

    def test_runtime_status_with_active_instance(self, tmp_path: Path) -> None:
        store = InstanceStore(str(tmp_path / "adapter.db")).connect()
        try:
            store.insert(
                InstanceRecord(
                    instance_id="inst-b4",
                    sim_id="sim-b4-1",
                    scene_id="scene-b4",
                    scene_name="真 SIL 链路",
                    mode="sil",
                    status="running",
                    scene_config={"duration": 30.0},
                    total_time_s=30.0,
                    actors_count=1,
                )
            )
            segment = {"enabled": True, "control_source": "edge_link", "control_hz": 10.0}
            executor = SimpleNamespace(edge_link_status=lambda _id: segment)
            status = _service(executor, store).runtime_status()
            assert status["instances"]["active"] == 1
            assert status["edge_link"] == segment
        finally:
            store.close()


# ─── viz 端点骨架 ────────────────────────────────────────────────────────────


class TestVizStatusSkeleton:
    def test_status_contains_edge_link_without_service_snapshot(self) -> None:
        """服务替身缺 runtime_status 时，前端仍拿到确定形状（不报错、不缺字段）。"""
        with TestClient(create_app(service=SimpleNamespace())) as client:
            body = client.get("/viz/api/status").json()
        assert body["edge_link"] == {"enabled": False, "control_source": "script"}

    def test_status_snapshot_overrides_skeleton(self) -> None:
        segment = {"enabled": True, "control_source": "edge_link", "control_hz": 8.0}
        service = SimpleNamespace(runtime_status=lambda: {"edge_link": segment})
        with TestClient(create_app(service=service)) as client:
            body = client.get("/viz/api/status").json()
        assert body["edge_link"] == segment


# ─── 生命周期 / 会话信令 / 编解码（mock Kafka）──────────────────────


class _Consumer:
    """桩消费者：先吐预置批次，批次耗尽后回调 ``on_exhausted``。

    两种驱动方式：
    - **同步**（设 on_exhausted=link._running.clear）：测试线程直调 ``_consume_loop``，
      吐完批次即自然退出——完全确定性，无 sleep、无线程调度抖动（编码规则 §10.2）。
    - **异步**（不设回调）：start() 的真实消费线程用 Event.wait 让出 CPU，避免空转。
      不用 time.sleep 是因为 edge_link 模块的 time 已被 _Clock 接管（sleep 为空操作）。
    """

    def __init__(self, batches: Optional[list[dict[str, Any]]] = None) -> None:
        self._batches = list(batches or [])
        self._gate = threading.Event()
        self.on_exhausted: Optional[Callable[[], None]] = None
        self.closed = False
        self.polls = 0
        self.raise_once = False

    def poll(self, timeout_ms: int = 0) -> dict[str, Any]:
        self.polls += 1
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("broker rebalance")
        if self._batches:
            return self._batches.pop(0)
        if self.on_exhausted is not None:
            self.on_exhausted()
            return {}
        self._gate.wait(0.05)
        return {}

    def close(self) -> None:
        self.closed = True
        self._gate.set()


def _make_unstarted_link(
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sim_id: str = "sim-b4-1",
    ego: Any = None,
    producer: Optional[Any] = None,
    consumer: Optional[Any] = None,
    producer_factory: Optional[Callable[[], Any]] = None,
    consumer_factory: Optional[Callable[[list[str]], Any]] = None,
    admin_factory: Optional[Callable[[], Any]] = None,
    edge_config: Optional[dict[str, Any]] = None,
    **edge_overrides: Any,
) -> tuple[EdgeLinkRuntime, Any, Any]:
    """构造未启动的链路（工厂已接线），由测试自行调 start()/stop()。"""
    monkeypatch.setattr(edge_link_module, "time", clock)
    prod = producer if producer is not None else _Producer()
    cons = consumer if consumer is not None else _Consumer()
    runner = None if ego is None else SimpleNamespace(ego_vehicle=ego)
    link = EdgeLinkRuntime(
        _settings(**edge_overrides),
        sim_id=sim_id,
        runner_provider=lambda: runner,
        scene_id="scene-b4",
        map_id="Town03",
        edge_config=edge_config,
        producer_factory=producer_factory if producer_factory is not None else (lambda: prod),
        consumer_factory=(
            consumer_factory if consumer_factory is not None else (lambda _topics: cons)
        ),
        admin_factory=admin_factory,
    )
    return link, prod, cons


def _envelopes(producer: Any) -> list[dict[str, Any]]:
    return [json.loads(value.decode("utf-8")) for _topic, value in producer.sent]


class TestLinkLifecycle:
    def test_start_then_stop_closes_both_ends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, producer, consumer = _make_unstarted_link(_Clock(), monkeypatch)
        link.start()
        try:
            assert link._producer is producer  # noqa: SLF001
            assert link._consumer is consumer  # noqa: SLF001
        finally:
            link.stop()
        assert consumer.closed is True
        assert producer.closed is True
        assert producer.flushed is True
        assert link._producer is None  # noqa: SLF001
        assert link._consumer is None  # noqa: SLF001

    def test_start_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        def _factory() -> Any:
            calls.append(1)
            return _Producer()

        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, producer_factory=_factory
        )
        link.start()
        try:
            link.start()
            assert len(calls) == 1
        finally:
            link.stop()

    def test_stop_is_idempotent_and_sends_one_stop_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link, producer, _consumer = _make_unstarted_link(_Clock(), monkeypatch)
        link.start()
        link.stop()
        link.stop()
        phases = [
            item["meta"]["phase"]
            for item in _envelopes(producer)
            if item["stream"] == "session"
        ]
        assert phases == ["stop"]

    def test_start_requires_sim_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, sim_id=""
        )
        with pytest.raises(KafkaConnectionError) as excinfo:
            link.start()
        assert "sim_id" in str(excinfo.value)

    def test_producer_failure_fast_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SIL_EDGE 快速失败：不得静默回退脚本自车（避免假 SIL）。"""

        def _boom() -> Any:
            raise RuntimeError("broker unreachable")

        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, producer_factory=_boom
        )
        with pytest.raises(KafkaConnectionError) as excinfo:
            link.start()
        text = str(excinfo.value)
        assert "edge_sensor" in text
        assert "快速失败" in text
        assert "broker unreachable" in text

    def test_consumer_failure_closes_producer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_topics: list[str]) -> Any:
            raise RuntimeError("group auth failed")

        link, producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, consumer_factory=_boom
        )
        with pytest.raises(KafkaConnectionError) as excinfo:
            link.start()
        assert "edge_control" in str(excinfo.value)
        # 已建生产者必须回收，不能泄漏连接
        assert producer.closed is True
        assert link._producer is None  # noqa: SLF001

    def test_consume_loop_applies_control_frames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        batch = {
            "topic-0": [
                SimpleNamespace(value=_control(1, linear_x=1.0)),
                SimpleNamespace(value=_control(2, linear_x=2.5)),
            ]
        }
        consumer = _Consumer([batch])
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, consumer=consumer
        )
        # 同步驱动消费循环：批次耗尽即清 _running 退出，无需线程与 sleep。
        # 必须注入桩消费者：_consume_loop 读的是 self._consumer，为 None 时走
        # sleep+continue 分支，而 _Clock.sleep 是空操作 → 无限忙循环。
        link._consumer = consumer  # noqa: SLF001
        consumer.on_exhausted = link._running.clear  # noqa: SLF001
        link._running.set()  # noqa: SLF001
        link._consume_loop()  # noqa: SLF001
        stats = link.stats
        assert stats["control_received"] == 2
        assert stats["last_linear_x"] == 2.5
        assert stats["control_errors"] == 0

    def test_consume_loop_survives_poll_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        consumer = _Consumer()
        consumer.raise_once = True
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, consumer=consumer
        )
        link._consumer = consumer  # noqa: SLF001  同步驱动，不起消费线程
        consumer.on_exhausted = link._running.clear  # noqa: SLF001
        link._running.set()  # noqa: SLF001
        link._consume_loop()  # noqa: SLF001
        # 轮询异常只得退避重试，不得退出循环（否则控制下行永久断流）：
        # 首次 poll 抛错后仍继续轮询，直到批次耗尽才退出
        assert consumer.polls >= 2
        assert link.stats["control_received"] == 0

    def test_step_without_bound_ego_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, producer, _consumer = _make_unstarted_link(_Clock(), monkeypatch)
        assert link.step(0.02) is None
        assert producer.sent == []
        assert link.stats["steps"] == 1


class _Admin:
    """桩 KafkaAdminClient：只记调用，不连接 broker。"""

    def __init__(
        self,
        topics: Optional[list[str]] = None,
        create_error: Optional[BaseException] = None,
        list_error: Optional[BaseException] = None,
    ) -> None:
        self.topics = set(topics or [])
        self.create_error = create_error
        self.list_error = list_error
        self.created: list[str] = []
        self.specs: list[Any] = []
        self.broker_count = 1
        self.closed = False

    def list_topics(self) -> list[str]:
        if self.list_error is not None:
            raise self.list_error
        return sorted(self.topics)

    def describe_cluster(self) -> dict[str, Any]:
        return {"brokers": [{"id": i} for i in range(self.broker_count)]}

    def create_topics(self, new_topics: Any, timeout_ms: Optional[int] = None) -> None:
        if self.create_error is not None:
            raise self.create_error
        self.created.extend(topic.name for topic in new_topics)
        self.specs.extend(new_topics)
        self.topics.update(topic.name for topic in new_topics)

    def close(self) -> None:
        self.closed = True


class TestTopicProvisioning:
    """V3.9.1：broker 关闭 auto.create 时必须补建 edge_* Topic。

    生产者 send() 在 Topic 不存在时会阻塞满 max_block_ms 后抛
    KafkaTimeoutError（实机每秒 5 条），消费端则永远拿不到分区。
    """

    def test_creates_missing_topics_and_closes_admin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        admin = _Admin()
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, admin_factory=lambda: admin
        )
        link._ensure_topics()  # noqa: SLF001
        assert admin.created == ["carla.sim-b4-1.edge_sensor", "carla.sim-b4-1.edge_control"]
        assert admin.closed is True

    def test_topic_shape_is_one_partition_and_capped_rf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 分区（sim_id 单一 key 本就同分区）+ 副本数 min(3, broker 数)。

        kafka-python 的 CreateTopics 只到 v3，``-1``（用集群默认）不可靠；
        单 broker 现场 RF 必须为 1，否则 InvalidReplicationFactor 直接失败。
        """
        admin = _Admin()
        admin.broker_count = 5
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, admin_factory=lambda: admin
        )
        link._ensure_topics()  # noqa: SLF001
        assert [(t.num_partitions, t.replication_factor) for t in admin.specs] == [
            (1, 3),
            (1, 3),
        ]

    def test_existing_topics_skip_creation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        admin = _Admin(["carla.sim-b4-1.edge_sensor", "carla.sim-b4-1.edge_control"])
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, admin_factory=lambda: admin
        )
        link._ensure_topics()  # noqa: SLF001
        assert admin.created == []
        assert admin.closed is True

    def test_list_failure_only_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """自检不可用时保持既有启动行为，不把可用链路误判为故障。"""
        admin = _Admin(list_error=RuntimeError("metadata denied"))
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, admin_factory=lambda: admin
        )
        link._ensure_topics()  # noqa: SLF001
        assert admin.created == []
        assert admin.closed is True

    def test_already_exists_is_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kafka.errors import TopicAlreadyExistsError

        admin = _Admin(create_error=TopicAlreadyExistsError("exists"))
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, admin_factory=lambda: admin
        )
        link._ensure_topics()  # noqa: SLF001  并发建 Topic 属成功

    def test_auth_failure_fast_fails_with_acl_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 CREATE 权限 → 快速失败，且报错给的是**权限口径**而非手工建 Topic。

        sim_id 每轮都变，手工 ``kafka-topics.sh --topic carla.<sim_id>...`` 不可能
        作为常规手段；可执行的修法是按 ``carla.*`` 前缀授 ACL 或开 broker
        ``auto.create``，Topic 本身由 AdminClient 预建。
        """
        from kafka.errors import TopicAuthorizationFailedError

        admin = _Admin(create_error=TopicAuthorizationFailedError("denied"))
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, admin_factory=lambda: admin
        )
        with pytest.raises(KafkaConnectionError) as excinfo:
            link._ensure_topics()  # noqa: SLF001
        text = str(excinfo.value)
        assert "edge_sensor" in text and "edge_control" in text
        # 修法口径：前缀 ACL 或 broker auto.create（两项都要能看到）
        assert "CREATE" in text and "carla.*" in text
        assert "auto.create.topics.enable" in text
        # 守住「不引导手工预建」的决策（sim_id 每轮变）
        assert "kafka-topics.sh" in text and "不是常规手段" in text

    def test_start_provisions_topics_before_building_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        admin = _Admin()
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, admin_factory=lambda: admin
        )
        # 去掉工厂注入，模拟真实连接路径（build_* 打桩，不连 broker）
        link._producer_factory = None  # noqa: SLF001
        link._consumer_factory = None  # noqa: SLF001
        monkeypatch.setattr(link, "_build_producer", lambda: _Producer())
        monkeypatch.setattr(link, "_build_consumer", lambda: _Consumer())
        link.start()
        try:
            assert admin.created == [
                "carla.sim-b4-1.edge_sensor",
                "carla.sim-b4-1.edge_control",
            ]
        finally:
            link.stop()


class TestSessionSignalling:
    def test_boot_frame_carries_session_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        link, producer = _make_link(clock, monkeypatch, ego=SimpleNamespace())
        link.step(0.02)
        boot = _envelopes(producer)[0]
        assert boot["stream"] == "session"
        assert boot["payload_b64"] == ""
        assert boot["wall_ts_ns"] == int(clock.now * 1e9)
        meta = boot["meta"]
        assert meta["phase"] == "boot"
        assert meta["sim_id"] == "sim-b4-1"
        assert meta["scene_id"] == "scene-b4"
        assert meta["map_id"] == "Town03"
        assert meta["streams"] == ["rgb", "depth", "lidar", "imu", "ego_state"]
        assert meta["watchdog_ms"] == 500
        assert link.stats["boot_sent"] is True

    def test_boot_sent_only_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, producer = _make_link(_Clock(), monkeypatch, ego=SimpleNamespace())
        link.step(0.02)
        link.step(0.02)
        sessions = [item for item in _envelopes(producer) if item["stream"] == "session"]
        assert len(sessions) == 1

    def test_stop_frame_after_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, ego=SimpleNamespace()
        )
        link.start()
        link.step(0.02)
        link.stop()
        phases = [
            item["meta"]["phase"]
            for item in _envelopes(producer)
            if item["stream"] == "session"
        ]
        assert phases == ["boot", "stop"]

    def test_sensor_envelope_roundtrip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上行信封可被车端原样解码（stream/sim_ts/wall_ts_ns/seq/meta/payload_b64）。"""
        clock = _Clock()
        link, producer = _make_link(clock, monkeypatch)
        payload = b"\x00\x01jpeg-bytes"
        assert link.publish_sensor("rgb", 1.25, 1_760_000_000_000_000_000, payload, {"width": 640})
        topic, raw = producer.sent[0]
        assert topic == "carla.sim-b4-1.edge_sensor"
        envelope = json.loads(raw.decode("utf-8"))
        assert envelope["stream"] == "rgb"
        assert envelope["sim_ts"] == 1.25
        assert envelope["wall_ts_ns"] == 1_760_000_000_000_000_000
        assert envelope["seq"] == 1
        assert envelope["meta"] == {"width": 640}
        assert base64.b64decode(envelope["payload_b64"]) == payload

    def test_seq_is_monotonic_across_streams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        link, producer = _make_link(clock, monkeypatch)
        link.publish_sensor("rgb", 0.0, 0, b"a")
        clock.advance(0.2)
        link.publish_sensor("lidar", 0.2, 0, b"b")
        assert [item["seq"] for item in _envelopes(producer)] == [1, 2]


class TestSendFailurePaths:
    def test_send_without_producer_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, _producer, _consumer = _make_unstarted_link(_Clock(), monkeypatch)
        assert link.publish_sensor("rgb", 0.0, 0, b"a") is False
        assert link._send_session("boot") is None  # noqa: SLF001  内部无返回值
        assert link.stats["send_errors"] == 0

    def test_producer_send_error_counted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _BrokenProducer(_Producer):
            def send(self, topic: str, key: Any = None, value: bytes = b"") -> None:
                raise RuntimeError("broker gone")

        link, _producer = _make_link(
            _Clock(), monkeypatch, producer=_BrokenProducer()
        )
        assert link.publish_sensor("rgb", 0.0, 0, b"a") is False
        stats = link.stats
        assert stats["send_errors"] == 1
        # 已计入发送计数（限频样本已记），但失败不阻断仿真
        assert stats["sensor_sent"]["rgb"] == 1

    def test_circular_envelope_counted_as_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, _producer = _make_link(_Clock(), monkeypatch)
        circular: dict[str, Any] = {}
        circular["self"] = circular
        assert link._send(circular) is False  # noqa: SLF001
        assert link.stats["send_errors"] == 1

    def test_repeated_send_errors_are_throttled(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Topic 缺失时 1Hz×5 流的同因告警不得刷屏（V3.9.1）。"""

        class _BrokenProducer(_Producer):
            def send(self, topic: str, key: Any = None, value: bytes = b"") -> None:
                raise RuntimeError("Failed to update metadata after 1.0 secs")

        clock = _Clock()
        link, _producer = _make_link(clock, monkeypatch, producer=_BrokenProducer())
        with caplog.at_level("WARNING", logger="hunter_sim.cloud.edge_link"):
            assert link.publish_sensor("rgb", 0.0, 0, b"a") is False
            assert link.publish_sensor("lidar", 0.0, 0, b"b") is False
        messages = [r.getMessage() for r in caplog.records if "send failed" in r.getMessage()]
        assert len(messages) == 1
        assert link._send_error_suppressed == 1  # noqa: SLF001
        assert link.stats["send_errors"] == 2

    def test_flush_close_tolerates_stub_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _RudeProducer:
            def flush(self) -> None:
                raise RuntimeError("flush failed")

            def close(self) -> None:
                raise RuntimeError("close failed")

        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, producer=_RudeProducer()
        )
        link.start()
        link.stop()  # 不得因 flush/close 异常而抛出
        assert link._producer is None  # noqa: SLF001

    def test_ego_state_read_failure_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """自车真值读取失败只降级 ego_state，不得阻断控制接管。"""
        link, producer = _make_link(_Clock(), monkeypatch, ego=SimpleNamespace())
        link._handle_control(_control(1, linear_x=3.0))  # noqa: SLF001
        control = link.step(0.02)
        assert control is not None
        assert control.linear_x == 3.0
        assert link._read_ego_state() is None  # noqa: SLF001
        assert link.stats["sensor_sent"].get("ego_state", 0) == 0
        assert all(item["stream"] != "ego_state" for item in _envelopes(producer))

    def test_unknown_stream_is_unlimited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, producer = _make_link(_Clock(), monkeypatch)
        assert link._fps_for("custom") == 0.0  # noqa: SLF001
        assert link.publish_sensor("custom", 0.0, 0, b"a") is True
        assert link.publish_sensor("custom", 0.0, 0, b"b") is True
        assert len(producer.sent) == 2

    def test_invalid_seq_does_not_block_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, _producer = _make_link(_Clock(), monkeypatch)
        raw = json.dumps({"linear_x": 1.0, "angular_z": 0.0, "seq": "bad"}).encode("utf-8")
        link._handle_control(raw)  # noqa: SLF001
        assert link.stats["control_received"] == 1


class TestKafkaBuilderKwargs:
    def test_producer_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class _StubProducer:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        monkeypatch.setattr("kafka.KafkaProducer", _StubProducer)
        link, _producer, _consumer = _make_unstarted_link(_Clock(), monkeypatch)
        link._build_producer()  # noqa: SLF001
        assert captured["bootstrap_servers"] == ["localhost:9092"]
        assert captured["acks"] == 1
        assert captured["security_protocol"] == "PLAINTEXT"
        assert captured["retries"] == 3
        # V3.9.2：显式跟随记录上限（+ 协议头预留）。不设则 kafka-python 默认
        # 1 MiB，会在 send() 同步抛 MessageSizeTooLargeError。
        limit = link.stats["record_limit_bytes"]
        assert captured["max_request_size"] == limit + edge_link_module._MAX_REQUEST_OVERHEAD_BYTES  # noqa: SLF001,E501

    def test_consumer_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        topics: list[Any] = []

        class _StubConsumer:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                topics.extend(args)
                captured.update(kwargs)

        monkeypatch.setattr("kafka.KafkaConsumer", _StubConsumer)
        link, _producer, _consumer = _make_unstarted_link(_Clock(), monkeypatch)
        link._build_consumer()  # noqa: SLF001
        assert topics == ["carla.sim-b4-1.edge_control"]
        assert captured["group_id"] == "carla-edge-control"
        assert captured["auto_offset_reset"] == "latest"
        assert captured["enable_auto_commit"] is True


class TestConfigOverride:
    def test_unknown_key_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, edge_config={"not_a_field": 1, "rgb_fps": 5.0}
        )
        assert link.stats["sensor_nominal_fps"]["rgb"] == 5.0

    def test_invalid_override_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, edge_config={"rgb_fps": "not-a-number"}
        )
        assert link.stats["sensor_nominal_fps"]["rgb"] == 10.0

    def test_empty_override_keeps_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link, _producer, _consumer = _make_unstarted_link(
            _Clock(), monkeypatch, edge_config={}
        )
        assert link.enabled is True
        assert link.stats["watchdog_ms"] == 500
