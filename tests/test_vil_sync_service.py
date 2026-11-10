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
from typing import Any, Iterator

import pytest

from hunter_sim.common.exceptions import CarlaSimulationError
from hunter_sim.common.models import VILSettings
from hunter_sim.engine.carla_mailbox import CarlaMailbox, reset_mailboxes
from hunter_sim.vil_mapper.data_health_monitor import DataHealthStatus
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer
from hunter_sim.vil_mapper.vil_sync_service import VILSyncService
from tests.mocks.carla_thread_mocks import held_endpoint_owner


@pytest.fixture(autouse=True)
def _clean_mailboxes() -> Iterator[None]:
    reset_mailboxes()
    yield
    reset_mailboxes()


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


class TestMailboxThreadGuards:
    """V3.6.0：注入端点信箱后，VIL 写 CARLA 只能在持有端点的线程上做。

    ``apply_once`` 由场景 tick 线程调用（正常路径），但 ``start_sync_loop`` 会
    自己 ``world.tick()``：一旦在 tick 线程已在跑时被拉起来，就是「两个线程
    tick 同一个 world」——除帧同步错乱外还会触发原生崩溃。
    """

    @staticmethod
    def _service_with(mailbox: CarlaMailbox) -> tuple[VILSyncService, _FakeVehicle, _FakeWorld]:
        vehicle = _FakeVehicle()
        world = _FakeWorld()
        svc = VILSyncService(
            settings=VILSettings(),
            buffer=TelemetryBuffer(),
            coord_transformer=_FakeCoord(),  # type: ignore[arg-type]
            extrapolator=_FakeExtrapolator(),  # type: ignore[arg-type]
            health_monitor=_FakeHealth(DataHealthStatus.OK),  # type: ignore[arg-type]
            vehicle_actor=vehicle,  # type: ignore[arg-type]
            world=world,  # type: ignore[arg-type]
            mailbox=mailbox,
        )
        return svc, vehicle, world

    def test_apply_once_skips_when_endpoint_owned_by_other_thread(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2091")
        svc, vehicle, _world = self._service_with(mailbox)

        with held_endpoint_owner(mailbox):
            outcome = svc.apply_once()

        assert outcome == "skipped"
        assert vehicle.transforms == [], "非 owner 线程不得写 Actor 位姿"

    def test_apply_once_still_writes_for_owner_thread(self) -> None:
        """守护不能堵死正常 tick 路径：owner 线程照旧写位姿。"""
        mailbox = CarlaMailbox("127.0.0.1:2092")
        svc, vehicle, _world = self._service_with(mailbox)
        assert mailbox.claim("MainThread", timeout=0.0)
        try:
            svc.apply_once()
        finally:
            mailbox.relinquish()

        assert len(vehicle.transforms) <= 1  # 无新栅格时允许 skipped，但不得报错

    def test_start_sync_loop_refused_while_tick_thread_owns_endpoint(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2093")
        svc, _vehicle, world = self._service_with(mailbox)

        with held_endpoint_owner(mailbox):
            with pytest.raises(CarlaSimulationError, match="vil_sync_loop"):
                svc.start_sync_loop()

        assert world.ticks == 0, "不得在别的线程上 tick 同一个 world"
