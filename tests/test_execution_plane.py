"""执行平面测试（V2.4.0 执行面分离，V2.8.0 P2-14 端点级地图亲和）。

覆盖 ``ExecutionPlane`` 的引擎租约、地图加载、同步模式配置、
运行器装配/释放与失败回滚，以及端点地图事实登记与冲突判定；
CARLA world 全部使用桩对象。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from hunter_sim.execution.engine_pool import EngineEndpoint, EnginePool
from hunter_sim.execution.plane import (
    ExecutionPlane,
    ExecutionUnavailable,
    _world_map_id,
)


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


# ─── V2.6.0：引擎级锁拆分（P0-2） ───────────────────────────────────────────


class _ConcurrentProbeMapManager(_StubMapManager):
    """记录 load_map 并发进入数的 MapManager 桩。"""

    def __init__(self, gate: Any = None) -> None:
        super().__init__()
        self.gate = gate
        self.active = 0
        self.max_active = 0
        self.probe_lock = threading.Lock()

    def load_map(self, map_id: str) -> _StubWorld:
        with self.probe_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                self.gate.wait(timeout=5.0)
            return super().load_map(map_id)
        finally:
            with self.probe_lock:
                self.active -= 1


class TestEngineLocks:
    def test_engine_lock_is_reused_per_endpoint(self) -> None:
        plane, _ = _plane()
        first = plane._engine_lock("10.0.0.1:2000")
        assert plane._engine_lock("10.0.0.1:2000") is first
        assert plane._engine_lock("10.0.0.2:2000") is not first

    def test_different_endpoints_assemble_in_parallel(self) -> None:
        """多引擎装配不再被全局锁串行化（V2.6.0 P0-2）。"""
        manager_a = _ConcurrentProbeMapManager()
        manager_b = _ConcurrentProbeMapManager()
        endpoint_a = EngineEndpoint("10.0.0.1", 2000, 2001)
        endpoint_b = EngineEndpoint("10.0.0.2", 2000, 2001)
        managers = {endpoint_a.key: manager_a, endpoint_b.key: manager_b}
        plane = ExecutionPlane(
            EnginePool([endpoint_a, endpoint_b]),
            lambda endpoint: managers[endpoint.key],
            runner_factory=_StubRunner,
        )
        barrier = threading.Barrier(2, timeout=5.0)
        manager_a.gate = barrier
        manager_b.gate = barrier

        results: list[Any] = []

        def _assemble(instance: Any) -> None:
            results.append(plane.ensure_runner(instance))

        threads = [
            threading.Thread(target=_assemble, args=(_instance("inst-1", host="10.0.0.1"),)),
            threading.Thread(target=_assemble, args=(_instance("inst-2", host="10.0.0.2"),)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # barrier 双线程会合成功 → 两次 load 时间上重叠（全局锁下会超时 BrokenBarrier）
        assert not any(t.is_alive() for t in threads)
        assert len(results) == 2
        assert manager_a.max_active == 1 and manager_b.max_active == 1
        assert plane.active_engine_count == 2

    def test_external_holder_serializes_engine_operations(self) -> None:
        """同端点的 CARLA 操作与引擎锁持有者严格串行（单引擎退化语义）。"""
        plane, manager = _plane()
        key = "10.0.0.1:2000"
        engine_lock = plane._engine_lock(key)  # 线程启动前先建锁，避免建锁竞态
        done: list[str] = []

        def _assemble() -> None:
            plane.ensure_runner(_instance())
            done.append("assembled")

        t = threading.Thread(target=_assemble)
        with engine_lock:  # 外部持锁 → 装配必须等待
            t.start()
            time.sleep(0.15)
            assert not done
        t.join(timeout=10.0)
        assert done == ["assembled"]
        assert manager.loaded_maps == ["Town03"]

    def test_concurrent_same_instance_loads_once(self) -> None:
        """同实例并发 ensure：后到者在引擎锁内事后再查注册表，不重复 load。"""
        slow: list[str] = []

        def _slow_factory(world: Any, **kwargs: Any) -> _StubRunner:
            time.sleep(0.1)
            slow.append("built")
            return _StubRunner(world)

        plane = ExecutionPlane(
            EnginePool([EngineEndpoint("10.0.0.1", 2000, 2001)]),
            lambda endpoint: _StubMapManager(),
            runner_factory=_slow_factory,
        )
        instance = _instance()
        results: list[Any] = []

        def _assemble() -> None:
            results.append(plane.ensure_runner(instance))

        threads = [threading.Thread(target=_assemble) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(results) == 2
        assert results[0] is results[1]
        assert len(slow) == 1
        assert plane.runners["inst-1"] is results[0]

    def test_release_of_registered_runner_waits_engine_lock(self) -> None:
        """已注册运行器的销毁在引擎锁下执行，与在途 CARLA 操作串行。"""
        plane, _ = _plane()
        runner = plane.ensure_runner(_instance())
        key = "10.0.0.1:2000"
        engine_lock = plane._engine_lock(key)
        destroyed: list[bool] = []
        original_destroy = runner.destroy

        def _spy_destroy() -> None:
            destroyed.append(True)
            original_destroy()

        runner.destroy = _spy_destroy  # type: ignore[method-assign]

        with engine_lock:  # 外部持锁 → 销毁必须等待
            releaser = threading.Thread(target=plane.release, args=("inst-1",))
            releaser.start()
            time.sleep(0.15)
            assert releaser.is_alive()
            assert destroyed == []
        releaser.join(timeout=10.0)
        assert destroyed == [True]
        assert plane.get_runner("inst-1") is None
        assert plane.active_engine_count == 0


class _NamedWorld(_StubWorld):
    """带 ``get_map()`` 的世界桩（CARLA 返回全路径地图名）。"""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def get_map(self) -> Any:
        return SimpleNamespace(name=self._name)


class _NamedMapManager(_StubMapManager):
    """返回带地图名的 world，用于验证“以实际加载为准”的地图登记。"""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def load_map(self, map_id: str) -> _NamedWorld:
        self.loaded_maps.append(map_id)
        return _NamedWorld(self._name)


class TestWorldMapId:
    def test_strips_carla_map_path(self) -> None:
        world = _NamedWorld("/Game/Maps/Town05")
        assert _world_map_id(world, "Town03") == "Town05"

    def test_falls_back_when_map_unreadable(self) -> None:
        """无 get_map 的 Mock/受限世界不阻断装配，回退到请求地图。"""
        assert _world_map_id(_StubWorld(), "Town03") == "Town03"
        assert _world_map_id(_NamedWorld(""), "Town03") == "Town03"


class TestEndpointMapAwareness:
    def test_assemble_records_actual_loaded_map(self) -> None:
        """登记的是世界实际地图，而非申请地图（CARLA 回退默认图时不失真）。"""
        manager = _NamedMapManager("/Game/Maps/Town05")
        plane, _ = _plane(map_manager=manager)

        plane.ensure_runner(_instance("inst-1", map_id="Town03"))

        assert plane.pool.loaded_map_of("10.0.0.1:2000") == "Town05"

    def test_endpoint_key_of_tracks_instance_lease(self) -> None:
        plane, _ = _plane()
        assert plane.endpoint_key_of("inst-1") == ""

        plane.ensure_runner(_instance())
        assert plane.endpoint_key_of("inst-1") == "10.0.0.1:2000"

        plane.release("inst-1")
        assert plane.endpoint_key_of("inst-1") == ""

    def test_conflict_only_when_other_instance_holds_different_map(self) -> None:
        plane, _ = _plane()
        plane.ensure_runner(_instance("inst-1", map_id="Town03"))

        assert plane.map_conflict("10.0.0.1:2000", "inst-2", "Town04") == ("inst-1", "Town03")
        # 同地图不冲突（这正是 P2-14 要的不切图复用）
        assert plane.map_conflict("10.0.0.1:2000", "inst-2", "Town03") == ("", "")
        # 自己占用自己的端点不构成冲突
        assert plane.map_conflict("10.0.0.1:2000", "inst-1", "Town04") == ("", "")

    def test_free_endpoint_never_conflicts(self) -> None:
        plane, _ = _plane()
        plane.ensure_runner(_instance("inst-1", map_id="Town03"))
        plane.release("inst-1")

        assert plane.map_conflict("10.0.0.1:2000", "inst-2", "Town04") == ("", "")
        assert plane.map_conflict("", "inst-2", "Town04") == ("", "")
