"""场景运行时装配测试（V2.3.0）。

覆盖 api/runtime.py 的按需装配、单引擎占用约束、释放与实例归属校验，
不依赖真实 CARLA server（MapManager/CARLA Client 均为桩）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from hunter_sim.api.runtime import (
    InstanceNotFound,
    SceneRuntimeUnavailable,
    assert_instance_access,
    ensure_scene_runner,
    get_scene_runner,
    release_all_scene_runners,
    release_scene_runner,
)
from hunter_sim.common.models import InstanceStatus
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

    def test_map_conflict_raises_unavailable(self) -> None:
        app, _, _, _ = _make_app(map_id="Town03")
        app.state.scene_world_map_id = "Town04"

        with pytest.raises(SceneRuntimeUnavailable, match="Town04"):
            ensure_scene_runner(app, "inst-1")

    def test_engine_occupied_by_another_instance(self) -> None:
        app, _, _, _ = _make_app()
        first = ensure_scene_runner(app, "inst-1")

        # 新实例（同地图）也不允许共享同一引擎
        other = SimpleNamespace(
            sim_instance_id="inst-2",
            map_id="Town03",
            status=InstanceStatus.CREATED,
            user_id="user-2",
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
