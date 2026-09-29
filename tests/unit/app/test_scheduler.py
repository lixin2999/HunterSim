"""模块 5.1 采集调度器的单元测试（全部协作者 mock）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter_sim.app.scheduler import CollectionSchedulerImpl
from hunter_sim.core.exceptions import ConversionError
from hunter_sim.simulation.protocols import ScenarioState


class FakeScenario:
    """可控状态与步进次数的假场景。"""

    def __init__(self, *, stop_after: int | None = None) -> None:
        self.state = ScenarioState.RUNNING
        self.step_count = 0
        self._stop_after = stop_after

    async def step(self) -> object:
        self.step_count += 1
        if self._stop_after is not None and self.step_count >= self._stop_after:
            self.state = ScenarioState.COMPLETED
        return object()


class FakeBuffer:
    """每 tick 返回固定原始测量的假缓冲。"""

    def __init__(self, items: list[object]) -> None:
        self._items = items

    def drain_nowait(self, max_items: int | None = None) -> list[object]:
        return list(self._items)


def _make_registry(buffers: dict[str, FakeBuffer]) -> MagicMock:
    registry = MagicMock()
    registry.sensor_ids.return_value = list(buffers.keys())
    registry.get.side_effect = lambda sid: buffers.get(sid)
    return registry


def _make_scheduler(
    *,
    scenario: FakeScenario,
    buffers: dict[str, FakeBuffer],
    sensor_types: dict[str, str],
    convert_side: object = None,
) -> CollectionSchedulerImpl:
    converter = MagicMock()
    if isinstance(convert_side, BaseException):
        converter.convert.side_effect = convert_side
    else:
        converter.convert.return_value = MagicMock()
    cleaner = MagicMock()
    cleaner.clean.side_effect = lambda frame: frame
    writer = MagicMock()
    writer.write = AsyncMock()
    scheduler = CollectionSchedulerImpl(
        scenario=scenario,
        registry=_make_registry(buffers),
        converter=converter,
        cleaner=cleaner,
        writer=writer,
        sensor_types=sensor_types,
    )
    return scheduler


async def test_step_once_processes_all_drained_items() -> None:
    scenario = FakeScenario()
    buffers = {"cam0": FakeBuffer(["raw1", "raw2"]), "lidar0": FakeBuffer(["raw3"])}
    scheduler = _make_scheduler(
        scenario=scenario,
        buffers=buffers,
        sensor_types={"cam0": "camera.rgb", "lidar0": "lidar.ray_cast"},
    )

    written = await scheduler.step_once()

    assert written == 3
    assert scheduler.ticks == 1
    assert scheduler.sensor_counts == {"cam0": 2, "lidar0": 1}
    assert scheduler.written_frames == 3


async def test_conversion_failure_is_skipped() -> None:
    scenario = FakeScenario()
    buffers = {"cam0": FakeBuffer(["raw1", "raw2"])}
    # 转换对每次调用均失败，本帧应零写入但仍计数一次 tick
    scheduler = _make_scheduler(
        scenario=scenario,
        buffers=buffers,
        sensor_types={"cam0": "camera.rgb"},
        convert_side=ConversionError("坏数据"),
    )

    written = await scheduler.step_once()

    assert written == 0
    assert scheduler.ticks == 1
    assert scheduler.written_frames == 0


async def test_sensor_without_type_mapping_is_ignored() -> None:
    scenario = FakeScenario()
    buffers = {"cam0": FakeBuffer(["raw1"]), "unknown": FakeBuffer(["raw2"])}
    scheduler = _make_scheduler(
        scenario=scenario, buffers=buffers, sensor_types={"cam0": "camera.rgb"}
    )

    written = await scheduler.step_once()

    assert written == 1
    assert scheduler.sensor_counts == {"cam0": 1}


async def test_run_stops_at_max_ticks() -> None:
    scenario = FakeScenario()
    buffers = {"cam0": FakeBuffer(["raw1"])}
    scheduler = _make_scheduler(
        scenario=scenario, buffers=buffers, sensor_types={"cam0": "camera.rgb"}
    )

    ticks = await scheduler.run(max_ticks=5)

    assert ticks == 5
    assert scenario.step_count == 5


async def test_run_breaks_when_scenario_not_running() -> None:
    scenario = FakeScenario(stop_after=3)
    buffers = {"cam0": FakeBuffer(["raw1"])}
    scheduler = _make_scheduler(
        scenario=scenario, buffers=buffers, sensor_types={"cam0": "camera.rgb"}
    )

    ticks = await scheduler.run(max_ticks=100)

    assert ticks == 3
    assert scenario.state is ScenarioState.COMPLETED


async def test_run_immediate_break_when_idle() -> None:
    scenario = FakeScenario()
    scenario.state = ScenarioState.IDLE
    scheduler = _make_scheduler(scenario=scenario, buffers={}, sensor_types={})

    ticks = await scheduler.run(max_ticks=10)

    assert ticks == 0
    assert scenario.step_count == 0


def test_sensor_counts_returns_copy() -> None:
    scenario = FakeScenario()
    scheduler = _make_scheduler(scenario=scenario, buffers={}, sensor_types={})
    counts = scheduler.sensor_counts
    counts["injected"] = 99
    assert "injected" not in scheduler.sensor_counts


@pytest.mark.parametrize("n", [1, 2, 3])
async def test_step_once_increments_ticks(n: int) -> None:
    scenario = FakeScenario()
    buffers = {"cam0": FakeBuffer(["raw"])}
    scheduler = _make_scheduler(
        scenario=scenario, buffers=buffers, sensor_types={"cam0": "camera.rgb"}
    )
    for _ in range(n):
        await scheduler.step_once()
    assert scheduler.ticks == n
