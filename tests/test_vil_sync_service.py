"""VILSyncService 同步循环测试（V2.12.0）。

覆盖审查中加固的并发安全与日志治理行为：
- ``stats`` 返回只读副本（持锁快照，外部修改不污染内部状态）；
- ``avg_latency_ms`` 由有界延迟样本推导；
- 数据中断（``SimTimeoutError``）日志限频：5s 窗口内只告警一次，
  但 ``sync_errors`` 仍逐帧累加。

使用桩件替代 CARLA / 坐标转换 / 外推，无需真实引擎。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from hunter_sim.common.models import VILSettings
from hunter_sim.vil_mapper.data_health_monitor import DataHealthStatus
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer
from hunter_sim.vil_mapper.vil_sync_service import VILSyncService


# ── 桩件 ──────────────────────────────────────────────────────────────────────


class _FakeReport:
    def __init__(self, status: DataHealthStatus) -> None:
        self.status = status


class _FakeHealth:
    """固定返回指定健康状态，可切换。"""

    def __init__(self, status: DataHealthStatus) -> None:
        self._status = status

    @property
    def current_status(self) -> DataHealthStatus:
        return self._status

    def check(self) -> _FakeReport:
        return _FakeReport(self._status)


class _FakeCoord:
    def odom_to_carla(self, **_kwargs: Any) -> tuple[float, float, float, float, float, float]:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


class _FakeExtrapolator:
    def extrapolate_transform(self, transform: Any, _speed: float, _yaw_rate: float) -> Any:
        return transform


class _FakeVehicle:
    def __init__(self) -> None:
        self.transforms: list[Any] = []

    def set_transform(self, transform: Any) -> None:
        self.transforms.append(transform)

    def set_velocity(self, vector: Any) -> None:  # noqa: ARG002
        return None

    def set_angular_velocity(self, vector: Any) -> None:  # noqa: ARG002
        return None


class _FakeWorld:
    def __init__(self) -> None:
        self.ticks = 0

    def tick(self) -> int:
        self.ticks += 1
        return self.ticks


def _service(status: DataHealthStatus = DataHealthStatus.INTERRUPTED) -> VILSyncService:
    return VILSyncService(
        settings=VILSettings(),
        buffer=TelemetryBuffer(),
        coord_transformer=_FakeCoord(),  # type: ignore[arg-type]
        extrapolator=_FakeExtrapolator(),  # type: ignore[arg-type]
        health_monitor=_FakeHealth(status),  # type: ignore[arg-type]
        vehicle_actor=_FakeVehicle(),  # type: ignore[arg-type]
        world=_FakeWorld(),  # type: ignore[arg-type]
    )


# ── 统计快照 ──────────────────────────────────────────────────────────────────


class TestStatsSnapshot:
    def test_stats_returns_copy(self) -> None:
        """外部修改快照不得污染内部累计值。"""
        svc = _service()
        snapshot = svc.stats
        snapshot.total_ticks = 999
        assert svc.stats.total_ticks == 0

    def test_avg_latency_from_samples(self) -> None:
        svc = _service()
        svc._latency_samples.extend([1.0, 3.0])
        assert svc.stats.avg_latency_ms == pytest.approx(2.0)

    def test_avg_latency_zero_without_samples(self) -> None:
        assert _service().stats.avg_latency_ms == 0.0

    def test_fps_zero_with_single_tick(self) -> None:
        svc = _service()
        svc._tick_times.append(time.perf_counter())
        assert svc.stats.current_fps == 0.0


# ── 断流日志限频 ──────────────────────────────────────────────────────────────


class TestTimeoutLogThrottle:
    def test_logs_once_per_window_but_counts_every_tick(self, caplog: pytest.LogCaptureFixture) -> None:
        """50Hz 下连续断流：告警最多一条，sync_errors 逐帧累加。"""
        caplog.set_level("WARNING", logger="hunter_sim.vil_mapper.vil_sync_service")
        svc = _service(DataHealthStatus.INTERRUPTED)

        thread = threading.Thread(target=svc.start_sync_loop, daemon=True)
        thread.start()
        time.sleep(0.3)
        svc.stop()
        thread.join(timeout=2.0)

        assert not thread.is_alive(), "sync loop must exit after stop()"

        timeout_logs = [r for r in caplog.records if "Sync timeout" in r.getMessage()]
        assert len(timeout_logs) == 1, timeout_logs

        stats = svc.stats
        assert stats.sync_errors >= 2, "每一 tick 仍必须计入失败次数"
        assert stats.total_ticks == stats.sync_errors + stats.total_syncs

    def test_healthy_cycle_counts_syncs(self) -> None:
        """健康但无遥测帧：不抛错、不累计 sync_errors。"""
        svc = _service(DataHealthStatus.OK)

        thread = threading.Thread(target=svc.start_sync_loop, daemon=True)
        thread.start()
        time.sleep(0.2)
        svc.stop()
        thread.join(timeout=2.0)

        assert not thread.is_alive()
        assert svc.stats.sync_errors == 0
        assert svc.stats.total_syncs >= 1
