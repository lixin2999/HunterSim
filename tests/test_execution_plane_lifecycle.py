"""ExecutionPlane 装配 / 释放生命周期单测（V3.0.0 执行面收敛后补齐）。

覆盖：引擎租约领取失败与地图装配失败的统一降级、装配窗口内取消、
运行器注册表与引擎锁、心跳续租 / 僵尸判定、地图冲突检查、
producer 生命周期（按 sim_id 复用与释放）、执行面快照。
不依赖 CARLA：world / map_manager / 运行器全部使用替身。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest

from hunter_sim.execution.engine_pool import EngineEndpoint, EnginePool
from hunter_sim.execution.plane import ExecutionPlane, ExecutionUnavailable, _world_map_id


# ─── CARLA 替身 ──────────────────────────────────────────────────────────────


class _WorldSettings:
    def __init__(self) -> None:
        self.synchronous_mode = False
        self.fixed_delta_seconds = 0.0


class _World:
    """CARLA world 替身：记录同步模式设置。"""

    def __init__(
        self,
        name: str = "/Game/Maps/Town01",
        *,
        settings_error: bool = False,
        settings_none: bool = False,
        apply_error: bool = False,
    ) -> None:
        self.name = name
        self.settings = _WorldSettings()
        self.applied = 0
        self._settings_error = settings_error
        self._settings_none = settings_none
        self._apply_error = apply_error

    def get_map(self) -> SimpleNamespace:
        return SimpleNamespace(name=self.name)

    def get_settings(self) -> Optional[_WorldSettings]:
        if self._settings_error:
            raise RuntimeError("settings unavailable")
        return None if self._settings_none else self.settings

    def apply_settings(self, _settings: _WorldSettings) -> None:
        if self._apply_error:
            raise RuntimeError("apply rejected")
        self.applied += 1


class _MapManager:
    """MapManager 替身：按需返回 world 或抛错。"""

    def __init__(self, world: Optional[_World] = None, error: Optional[Exception] = None) -> None:
        self.world = world or _World()
        self.error = error
        self.loaded: list[str] = []

    def load_map(self, map_id: str) -> _World:
        if self.error is not None:
            raise self.error
        self.loaded.append(map_id)
        return self.world


class _Runner:
    """SceneRunnerService 替身。"""

    def __init__(self, world: Any, **kwargs: Any) -> None:
        self.world = world
        self.kwargs = kwargs
        self.destroyed = 0
        self.is_running = True
        self.destroy_error: Optional[Exception] = None

    def destroy(self) -> None:
        self.destroyed += 1
        if self.destroy_error is not None:
            raise self.destroy_error


class _ExplodingBool:
    """``bool()`` 抛错的哨兵（覆盖 heartbeat 防御分支）。"""

    def __bool__(self) -> bool:
        raise RuntimeError("boom")


class _WeirdRunner:
    """``is_running`` 不可转 bool 的运行器替身。"""

    def __init__(self) -> None:
        self.destroyed = 0

    @property
    def is_running(self) -> Any:
        return _ExplodingBool()

    def destroy(self) -> None:
        self.destroyed += 1


class _FakeProducer:
    def __init__(self, settings: Any = None, sim_id: str = "") -> None:
        self.settings = settings
        self.sim_id = sim_id
        self.connected = 0
        self.closed = 0
        self.close_error: Optional[Exception] = None

    def connect(self) -> bool:
        self.connected += 1
        return True

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class _FailingPool(EnginePool):
    """acquire 恒失败的引擎池（端点被占用场景）。"""

    def acquire(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("engine busy")


# ─── 工厂脚手架 ──────────────────────────────────────────────────────────────


def _instance(instance_id: str = "inst-1", map_id: str = "/Game/Maps/Town01", **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "sim_instance_id": instance_id,
        "map_id": map_id,
        "sim_id": "sim-1",
        "vehicle_id": "veh-1",
        "scene_id": "scene-1",
        "carla_host": "127.0.0.1",
        "carla_rpc_port": 2000,
        "carla_stream_port": 2001,
        "carla_tm_port": 8000,
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _plane(
    *,
    pool: Optional[EnginePool] = None,
    world: Optional[_World] = None,
    manager: Optional[_MapManager] = None,
    runners: Optional[list[_Runner]] = None,
    cloud_settings: Any = None,
) -> ExecutionPlane:
    created = runners if runners is not None else []
    target = manager or _MapManager(world)

    def factory(world_obj: Any, **kwargs: Any) -> _Runner:
        runner = _Runner(world_obj, **kwargs)
        created.append(runner)
        return runner

    return ExecutionPlane(
        pool if pool is not None else EnginePool(),
        lambda _endpoint: target,
        runner_factory=factory,
        cloud_settings=cloud_settings,
    )


# ─── 装配 ────────────────────────────────────────────────────────────────────


class TestEnsureRunner:
    def test_assembles_and_switches_world_to_sync_mode(self) -> None:
        world = _World()
        runners: list[_Runner] = []
        plane = _plane(world=world, runners=runners)
        instance = _instance()

        runner = plane.ensure_runner(instance)

        assert runner is runners[0]
        assert world.settings.synchronous_mode is True
        assert world.settings.fixed_delta_seconds == pytest.approx(0.02)
        assert world.applied == 1
        # 以事实加载的地图登记到引擎池（P2-14）
        assert plane.pool.loaded_map_of("127.0.0.1:2000") == "Town01"
        assert plane.get_runner("inst-1") is runner
        assert plane.active_engine_count == 1

    def test_returns_existing_runner_without_reassembling(self) -> None:
        runners: list[_Runner] = []
        plane = _plane(runners=runners)
        instance = _instance()

        first = plane.ensure_runner(instance)
        second = plane.ensure_runner(instance)

        assert second is first
        assert len(runners) == 1

    def test_passes_platform_and_hook_kwargs(self) -> None:
        runners: list[_Runner] = []
        plane = _plane(runners=runners)

        plane.ensure_runner(
            _instance(),
            on_state_change=lambda *_a: None,
            on_event=lambda *_a: None,
            runner_hooks={"pose_driver": "vil", "sample_sink": object()},
        )

        kwargs = runners[0].kwargs
        assert kwargs["sim_id"] == "sim-1"
        assert kwargs["vehicle_id"] == "veh-1"
        assert kwargs["scene_id"] == "scene-1"
        assert kwargs["pose_driver"] == "vil"
        assert callable(kwargs["on_state_change"])

    def test_lease_failure_maps_to_execution_unavailable(self) -> None:
        plane = _plane(pool=_FailingPool())

        with pytest.raises(ExecutionUnavailable, match="engine busy"):
            plane.ensure_runner(_instance())

        assert plane.get_runner("inst-1") is None

    def test_missing_map_manager_releases_lease(self) -> None:
        plane = ExecutionPlane(
            EnginePool(), lambda _endpoint: None, runner_factory=lambda *_a, **_k: _Runner(None)
        )

        with pytest.raises(ExecutionUnavailable, match="No CARLA client"):
            plane.ensure_runner(_instance())

        assert plane.pool.get("inst-1") is None
        assert plane.active_engine_count == 0

    def test_load_map_failure_is_wrapped_and_released(self) -> None:
        manager = _MapManager(error=RuntimeError("map missing"))
        plane = _plane(manager=manager)

        with pytest.raises(ExecutionUnavailable, match="Failed to assemble scene runtime"):
            plane.ensure_runner(_instance())

        assert plane.pool.get("inst-1") is None
        assert plane.pool.active_count == 0  # noqa: SLF001  内部字典为空即无残留租约

    def test_release_during_assembly_discards_runner(self) -> None:
        runner = _Runner(None)
        plane = _plane()
        instance = _instance()

        def factory(_world: Any, **_kwargs: Any) -> _Runner:
            plane.release(instance.sim_instance_id)  # 装配窗口内被销毁
            return runner

        plane._runner_factory = factory  # noqa: SLF001  注入装配中取消

        with pytest.raises(ExecutionUnavailable, match="cancelled"):
            plane.ensure_runner(instance)

        assert runner.destroyed == 1
        assert plane.get_runner("inst-1") is None
        assert plane.pool.get("inst-1") is None

    def test_cancelled_destroy_failure_is_swallowed(self) -> None:
        runner = _Runner(None)
        runner.destroy_error = RuntimeError("destroy failed")
        plane = _plane()

        def factory(_world: Any, **_kwargs: Any) -> _Runner:
            plane.release("inst-1")
            return runner

        plane._runner_factory = factory  # noqa: SLF001

        with pytest.raises(ExecutionUnavailable, match="cancelled"):
            plane.ensure_runner(_instance())

        assert runner.destroyed == 1


# ─── 释放 ────────────────────────────────────────────────────────────────────


class TestRelease:
    def test_release_destroys_runner(self) -> None:
        runners: list[_Runner] = []
        plane = _plane(runners=runners)
        plane.ensure_runner(_instance())

        assert plane.release("inst-1") is True

        assert runners[0].destroyed == 1
        assert plane.get_runner("inst-1") is None
        assert plane.pool.get("inst-1") is None
        assert plane.release("inst-1") is False

    def test_release_swallows_destroy_failure(self) -> None:
        runners: list[_Runner] = []
        plane = _plane(runners=runners)
        plane.ensure_runner(_instance())
        runners[0].destroy_error = RuntimeError("boom")

        assert plane.release("inst-1") is True

    def test_release_all_counts_instances(self) -> None:
        plane = _plane(pool=EnginePool([EngineEndpoint("127.0.0.1", 2000, 2001)]))
        plane.ensure_runner(_instance("inst-1"))
        plane.ensure_runner(_instance("inst-2", carla_rpc_port=3000, carla_stream_port=3001))

        assert plane.release_all() == 2
        assert plane.release_all() == 0

    def test_force_release_returns_pool_state(self) -> None:
        plane = _plane()
        plane.pool.acquire("inst-x", EngineEndpoint("127.0.0.1", 2000, 2001), "m")

        assert plane.force_release("inst-x") is True
        assert plane.force_release("inst-x") is False


# ─── 心跳 / 租约查询 ─────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_without_runner_is_false(self) -> None:
        assert _plane().heartbeat("missing") is False

    def test_heartbeat_renews_live_runner(self) -> None:
        runners: list[_Runner] = []
        plane = _plane(runners=runners)
        plane.ensure_runner(_instance())

        assert plane.heartbeat("inst-1") is True
        assert plane.renew_live_leases() == 1

    def test_heartbeat_skips_dead_runner(self) -> None:
        runners: list[_Runner] = []
        plane = _plane(runners=runners)
        plane.ensure_runner(_instance())
        runners[0].is_running = False

        assert plane.heartbeat("inst-1") is False
        assert plane.renew_live_leases() == 0

    def test_heartbeat_treats_unbooleanable_flag_as_alive(self) -> None:
        plane = _plane()
        plane.runners["inst-1"] = _WeirdRunner()
        plane.pool.acquire("inst-1", EngineEndpoint("127.0.0.1", 2000, 2001), "m")

        assert plane.heartbeat("inst-1") is True

    def test_lease_queries(self) -> None:
        plane = _plane()
        plane.ensure_runner(_instance())

        assert plane.lease_of("inst-1") is not None
        assert plane.is_stale_lease("inst-1") is False
        assert plane.stale_instance_ids() == []
        assert plane.endpoint_key_of("inst-1") == "127.0.0.1:2000"

    def test_endpoint_key_of_falls_back_to_pool(self) -> None:
        plane = _plane()
        plane.pool.acquire("inst-1", EngineEndpoint("127.0.0.1", 2000, 2001), "m")

        assert plane.endpoint_key_of("inst-1") == "127.0.0.1:2000"
        assert plane.endpoint_key_of("missing") == ""

    def test_stale_instance_ids_reports_zombies(self) -> None:
        plane = _plane()
        plane.ensure_runner(_instance())
        lease = plane.lease_of("inst-1")
        assert lease is not None
        lease.renew(now=1000.0)
        plane.pool._ttl_seconds = 5.0  # noqa: SLF001  开启僵判定

        assert plane.is_stale_lease("inst-1") is True
        assert plane.stale_instance_ids() == ["inst-1"]


class TestMapConflict:
    def test_empty_endpoint_key_has_no_conflict(self) -> None:
        assert _plane().map_conflict("", "inst-1", "/Game/Maps/Town01") == ("", "")

    def test_no_holder_has_no_conflict(self) -> None:
        assert _plane().map_conflict("127.0.0.1:2000", "inst-1", "/Game/Maps/Town01") == ("", "")

    def test_same_holder_has_no_conflict(self) -> None:
        plane = _plane()
        plane.ensure_runner(_instance())

        assert plane.map_conflict("127.0.0.1:2000", "inst-1", "/Game/Maps/Town02") == ("", "")

    def test_other_holder_with_different_map_conflicts(self) -> None:
        plane = _plane()
        plane.ensure_runner(_instance("inst-1", "/Game/Maps/Town01"))

        holder, loaded = plane.map_conflict("127.0.0.1:2000", "inst-2", "/Game/Maps/Town02")

        assert holder == "inst-1"
        assert loaded == "Town01"

    def test_other_holder_with_same_map_is_fine(self) -> None:
        plane = _plane()
        plane.ensure_runner(_instance("inst-1", "/Game/Maps/Town01"))
        plane.pool.note_loaded_map("127.0.0.1:2000", "/Game/Maps/Town02")

        assert plane.map_conflict("127.0.0.1:2000", "inst-2", "/Game/Maps/Town02") == ("", "")


# ─── Producer 生命周期 ───────────────────────────────────────────────────────


class TestProducerLifecycle:
    def _settings(self, enabled: bool) -> Any:
        return SimpleNamespace(
            enabled=enabled, status_report_interval_s=2.0, telemetry_proxy_interval_s=0.5
        )

    def test_cloud_disabled_injects_no_producer(self) -> None:
        runners: list[_Runner] = []
        plane = _plane(runners=runners)

        plane.ensure_runner(_instance())

        assert "sim_data_producer" not in runners[0].kwargs
        assert plane.producer_for_sim_id("sim-1") is None
        assert plane.producer_for_instance("inst-1") is None

    def test_cloud_enabled_creates_and_releases_producer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list[_FakeProducer] = []

        def _factory(settings: Any, sim_id: str = "") -> _FakeProducer:
            producer = _FakeProducer(settings, sim_id)
            created.append(producer)
            return producer

        monkeypatch.setattr("hunter_sim.cloud.sim_data_producer.SimDataProducer", _factory)
        runners: list[_Runner] = []
        plane = _plane(runners=runners, cloud_settings=self._settings(enabled=True))

        plane.ensure_runner(_instance())

        kwargs = runners[0].kwargs
        assert kwargs["status_interval_s"] == pytest.approx(2.0)
        assert kwargs["telemetry_interval_s"] == pytest.approx(0.5)
        producer = kwargs["sim_data_producer"]
        assert producer.connected == 1
        assert plane.producer_for_sim_id("sim-1") is producer
        assert plane.producer_for_instance("inst-1") is producer
        assert plane.producer_for_sim_id("") is None

        plane.release("inst-1")

        assert producer.closed == 1
        assert plane.producer_for_sim_id("sim-1") is None
        assert plane.producer_for_instance("inst-1") is None

    def test_producer_reused_across_instances_of_same_sim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list[_FakeProducer] = []

        def _factory(settings: Any, sim_id: str = "") -> _FakeProducer:
            producer = _FakeProducer(settings, sim_id)
            created.append(producer)
            return producer

        monkeypatch.setattr("hunter_sim.cloud.sim_data_producer.SimDataProducer", _factory)
        runners: list[_Runner] = []
        plane = _plane(
            pool=EnginePool([EngineEndpoint("127.0.0.1", 2000, 2001)]),
            runners=runners,
            cloud_settings=self._settings(enabled=True),
        )

        plane.ensure_runner(_instance("inst-1"))
        plane.ensure_runner(_instance("inst-2", carla_rpc_port=3000, carla_stream_port=3001))

        assert len(created) == 1
        assert runners[0].kwargs["sim_data_producer"] is runners[1].kwargs["sim_data_producer"]

    def test_close_cloud_producer_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        producers: list[_FakeProducer] = []

        def _factory(settings: Any, sim_id: str = "") -> _FakeProducer:
            producer = _FakeProducer(settings, sim_id)
            producers.append(producer)
            return producer

        monkeypatch.setattr("hunter_sim.cloud.sim_data_producer.SimDataProducer", _factory)
        plane = _plane(cloud_settings=self._settings(enabled=True))
        plane.ensure_runner(_instance())

        plane.close_cloud_producer("sim-1")
        plane.close_cloud_producer("sim-1")

        assert producers[0].closed == 1

    def test_close_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _factory(settings: Any, sim_id: str = "") -> _FakeProducer:
            producer = _FakeProducer(settings, sim_id)
            producer.close_error = RuntimeError("already closed")
            return producer

        monkeypatch.setattr("hunter_sim.cloud.sim_data_producer.SimDataProducer", _factory)
        plane = _plane(cloud_settings=self._settings(enabled=True))
        plane.ensure_runner(_instance())

        plane.release("inst-1")  # 不应抛


# ─── 快照 / world 设置 / 端点推导 ───────────────────────────────────────────


class TestSnapshotAndWorld:
    def test_snapshot_lists_runners_and_engines(self) -> None:
        plane = _plane()
        plane.ensure_runner(_instance())

        payload = plane.snapshot()

        assert payload["active_leases"] == 1
        assert payload["runners"] == [{"instance_id": "inst-1", "engine": "127.0.0.1:2000"}]
        assert payload["engines"][0]["holder"] == "inst-1"

    def test_properties_expose_pool_and_runners(self) -> None:
        plane = _plane()
        plane.ensure_runner(_instance())

        assert plane.pool is not None
        assert list(plane.runners) == ["inst-1"]

    def test_world_map_id_extracts_tail(self) -> None:
        world = SimpleNamespace(get_map=lambda: SimpleNamespace(name="/Game/Maps/Town03/"))

        assert _world_map_id(world, "fallback") == "Town03"
        assert _world_map_id(SimpleNamespace(get_map=_raising), "fallback") == "fallback"
        assert _world_map_id(SimpleNamespace(get_map=lambda: SimpleNamespace(name="")), "fb") == "fb"

    def test_configure_world_without_settings(self) -> None:
        plane = _plane()
        plane._configure_world(_World(settings_error=True))  # noqa: SLF001
        plane._configure_world(_World(settings_none=True))  # noqa: SLF001

    def test_configure_world_swallows_apply_failure(self) -> None:
        plane = _plane()
        world = _World(apply_error=True)

        plane._configure_world(world)  # noqa: SLF001

        assert world.applied == 0

    def test_endpoint_of_defaults(self) -> None:
        endpoint = ExecutionPlane._endpoint_of(SimpleNamespace())  # noqa: SLF001

        assert endpoint == EngineEndpoint("127.0.0.1", 2000, 2001, 8000)


def _raising() -> Any:
    raise RuntimeError("no map")
