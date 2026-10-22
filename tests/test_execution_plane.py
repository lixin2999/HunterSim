"""执行平面测试（V2.4.0 执行面分离）。

覆盖 ``ExecutionPlane`` 的引擎租约、地图加载、同步模式配置、
运行器装配/释放与失败回滚；CARLA world 全部使用桩对象。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hunter_sim.execution.engine_pool import EngineEndpoint, EnginePool
from hunter_sim.execution.plane import ExecutionPlane, ExecutionUnavailable


class _StubWorld:
    """最小 CARLA World 桩：记录同步模式设置。"""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(synchronous_mode=False, fixed_delta_seconds=0.0)
        self.applied: list[Any] = []

    def get_settings(self) -> Any:
        return self.settings

    def apply_settings(self, settings: Any) -> None:
        self.applied.append(settings)


class _StubMapManager:
    """MapManager 桩：记录地图加载并返回 World。"""

    def __init__(self, fail: bool = False) -> None:
        self.loaded_maps: list[str] = []
        self.fail = fail

    def load_map(self, map_id: str) -> _StubWorld:
        if self.fail:
            raise RuntimeError("load_world failed")
        self.loaded_maps.append(map_id)
        return _StubWorld()


class _StubRunner:
    """运行器桩：记录销毁状态与回调。"""

    def __init__(self, world: Any, on_state_change: Any = None, on_event: Any = None) -> None:
        self.world = world
        self.on_state_change = on_state_change
        self.on_event = on_event
        self.destroyed = False
        self.loaded_configs: list[Any] = []

    def destroy(self) -> None:
        self.destroyed = True

    @property
    def has_scene(self) -> bool:
        return bool(self.loaded_configs)

    def load_scene(self, config: Any) -> None:
        self.loaded_configs.append(config)


def _instance(
    instance_id: str = "inst-1",
    map_id: str = "Town03",
    host: str = "10.0.0.1",
    rpc: int = 2000,
) -> SimpleNamespace:
    return SimpleNamespace(
        sim_instance_id=instance_id,
        map_id=map_id,
        carla_host=host,
        carla_rpc_port=rpc,
        carla_stream_port=rpc + 1,
    )


def _plane(
    endpoints: list[EngineEndpoint] | None = None,
    map_manager: _StubMapManager | None = None,
) -> tuple[ExecutionPlane, _StubMapManager]:
    manager = map_manager or _StubMapManager()
    endpoints = endpoints or [EngineEndpoint("10.0.0.1", 2000, 2001)]
    return (
        ExecutionPlane(
            EnginePool(endpoints),
            lambda endpoint: manager,
            runner_factory=_StubRunner,
        ),
        manager,
    )


class TestEnsureRunner:
    def test_assembles_runner_with_sync_world(self) -> None:
        plane, manager = _plane()
        instance = _instance()

        runner = plane.ensure_runner(instance)

        assert isinstance(runner, _StubRunner)
        assert manager.loaded_maps == ["Town03"]
        assert runner.world.settings.synchronous_mode is True
        assert runner.world.settings.fixed_delta_seconds == pytest.approx(0.02)
        assert runner.world.applied
        assert plane.get_runner("inst-1") is runner
        assert plane.active_engine_count == 1

    def test_reuses_existing_runner(self) -> None:
        plane, manager = _plane()
        instance = _instance()

        first = plane.ensure_runner(instance)
        second = plane.ensure_runner(instance)

        assert first is second
        assert manager.loaded_maps == ["Town03"]

    def test_callbacks_forwarded_to_runner(self) -> None:
        plane, _ = _plane()
        changes: list[Any] = []
        events: list[Any] = []

        def _on_change(snapshot: Any) -> None:
            changes.append(snapshot)

        def _on_event(event: Any) -> None:
            events.append(event)

        runner = plane.ensure_runner(
            _instance(), on_state_change=_on_change, on_event=_on_event
        )

        assert runner.on_state_change is _on_change
        assert runner.on_event is _on_event

    def test_engine_occupied_by_other_instance(self) -> None:
        plane, _ = _plane()
        first = plane.ensure_runner(_instance("inst-1"))

        with pytest.raises(ExecutionUnavailable, match="inst-1"):
            plane.ensure_runner(_instance("inst-2"))

        assert plane.get_runner("inst-1") is first
        assert plane.active_engine_count == 1

    def test_missing_map_manager_releases_lease(self) -> None:
        plane = ExecutionPlane(
            EnginePool([EngineEndpoint("10.0.0.1", 2000, 2001)]),
            lambda endpoint: None,
            runner_factory=_StubRunner,
        )

        with pytest.raises(ExecutionUnavailable, match="No CARLA client"):
            plane.ensure_runner(_instance())

        assert plane.active_engine_count == 0
        assert plane.get_runner("inst-1") is None

    def test_load_map_failure_releases_lease(self) -> None:
        plane, _ = _plane(map_manager=_StubMapManager(fail=True))

        with pytest.raises(ExecutionUnavailable, match="Failed to assemble"):
            plane.ensure_runner(_instance())

        assert plane.active_engine_count == 0
        assert plane.get_runner("inst-1") is None

    def test_second_engine_allows_parallel_scenes(self) -> None:
        plane, _ = _plane(
            [
                EngineEndpoint("10.0.0.1", 2000, 2001),
                EngineEndpoint("10.0.0.2", 2000, 2001),
            ]
        )

        first = plane.ensure_runner(_instance("inst-1", host="10.0.0.1"))
        second = plane.ensure_runner(_instance("inst-2", map_id="Town04", host="10.0.0.2"))

        assert first is not second
        assert plane.active_engine_count == 2
        engines = {item["instance_id"]: item["engine"] for item in plane.snapshot()["runners"]}
        assert engines["inst-1"] == "10.0.0.1:2000"
        assert engines["inst-2"] == "10.0.0.2:2000"


class TestRelease:
    def test_release_destroys_runner_and_frees_engine(self) -> None:
        plane, _ = _plane()
        runner = plane.ensure_runner(_instance())

        assert plane.release("inst-1") is True

        assert runner.destroyed is True
        assert plane.get_runner("inst-1") is None
        assert plane.active_engine_count == 0

    def test_release_unknown_returns_false(self) -> None:
        plane, _ = _plane()
        assert plane.release("ghost") is False

    def test_release_all(self) -> None:
        plane, _ = _plane(
            [
                EngineEndpoint("10.0.0.1", 2000, 2001),
                EngineEndpoint("10.0.0.2", 2000, 2001),
            ]
        )
        first = plane.ensure_runner(_instance("inst-1", host="10.0.0.1"))
        second = plane.ensure_runner(_instance("inst-2", host="10.0.0.2"))

        assert plane.release_all() == 2

        assert first.destroyed is True
        assert second.destroyed is True
        assert plane.runners == {}
        assert plane.active_engine_count == 0

    def test_destroy_failure_does_not_block_release(self) -> None:
        plane, _ = _plane()
        runner = plane.ensure_runner(_instance())

        def _boom() -> None:
            raise RuntimeError("destroy failed")

        runner.destroy = _boom  # type: ignore[method-assign]

        assert plane.release("inst-1") is True
        assert plane.active_engine_count == 0


class TestEndpointResolution:
    def test_defaults_when_instance_lacks_carla_fields(self) -> None:
        endpoint = ExecutionPlane._endpoint_of(SimpleNamespace(sim_instance_id="inst-x"))
        assert endpoint.key == "127.0.0.1:2000"
        assert endpoint.stream_port == 2001

    def test_uses_instance_fields(self) -> None:
        endpoint = ExecutionPlane._endpoint_of(
            SimpleNamespace(
                sim_instance_id="inst-x",
                carla_host="10.0.0.9",
                carla_rpc_port=2100,
                carla_stream_port=2101,
                carla_tm_port=9000,
            )
        )
        assert endpoint.key == "10.0.0.9:2100"
        assert endpoint.tm_port == 9000


class TestSnapshot:
    def test_snapshot_without_leases(self) -> None:
        plane, _ = _plane()
        snapshot = plane.snapshot()

        assert snapshot["capacity"] == 1
        assert snapshot["active_leases"] == 0
        assert snapshot["runners"] == []
        assert snapshot["engines"][0]["holder"] is None

    def test_snapshot_after_assembly(self) -> None:
        plane, _ = _plane()
        plane.ensure_runner(_instance())

        snapshot = plane.snapshot()
        assert snapshot["active_leases"] == 1
        assert snapshot["engines"][0]["holder"] == "inst-1"
        assert snapshot["engines"][0]["map_id"] == "Town03"
        assert snapshot["runners"] == [{"instance_id": "inst-1", "engine": "10.0.0.1:2000"}]
