"""模块 5.2 运行编排器的单元测试（全部协作者 mock，验证生命周期与聚合）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter_sim.app.orchestrator import RunOrchestratorImpl
from hunter_sim.core.config import ScenarioConfig
from hunter_sim.core.events import CollisionEvent, Event, LaneInvasionEvent
from hunter_sim.evaluation.models import EvaluationReport
from hunter_sim.simulation.protocols import ScenarioState


def make_config() -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "scenario": {
                "name": "demo",
                "map": "Town01",
                "duration_seconds": 2.0,
                "tick_rate": 10.0,
            },
            "vehicle": {"blueprint": "vehicle.tesla.model3"},
            "sensors": [
                {
                    "id": "cam_front",
                    "type": "camera.rgb",
                    "position": [0.0, 0.0, 1.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "tick_rate": 10.0,
                    "width": 8,
                    "height": 8,
                    "fov": 90.0,
                }
            ],
            "output": {"base_dir": "./data/runs"},
        }
    )


class FakeEventBus:
    """记录订阅回调并可手动触发的假事件总线。"""

    def __init__(self) -> None:
        self.callbacks: dict[type[Event], list] = {}
        self.unsubscribed = 0

    def subscribe(self, event_type: type[Event], callback) -> object:
        self.callbacks.setdefault(event_type, []).append(callback)

        def _unsubscribe() -> None:
            self.unsubscribed += 1

        return _unsubscribe

    def emit(self, event_type: type[Event]) -> None:
        # 编排器的回调仅计数、忽略事件负载，传None即可
        for cb in self.callbacks.get(event_type, []):
            cb(None)  # type: ignore[arg-type]


def _make_orchestrator(**overrides: object) -> tuple[RunOrchestratorImpl, dict[str, object]]:
    config = make_config()
    connection = MagicMock()
    connection.connect = AsyncMock()
    connection.disconnect = AsyncMock()
    connection.get_world.return_value = MagicMock(name="world")

    scenario = MagicMock()
    scenario.state = ScenarioState.RUNNING
    scenario.configure = AsyncMock()
    scenario.start = AsyncMock()
    scenario.stop = AsyncMock()

    vehicle = MagicMock()
    vehicle.get_actor.return_value = MagicMock(name="ego_actor")

    sensor_manager = MagicMock()
    sensor_manager.initialize = AsyncMock()
    sensor_manager.start_all = AsyncMock()
    sensor_manager.stop_all = AsyncMock()
    sensor_manager.destroy_all = AsyncMock()

    registry = MagicMock()
    registry.close_all = AsyncMock()

    scheduler = MagicMock()
    scheduler.ticks = 20
    scheduler.written_frames = 40
    scheduler.sensor_counts = {"cam_front": 40}
    scheduler.run = AsyncMock(return_value=20)

    writer = MagicMock()
    writer.write_metadata = AsyncMock()
    writer.close = AsyncMock()

    bus = FakeEventBus()
    metrics = MagicMock()
    metrics.safety.return_value = MagicMock(name="safety")
    metrics.coverage.return_value = MagicMock(name="coverage")

    reporter = MagicMock()
    reporter.build_report.return_value = EvaluationReport(run_id="run-1", scenario_name="demo")

    deps = {
        "config": config,
        "connection": connection,
        "scenario": scenario,
        "vehicle": vehicle,
        "sensor_manager": sensor_manager,
        "registry": registry,
        "scheduler": scheduler,
        "writer": writer,
        "event_bus": bus,
        "metrics": metrics,
        "reporter": reporter,
    }
    for key, value in overrides.items():
        deps[key] = value

    orchestrator = RunOrchestratorImpl(run_id="run-1", **deps)  # type: ignore[arg-type]
    return orchestrator, deps


async def test_run_success_orchestration_and_result() -> None:
    orchestrator, deps = _make_orchestrator()

    result = await orchestrator.run()

    connection = deps["connection"]
    scenario = deps["scenario"]
    sensor_manager = deps["sensor_manager"]
    scheduler = deps["scheduler"]
    assert connection.connect.await_count == 1  # type: ignore[attr-defined]
    scenario.configure.assert_awaited_once()  # type: ignore[attr-defined]
    scenario.start.assert_awaited_once()  # type: ignore[attr-defined]
    sensor_manager.initialize.assert_awaited_once()  # type: ignore[attr-defined]
    sensor_manager.start_all.assert_awaited_once()  # type: ignore[attr-defined]
    # max_ticks=None 时按 duration_seconds*tick_rate = 2*10 = 20
    scheduler.run.assert_awaited_once_with(max_ticks=20)  # type: ignore[attr-defined]
    sensor_manager.stop_all.assert_awaited_once()  # type: ignore[attr-defined]
    scenario.stop.assert_awaited_once()  # type: ignore[attr-defined]

    assert result.run_id == "run-1"
    assert result.scenario_name == "demo"
    assert result.ticks == 20
    assert result.written_frames == 40
    assert result.metadata.status == "completed"
    assert result.metadata.total_frames == 40
    assert result.report is deps["reporter"].build_report.return_value  # type: ignore[attr-defined]


async def test_run_attaches_sensors_to_ego_actor_and_world() -> None:
    orchestrator, deps = _make_orchestrator()

    await orchestrator.run()

    sensor_manager = deps["sensor_manager"]
    kwargs = sensor_manager.initialize.await_args.kwargs  # type: ignore[attr-defined]
    assert kwargs["vehicle"] is deps["vehicle"].get_actor.return_value  # type: ignore[attr-defined]
    assert kwargs["world"] is deps["connection"].get_world.return_value  # type: ignore[attr-defined]


async def test_cleanup_always_runs() -> None:
    orchestrator, deps = _make_orchestrator()

    await orchestrator.run()

    deps["sensor_manager"].destroy_all.assert_awaited_once()  # type: ignore[attr-defined]
    deps["registry"].close_all.assert_awaited_once()  # type: ignore[attr-defined]
    deps["writer"].close.assert_awaited_once()  # type: ignore[attr-defined]
    deps["connection"].disconnect.assert_awaited_once()  # type: ignore[attr-defined]
    # 两个订阅均已退订
    assert deps["event_bus"].unsubscribed == 2  # type: ignore[attr-defined]


async def test_max_ticks_override_is_honored() -> None:
    orchestrator, deps = _make_orchestrator()

    await orchestrator.run(max_ticks=7)

    deps["scheduler"].run.assert_awaited_once_with(max_ticks=7)  # type: ignore[attr-defined]


async def test_metrics_receive_event_counts() -> None:
    bus = FakeEventBus()

    async def _emit_during_run(*, max_ticks: int | None = None) -> int:
        # 在调度器运行期间模拟事件：2 次碰撞、1 次压线
        bus.emit(CollisionEvent)
        bus.emit(CollisionEvent)
        bus.emit(LaneInvasionEvent)
        return 20

    scheduler = MagicMock()
    scheduler.ticks = 20
    scheduler.written_frames = 40
    scheduler.sensor_counts = {"cam_front": 40}
    scheduler.run = AsyncMock(side_effect=_emit_during_run)
    orchestrator, deps = _make_orchestrator(event_bus=bus, scheduler=scheduler)

    await orchestrator.run()

    metrics = deps["metrics"]
    metrics.safety.assert_called_once_with(  # type: ignore[attr-defined]
        collision_count=2, lane_invasion_count=1, total_ticks=20, tick_rate=10.0
    )
    metrics.coverage.assert_called_once_with(  # type: ignore[attr-defined]
        total_ticks=20, per_sensor_counts={"cam_front": 40}
    )


async def test_failure_finalizes_metadata_and_reraises() -> None:
    orchestrator, deps = _make_orchestrator()
    scheduler = deps["scheduler"]
    scheduler.run = AsyncMock(side_effect=RuntimeError("boom"))
    orchestrator, deps = _make_orchestrator(scheduler=scheduler)

    with pytest.raises(RuntimeError, match="boom"):
        await orchestrator.run()

    # 失败仍执行清理，但未构建报告/写元数据
    deps["sensor_manager"].destroy_all.assert_awaited_once()  # type: ignore[attr-defined]
    deps["connection"].disconnect.assert_awaited_once()  # type: ignore[attr-defined]
    deps["writer"].write_metadata.assert_not_awaited()  # type: ignore[attr-defined]
    deps["reporter"].build_report.assert_not_called()  # type: ignore[attr-defined]


def test_event_imports_present() -> None:
    # 确保编排器订阅使用的具体事件类型可导入且为 Event 子类
    assert issubclass(CollisionEvent, Event)
    assert issubclass(LaneInvasionEvent, Event)
