"""场景运行时装配测试（V2.4.0）。

覆盖 api/runtime.py 的按需装配、引擎占用约束、释放、实例归属校验，
以及场景持久化快照与重启后的运行器恢复；不依赖真实 CARLA server。
"""

from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from hunter_sim.api.runtime import (
    InstanceNotFound,
    SceneRuntimeUnavailable,
    _resolve_map_manager,
    _restore_persisted_scene,
    assert_instance_access,
    describe_execution,
    ensure_scene_runner,
    get_scene_runner,
    init_runtime_state,
    mark_instance_failed,
    persist_scene,
    persisted_scene_state,
    release_all_scene_runners,
    release_scene_runner,
)
from hunter_sim.common.models import InstanceStatus
from hunter_sim.execution.engine_pool import EngineEndpoint
from hunter_sim.scene_runner.scene_runner_service import SceneRunnerService
from tests.mocks.carla_mocks import MockWorld


class _StubMapManager:
    """MapManager 桩：记录加载的地图并返回 MockWorld。"""

    def __init__(self) -> None:
        self.loaded_maps: list[str] = []

    def load_map(self, map_id: str) -> MockWorld:
        self.loaded_maps.append(map_id)
        return MockWorld(map_id)


class _StubInstanceManager:
    """只支持一个实例的 SimInstanceManager 桩。"""

    def __init__(self, instance: Any) -> None:
        self.instance = instance
        self.transitions: list[tuple[str, InstanceStatus, str]] = []

    def get_instance(self, instance_id: str) -> Any:
        return self.instance if self.instance.sim_instance_id == instance_id else None

    def transition(self, instance_id: str, status: InstanceStatus, reason: str = "") -> None:
        self.transitions.append((instance_id, status, reason))
        self.instance.status = status


def _make_app(
    *,
    map_id: str = "Town03",
    status: InstanceStatus = InstanceStatus.CREATED,
    with_carla: bool = True,
) -> tuple[Any, Any, _StubInstanceManager, _StubMapManager]:
    """构造最小 app.state（scene_runners + instance_manager + carla 组件）。"""
    instance = SimpleNamespace(
        sim_instance_id="inst-1",
        map_id=map_id,
        status=status,
        user_id="user-1",
        carla_host="localhost",
        carla_rpc_port=2000,
    )
    manager = _StubInstanceManager(instance)
    map_manager = _StubMapManager()
    app = SimpleNamespace(
        state=SimpleNamespace(
            scene_runners={},
            instance_manager=manager,
            carla_client=object() if with_carla else None,
            map_manager=map_manager if with_carla else None,
            carla_probe_error=None if with_carla else "Connection refused",
            # 执行面配置：端点与实例一致（localhost:2000），与单引擎部署语义一致
            settings=SimpleNamespace(
                carla=SimpleNamespace(host="localhost", rpc_port=2000, stream_port=2001),
                execution=SimpleNamespace(
                    engine_endpoints="",
                    max_engines=8,
                    restore_scene_on_assemble=True,
                ),
            ),
        )
    )
    return app, instance, manager, map_manager


class TestEnsureSceneRunner:
    def test_assembles_runner_and_marks_ready(self) -> None:
        app, instance, manager, map_manager = _make_app()

        runner = ensure_scene_runner(app, "inst-1")

        assert isinstance(runner, SceneRunnerService)
        assert map_manager.loaded_maps == ["Town03"]
        assert app.state.scene_world_map_id == "Town03"
        assert get_scene_runner(app, "inst-1") is runner
        assert [t[1] for t in manager.transitions] == [InstanceStatus.LOADING, InstanceStatus.READY]
        assert instance.status == InstanceStatus.READY

    def test_reuses_existing_runner(self) -> None:
        app, _, _, map_manager = _make_app()

        first = ensure_scene_runner(app, "inst-1")
        second = ensure_scene_runner(app, "inst-1")

        assert first is second
        assert map_manager.loaded_maps == ["Town03"]  # 未重复 load_world

    def test_unknown_instance_raises_not_found(self) -> None:
        app, _, _, _ = _make_app()
        with pytest.raises(InstanceNotFound):
            ensure_scene_runner(app, "ghost")

    def test_missing_carla_raises_unavailable(self) -> None:
        app, instance, manager, _ = _make_app(with_carla=False)

        with pytest.raises(SceneRuntimeUnavailable, match="Connection refused"):
            ensure_scene_runner(app, "inst-1")

        assert instance.status == InstanceStatus.CREATED
        assert manager.transitions == []

    def test_global_map_field_alone_no_longer_blocks_assemble(self) -> None:
        """P2-14：遗留的全局地图字段不再误伤（端点空闲即可装配）。"""
        app, _, _, map_manager = _make_app(map_id="Town03")
        app.state.scene_world_map_id = "Town04"

        runner = ensure_scene_runner(app, "inst-1")

        assert isinstance(runner, SceneRunnerService)
        assert map_manager.loaded_maps == ["Town03"]

    def test_endpoint_map_conflict_raises_unavailable(self) -> None:
        """P2-14：同一端点被他人活跃占用且地图不同时仍须拒绝。"""
        app, _, _, _ = _make_app()
        ensure_scene_runner(app, "inst-1")  # 端点 localhost:2000 已加载 Town03

        other = SimpleNamespace(
            sim_instance_id="inst-2",
            map_id="Town04",
            status=InstanceStatus.CREATED,
            user_id="user-2",
            carla_host="localhost",
            carla_rpc_port=2000,
            carla_stream_port=2001,
        )
        original_get = app.state.instance_manager.get_instance
        app.state.instance_manager.get_instance = (
            lambda iid: other if iid == "inst-2" else original_get(iid)
        )

        with pytest.raises(SceneRuntimeUnavailable, match="Town04"):
            ensure_scene_runner(app, "inst-2")

    def test_engine_occupied_by_another_instance(self) -> None:
        app, _, _, _ = _make_app()
        first = ensure_scene_runner(app, "inst-1")

        # 新实例（同引擎同地图）也不允许共享同一引擎
        other = SimpleNamespace(
            sim_instance_id="inst-2",
            map_id="Town03",
            status=InstanceStatus.CREATED,
            user_id="user-2",
            carla_host="localhost",
            carla_rpc_port=2000,
            carla_stream_port=2001,
        )
        original_get = app.state.instance_manager.get_instance
        app.state.instance_manager.get_instance = lambda iid: other if iid == "inst-2" else original_get(iid)

        with pytest.raises(SceneRuntimeUnavailable, match="inst-1"):
            ensure_scene_runner(app, "inst-2")
        assert get_scene_runner(app, "inst-1") is first


class TestReleaseSceneRunner:
    def test_release_destroys_runner_and_clears_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app, _, _, _ = _make_app()
        runner = ensure_scene_runner(app, "inst-1")
        destroyed: list[Any] = []
        monkeypatch.setattr(SceneRunnerService, "destroy", lambda self: destroyed.append(self))

        assert release_scene_runner(app, "inst-1") is True

        assert destroyed == [runner]
        assert get_scene_runner(app, "inst-1") is None
        assert app.state.scene_world_map_id is None

    def test_release_unknown_returns_false(self) -> None:
        app, _, _, _ = _make_app()
        assert release_scene_runner(app, "ghost") is False

    def test_release_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app, _, _, _ = _make_app()
        ensure_scene_runner(app, "inst-1")
        monkeypatch.setattr(SceneRunnerService, "destroy", lambda self: None)

        assert release_all_scene_runners(app) == 1
        assert app.state.scene_runners == {}


def _make_request(app: Any, *, user_id: str, role: str = "user") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "app": app,
        "state": {"user_id": user_id, "token_payload": {"sub": user_id, "role": role}},
    }
    return Request(scope)


class TestInstanceAccess:
    def test_owner_allowed(self) -> None:
        app, instance, _, _ = _make_app()
        request = _make_request(app, user_id="user-1")
        assert assert_instance_access(request, "inst-1", "user-1") is instance

    def test_other_user_forbidden(self) -> None:
        app, _, _, _ = _make_app()
        request = _make_request(app, user_id="user-2")
        with pytest.raises(HTTPException) as exc_info:
            assert_instance_access(request, "inst-1", "user-2")
        assert exc_info.value.status_code == 403

    def test_admin_allowed(self) -> None:
        app, instance, _, _ = _make_app()
        request = _make_request(app, user_id="admin-1", role="admin")
        assert assert_instance_access(request, "inst-1", "admin-1") is instance

    def test_missing_instance_returns_404(self) -> None:
        app, _, _, _ = _make_app()
        request = _make_request(app, user_id="user-1")
        with pytest.raises(HTTPException) as exc_info:
            assert_instance_access(request, "ghost", "user-1")
        assert exc_info.value.status_code == 404

    def test_require_exists_false_skips_missing(self) -> None:
        app, _, _, _ = _make_app()
        request = _make_request(app, user_id="user-1")
        assert assert_instance_access(request, "ghost", "user-1", require_exists=False) is None


class _StubSceneRunner:
    """运行器桩：仅覆盖持久化恢复所需接口。"""

    def __init__(self, has_scene: bool = False, fail: bool = False) -> None:
        self.has_scene = has_scene
        self.fail = fail
        self.loaded: list[Any] = []

    def load_scene(self, config: Any) -> None:
        if self.fail:
            raise RuntimeError("restore failed")
        self.loaded.append(config)


def _scene_config() -> Any:
    from hunter_sim.scene_runner.scene_config import SceneConfig

    return SceneConfig(
        scene_id="scene-1",
        map_id="Town03",
        ego_vehicle={"spawn_point": {"x": 1.0, "y": 2.0}},
    )


def _app_with_store(tmp_path: Any) -> Any:
    from hunter_sim.persistence.state_store import StateStore

    app, _, _, _ = _make_app()
    app.state.state_store = StateStore(str(tmp_path / "state.db"))
    return app


class TestScenePersistence:
    """场景定义与快照持久化（V2.4.0 重启恢复）。"""

    def test_persist_then_read_snapshot(self, tmp_path: Any) -> None:
        app = _app_with_store(tmp_path)

        persist_scene(app, "inst-1", _scene_config())
        snapshot = persisted_scene_state(app, "inst-1")

        assert snapshot is not None
        assert snapshot["scene_id"] == "scene-1"
        assert snapshot["status"] == "ready"
        assert snapshot["detached"] is True
        app.state.state_store.close()

    def test_persist_without_store_is_noop(self) -> None:
        app, _, _, _ = _make_app()
        persist_scene(app, "inst-1", _scene_config())
        assert persisted_scene_state(app, "inst-1") is None

    def test_restore_loads_scene_when_runner_has_none(self, tmp_path: Any) -> None:
        app = _app_with_store(tmp_path)
        persist_scene(app, "inst-1", _scene_config())
        runner = _StubSceneRunner(has_scene=False)

        _restore_persisted_scene(app, "inst-1", runner)

        assert len(runner.loaded) == 1
        assert runner.loaded[0].scene_id == "scene-1"
        app.state.state_store.close()

    def test_restore_skipped_when_runner_already_has_scene(self, tmp_path: Any) -> None:
        app = _app_with_store(tmp_path)
        persist_scene(app, "inst-1", _scene_config())
        runner = _StubSceneRunner(has_scene=True)

        _restore_persisted_scene(app, "inst-1", runner)

        assert runner.loaded == []
        app.state.state_store.close()

    def test_restore_skipped_when_disabled(self, tmp_path: Any) -> None:
        app = _app_with_store(tmp_path)
        app.state.settings = SimpleNamespace(
            execution=SimpleNamespace(restore_scene_on_assemble=False)
        )
        persist_scene(app, "inst-1", _scene_config())
        runner = _StubSceneRunner(has_scene=False)

        _restore_persisted_scene(app, "inst-1", runner)

        assert runner.loaded == []
        app.state.state_store.close()

    def test_restore_failure_is_non_fatal(self, tmp_path: Any) -> None:
        app = _app_with_store(tmp_path)
        persist_scene(app, "inst-1", _scene_config())
        runner = _StubSceneRunner(has_scene=False, fail=True)

        _restore_persisted_scene(app, "inst-1", runner)  # 不应抛异常

        app.state.state_store.close()

    def test_state_callback_updates_snapshot(self, tmp_path: Any) -> None:
        from hunter_sim.api.runtime import _scene_state_callback
        from hunter_sim.common.models import SceneStatus

        app = _app_with_store(tmp_path)
        persist_scene(app, "inst-1", _scene_config())
        callback = _scene_state_callback(app, "inst-1")

        callback(
            SimpleNamespace(
                status=SceneStatus.RUNNING,
                progress=0.75,
                elapsed_seconds=12.5,
                error_message="",
            )
        )

        snapshot = persisted_scene_state(app, "inst-1")
        assert snapshot is not None
        assert snapshot["status"] == "running"
        assert snapshot["progress"] == 0.75
        assert snapshot["elapsed_seconds"] == 12.5
        app.state.state_store.close()


class TestDescribeExecution:
    def test_without_plane_uses_declared_endpoints(self) -> None:
        from hunter_sim.common.models import ExecutionSettings, HunterSimSettings

        app, _, _, _ = _make_app()
        app.state.settings = HunterSimSettings(
            execution=ExecutionSettings(engine_endpoints="10.0.0.1:2000,10.0.0.2:2100")
        )

        payload = describe_execution(app)

        assert payload["capacity"] == 2
        assert {e["key"] for e in payload["engines"]} == {
            "10.0.0.1:2000",
            "10.0.0.2:2100",
        }
        assert payload["active_leases"] == 0
        assert payload["stale_leases"] == 0
        assert payload["lease_ttl_seconds"] > 0.0
        assert payload["map_affinity_enabled"] is True
        assert payload["persistence"]["enabled"] is False

    def test_with_plane_reports_leases(self) -> None:
        app, _, _, _ = _make_app()
        ensure_scene_runner(app, "inst-1")

        payload = describe_execution(app)

        assert payload["active_leases"] == 1
        assert payload["engines"][0]["holder"] == "inst-1"
        assert payload["runners"] == [
            {"instance_id": "inst-1", "engine": "localhost:2000"}
        ]


# ─── V2.6.0：运行时状态预初始化（P0-1） ──────────────────────────────────


class TestInitRuntimeState:
    def test_creates_lock_and_registry(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())

        init_runtime_state(app)

        assert app.state.scene_runtime_lock is not None
        assert app.state.scene_runners == {}

    def test_idempotent_and_preserves_injected_objects(self) -> None:
        injected_lock = threading.RLock()
        injected: dict[str, Any] = {"inst-keep": object()}
        app = SimpleNamespace(
            state=SimpleNamespace(scene_runtime_lock=injected_lock, scene_runners=injected)
        )

        init_runtime_state(app)
        init_runtime_state(app)  # 重复调用无副作用

        assert app.state.scene_runtime_lock is injected_lock
        assert app.state.scene_runners is injected

    def test_concurrent_init_yields_single_lock(self) -> None:
        """并发预初始化只能创建一把锁（消除 check-then-act 竞态）。"""
        app = SimpleNamespace(state=SimpleNamespace())
        start = threading.Barrier(8, timeout=5.0)
        locks: list[Any] = []

        def _worker() -> None:
            start.wait()
            init_runtime_state(app)
            locks.append(app.state.scene_runtime_lock)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len({id(lock) for lock in locks}) == 1

    def test_lazy_fallback_still_works_without_init(self) -> None:
        """未走 lifespan 预初始化时，惰性双检仍能完成装配（兼容注入式 app）。"""
        app, _, _, map_manager = _make_app()
        assert not hasattr(app.state, "scene_runtime_lock")

        runner = ensure_scene_runner(app, "inst-1")

        assert runner is not None
        assert map_manager.loaded_maps == ["Town03"]
        assert app.state.scene_runtime_lock is not None


# ─── V2.9.0：按端点隔离 MapManager（让多引擎不再共用 base Client） ──────────


class TestMapManagerResolution:
    def test_base_endpoint_reuses_shared_manager(self) -> None:
        app, _, _, shared = _make_app()

        resolved = _resolve_map_manager(app, EngineEndpoint("localhost", 2000, 2001))

        assert resolved is shared

    def test_other_endpoint_gets_own_manager_and_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _, _, shared = _make_app()
        app.state.settings.carla.timeout_seconds = 12.5
        clients: list[SimpleNamespace] = []

        class _FakeClient:
            def __init__(self, host: str, port: int) -> None:
                self.record = SimpleNamespace(host=host, port=port, timeout=0.0)
                clients.append(self.record)

            def set_timeout(self, timeout: float) -> None:
                self.record.timeout = timeout

        fake_carla = types.ModuleType("carla")
        fake_carla.Client = _FakeClient  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "carla", fake_carla)

        endpoint = EngineEndpoint("10.0.0.2", 2100, 2101)
        resolved = _resolve_map_manager(app, endpoint)

        assert resolved is not None
        assert resolved is not shared  # 非 base 端点不得复用共享管理器
        assert _resolve_map_manager(app, endpoint) is resolved  # 同端点命中缓存
        assert len(clients) == 1
        assert (clients[0].host, clients[0].port) == ("10.0.0.2", 2100)
        assert clients[0].timeout == 12.5

    def test_injected_endpoint_managers_take_precedence(self) -> None:
        app, _, _, shared = _make_app()
        injected = object()
        app.state.map_managers = {"10.0.0.2:2100": injected}

        assert _resolve_map_manager(app, EngineEndpoint("10.0.0.2", 2100, 2101)) is injected
        # 未声明的端点在注入模式下返回 None（交由上层转不可用）
        assert _resolve_map_manager(app, EngineEndpoint("10.0.0.3", 2200, 2201)) is None
        assert _resolve_map_manager(app, EngineEndpoint("localhost", 2000, 2001)) is not shared


# ─── V2.9.0：实例状态联动（僵尸租约回收后置终态释放端点） ──────────────────


class TestMarkInstanceFailed:
    def test_ready_instance_transitions_to_failed(self) -> None:
        app, instance, manager, _ = _make_app(status=InstanceStatus.READY)

        assert mark_instance_failed(app, "inst-1", "lease reclaimed") is True

        assert instance.status == InstanceStatus.FAILED
        assert manager.transitions[-1] == (
            "inst-1",
            InstanceStatus.FAILED,
            "lease reclaimed",
        )

    def test_without_instance_manager_returns_false(self) -> None:
        app = SimpleNamespace(state=SimpleNamespace())

        assert mark_instance_failed(app, "inst-1", "lease reclaimed") is False

    def test_rejected_transition_returns_false(self) -> None:
        class _Rejecting:
            def transition(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("invalid transition")

        app = SimpleNamespace(state=SimpleNamespace(instance_manager=_Rejecting()))

        assert mark_instance_failed(app, "inst-1", "lease reclaimed") is False
