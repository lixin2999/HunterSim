"""VIL 引擎主循环单元测试（对应 §4.5.1）。"""

from __future__ import annotations

from typing import Any

from hunter_sim.app.vil.models import (
    ChassisData,
    LocalizationData,
    PerceptionData,
    VehicleTelemetry,
    VILCalibration,
    VILConfig,
)
from hunter_sim.app.vil.orchestrator import VILEngineImpl
from hunter_sim.simulation.models import Location, Rotation, Transform


class _FakeScenario:
    def __init__(self) -> None:
        self.step_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0

    async def step(self) -> None:
        self.step_calls += 1

    async def pause(self) -> None:
        self.pause_calls += 1

    async def resume(self) -> None:
        self.resume_calls += 1


class _FakeTelemetrySource:
    def __init__(self, initial: VehicleTelemetry | None = None) -> None:
        self._latest = initial
        self.start_calls = 0
        self.stop_calls = 0

    def set(self, telemetry: VehicleTelemetry | None) -> None:
        self._latest = telemetry

    async def start(self) -> None:
        self.start_calls += 1

    async def poll_latest(self) -> VehicleTelemetry | None:
        return self._latest

    async def stop(self) -> None:
        self.stop_calls += 1


class _FakeSynchronizer:
    def __init__(self) -> None:
        self.calls: list[VehicleTelemetry] = []

    async def sync(self, telemetry: VehicleTelemetry) -> Transform:
        self.calls.append(telemetry)
        return Transform(
            location=Location(x=1.0, y=2.0, z=0.3),
            rotation=Rotation(pitch=0.0, yaw=0.0, roll=0.0),
        )


class _FakeVisualizer:
    def __init__(self) -> None:
        self.calls: list[tuple[VehicleTelemetry, Transform]] = []

    async def adraw_all(self, telemetry: VehicleTelemetry, map_pose: Transform) -> None:
        self.calls.append((telemetry, map_pose))


class _FakeSyncCtrl:
    def __init__(self, *, pause_at: float, extrapolate_at: float = 0.0) -> None:
        self.pause_at = pause_at
        self.extrapolate_at = extrapolate_at

    def should_pause(self, delay_ms: float) -> bool:
        return delay_ms > self.pause_at

    def should_extrapolate(self, delay_ms: float) -> bool:
        return delay_ms > self.extrapolate_at

    def compensate(
        self, telemetry: VehicleTelemetry, elapsed_s: float
    ) -> VehicleTelemetry:  # pragma: no cover - 编排器不直接调用
        return telemetry


def _make_config(**overrides: Any) -> VILConfig:
    base: dict[str, Any] = {
        "target_vehicle_id": "hunter-001",
        "calibration": VILCalibration(x0=0.0, y0=0.0, yaw0=0.0),
        "data_timeout_ms": 500.0,
        "extrapolation_threshold_ms": 50.0,
    }
    base.update(overrides)
    return VILConfig(**base)


def _make_telemetry(ts: float) -> VehicleTelemetry:
    return VehicleTelemetry(
        vehicle_id="hunter-001",
        timestamp=ts,
        localization=LocalizationData(x=0.0, y=0.0, heading=0.0),
        chassis=ChassisData(velocity=5.0, steering=0.0, throttle=0.0, brake=0.0),
        perception=PerceptionData(),
    )


def _build(
    *,
    source: _FakeTelemetrySource,
    sync_ctrl: _FakeSyncCtrl,
    now: float = 1000.0,
) -> tuple[VILEngineImpl, _FakeScenario, _FakeSynchronizer, _FakeVisualizer]:
    scenario = _FakeScenario()
    synchronizer = _FakeSynchronizer()
    visualizer = _FakeVisualizer()
    engine = VILEngineImpl(
        scenario=scenario,  # type: ignore[arg-type]
        telemetry_source=source,  # type: ignore[arg-type]
        synchronizer=synchronizer,  # type: ignore[arg-type]
        visualizer=visualizer,  # type: ignore[arg-type]
        sync_ctrl=sync_ctrl,  # type: ignore[arg-type]
        config=_make_config(),
        clock=lambda: now,
    )
    return engine, scenario, synchronizer, visualizer


async def test_start_invokes_telemetry_source_start() -> None:
    source = _FakeTelemetrySource()
    engine, *_ = _build(source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500))
    await engine.start()
    assert source.start_calls == 1
    await engine.stop()
    assert source.stop_calls == 1


async def test_step_with_fresh_data_syncs_and_visualizes() -> None:
    tel = _make_telemetry(ts=999.9)  # 100ms 延迟，未超阈值
    source = _FakeTelemetrySource(initial=tel)
    engine, scenario, synchronizer, visualizer = _build(
        source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500), now=1000.0
    )
    await engine.start()
    await engine.step()
    assert scenario.step_calls == 1
    assert synchronizer.calls == [tel]
    assert len(visualizer.calls) == 1
    await engine.stop()


async def test_step_without_data_only_ticks_scenario() -> None:
    source = _FakeTelemetrySource(initial=None)
    engine, scenario, synchronizer, visualizer = _build(
        source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500)
    )
    await engine.start()
    await engine.step()
    assert scenario.step_calls == 1
    assert synchronizer.calls == []
    assert visualizer.calls == []
    await engine.stop()


async def test_stale_data_triggers_pause_once() -> None:
    # 数据延迟 800ms > pause_at=500
    tel = _make_telemetry(ts=999.2)
    source = _FakeTelemetrySource(initial=tel)
    engine, scenario, synchronizer, visualizer = _build(
        source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500), now=1000.0
    )
    await engine.start()
    await engine.step()
    await engine.step()
    assert scenario.step_calls == 2
    assert synchronizer.calls == []  # 暂停路径不做同步
    assert visualizer.calls == []
    # 只调用一次 pause（幂等）
    assert scenario.pause_calls == 1
    assert engine.is_paused is True
    await engine.stop()


async def test_pause_then_resume_on_fresh_data() -> None:
    stale = _make_telemetry(ts=999.2)
    source = _FakeTelemetrySource(initial=stale)
    engine, scenario, synchronizer, visualizer = _build(
        source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500), now=1000.0
    )
    await engine.start()
    await engine.step()
    assert engine.is_paused is True
    # 推送新鲜数据
    fresh = _make_telemetry(ts=999.99)
    source.set(fresh)
    await engine.step()
    assert engine.is_paused is False
    assert scenario.resume_calls == 1
    assert synchronizer.calls == [fresh]
    assert len(visualizer.calls) == 1
    await engine.stop()


async def test_run_stops_at_max_ticks() -> None:
    source = _FakeTelemetrySource()
    engine, scenario, _, _ = _build(source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500))
    await engine.start()
    ticks = await engine.run(max_ticks=5)
    assert ticks == 5
    assert scenario.step_calls == 5
    await engine.stop()


async def test_stop_is_idempotent() -> None:
    source = _FakeTelemetrySource()
    engine, *_ = _build(source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500))
    await engine.start()
    await engine.stop()
    await engine.stop()  # 第二次应不再调用 telemetry.stop
    assert source.stop_calls == 1


async def test_step_no_error_when_clock_before_timestamp() -> None:
    """时钟回退（负延迟）时应 clamp 到 0，不误判 pause。"""
    tel = _make_telemetry(ts=1000.5)
    source = _FakeTelemetrySource(initial=tel)
    engine, scenario, synchronizer, _ = _build(
        source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500), now=1000.0
    )
    await engine.start()
    await engine.step()
    assert scenario.step_calls == 1
    assert synchronizer.calls == [tel]
    await engine.stop()


async def test_tick_count_property_increments() -> None:
    source = _FakeTelemetrySource()
    engine, *_ = _build(source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500))
    await engine.start()
    assert engine.tick_count == 0
    for _ in range(3):
        await engine.step()
    assert engine.tick_count == 3
    await engine.stop()


async def test_run_returns_zero_when_not_started() -> None:
    source = _FakeTelemetrySource()
    engine, *_ = _build(source=source, sync_ctrl=_FakeSyncCtrl(pause_at=500))
    # 未 start：running=False，run 应立即返回 0
    ticks = await engine.run(max_ticks=10)
    assert ticks == 0
