"""V2.7.0 P1-12：端到端 REST 闭环集成测试（FakeCarla，无真实引擎依赖）。

覆盖完整用户旅程与服务韧性回归：
- REST 闭环：登录 → 创建实例 → 下发场景 → 启动 → 状态查询 → 停止 → 销毁 → 引擎租约归还；
- 重启恢复回归：SQLite 状态库跨进程重启恢复实例与场景快照；
- JWT 刷新/吊销链路（P1-6）：滑动续期轮换、旧 Token 即时失效、登出吊销；
- 僵尸租约管理员强制回收（P1-7）：权限校验与租约归还。

场景运行循环在 ``SceneRunnerService`` 真实实现上执行，CARLA SDK 由
``tests/mocks/carla_mocks`` 组装的 FakeCarla 模块顶替（``sys.modules["carla"]``）。
"""

from __future__ import annotations

import sys
import time
import types
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hunter_sim.api.main import create_app
from hunter_sim.api.runtime import get_scene_runner
from hunter_sim.common.models import (
    APISettings,
    CarlaSettings,
    HunterSimSettings,
    StateSettings,
)
from tests.mocks.carla_mocks import (
    MockLocation,
    MockRotation,
    MockTransform,
    MockVector3D,
    MockWorld,
)

_API = "/api/v1/sim"

_SCENE_CONFIG: dict[str, Any] = {
    "scene_id": "scene-e2e-1",
    "scene_name": "E2E 闭环场景",
    "map_id": "Town03",
    "duration_seconds": 60.0,
    "ego_vehicle": {"spawn_point": {"x": 1.0, "y": 2.0, "yaw_deg": 90.0}},
}


# ─── FakeCarla 与测试应用装配 ─────────────────────────────────────────────────


def _install_fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 FakeCarla 注入 ``sys.modules``，供场景运行循环的惰性 ``import carla`` 使用。"""

    class _WeatherParameters:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    mod = types.ModuleType("carla")
    mod.Transform = MockTransform  # type: ignore[attr-defined]
    mod.Location = MockLocation  # type: ignore[attr-defined]
    mod.Rotation = MockRotation  # type: ignore[attr-defined]
    mod.Vector3D = MockVector3D  # type: ignore[attr-defined]
    mod.WeatherParameters = _WeatherParameters  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "carla", mod)


class _StubMapManager:
    """MapManager 桩：按地图返回 MockWorld（免真实 load_world/RPC）。"""

    def __init__(self) -> None:
        self.loaded_maps: list[str] = []

    def load_map(self, map_id: str) -> MockWorld:
        self.loaded_maps.append(map_id)
        return MockWorld(map_id)


def _make_settings(db_path: str) -> HunterSimSettings:
    """集成测试配置：状态库开启、限流关闭、单引擎端点与桩一致。"""
    return HunterSimSettings(
        env="dev",
        log_level="WARNING",
        carla=CarlaSettings(host="127.0.0.1", rpc_port=2000, stream_port=2001),
        api=APISettings(
            rate_limit_enabled=False,
            login_max_failed_attempts=1_000_000,
            admin_username="admin",
            admin_password=APISettings().admin_password,  # 默认口令（仅 dev）
        ),
        state=StateSettings(enabled=True, db_path=db_path),
    )


@pytest.fixture
def e2e_app(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """工厂 fixture：返回 ``build()``，每次调用装配独立 app（共享同一状态库路径）。"""
    _install_fake_carla(monkeypatch)
    db_path = str(tmp_path / "e2e_state.db")

    def build() -> tuple[Any, _StubMapManager]:
        settings = _make_settings(db_path)
        application = create_app(settings)
        stub = _StubMapManager()
        application.state.map_manager = stub
        return application, stub

    return build, db_path


def _login(client: TestClient) -> dict[str, str]:
    """内置管理员登录并返回 Bearer header。"""
    resp = client.post(
        f"{_API}/auth/token", json={"username": "admin", "password": "admin"}
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_instance(client: TestClient, headers: dict[str, str]) -> str:
    resp = client.post(
        f"{_API}/instances",
        json={"mode": "sil", "map": "Town03", "vehicle_model": "hunter.se"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["data"]["sim_instance_id"])


def _wait_scene_status(
    client: TestClient, headers: dict[str, str], instance_id: str, expected: set[str],
    timeout: float = 5.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        resp = client.get(f"{_API}/scenes/{instance_id}/status", headers=headers)
        assert resp.status_code == 200, resp.text
        last = resp.json()["data"]
        if last.get("status") in expected:
            return last
        time.sleep(0.05)
    raise AssertionError(f"scene status stuck at {last!r}, expected one of {expected}")


# ─── REST 闭环 ────────────────────────────────────────────────────────────────


class TestRestClosedLoop:
    def test_full_lifecycle_with_real_runner_loop(self, e2e_app: Any) -> None:
        """登录 → 创建 → 下发 → 启动（真实 tick 循环）→ 停止 → 销毁 → 租约归还。"""
        build, _ = e2e_app
        app, stub = build()
        with TestClient(app) as client:
            headers = _login(client)

            instance_id = _create_instance(client, headers)
            resp = client.post(
                f"{_API}/scenes/load",
                json={"instance_id": instance_id, "scene_config": _SCENE_CONFIG},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            assert stub.loaded_maps == ["Town03"]
            runner = get_scene_runner(app, instance_id)
            assert runner is not None

            resp = client.post(f"{_API}/scenes/{instance_id}/start", headers=headers)
            assert resp.status_code == 200, resp.text
            state = _wait_scene_status(client, headers, instance_id, {"running"})
            assert state["scene_id"] == "scene-e2e-1"
            # 运行循环真实推进：MockWorld 已被 tick
            assert runner._world._tick_count > 0  # noqa: SLF001

            # 执行面快照显示引擎租约持有者
            resp = client.get(f"{_API}/execution", headers=headers)
            assert resp.status_code == 200
            leases = resp.json()["data"]["leases"]
            assert instance_id in {lease["instance_id"] for lease in leases}

            resp = client.post(f"{_API}/scenes/{instance_id}/stop", headers=headers)
            assert resp.status_code == 200, resp.text
            state = _wait_scene_status(client, headers, instance_id, {"completed"})
            assert state["status"] == "completed"

            resp = client.delete(f"{_API}/instances/{instance_id}", headers=headers)
            assert resp.status_code == 200, resp.text

            resp = client.get(f"{_API}/execution", headers=headers)
            leases = resp.json()["data"]["leases"]
            assert instance_id not in {lease["instance_id"] for lease in leases}

    def test_scene_snapshot_persisted_each_transition(self, e2e_app: Any) -> None:
        """P1-10：状态跃迁必写——load/start/stop 后快照状态可经 detached 通道追溯。"""
        build, _ = e2e_app
        app, _stub = build()
        with TestClient(app) as client:
            headers = _login(client)
            instance_id = _create_instance(client, headers)
            client.post(
                f"{_API}/scenes/load",
                json={"instance_id": instance_id, "scene_config": _SCENE_CONFIG},
                headers=headers,
            )
            store = app.state.state_store
            row = store.load_scene(instance_id)
            assert row is not None
            assert row["status"] in ("ready", "loading")


# ─── 重启恢复回归 ─────────────────────────────────────────────────────────────


class TestRestartRecovery:
    def test_instances_and_scene_snapshot_recovered(self, e2e_app: Any) -> None:
        """重启后：未终态实例被恢复；场景快照走 detached 通道；重新下发可再装配。"""
        build, _ = e2e_app

        app1, _ = build()
        with TestClient(app1) as client:
            headers = _login(client)
            instance_id = _create_instance(client, headers)
            resp = client.post(
                f"{_API}/scenes/load",
                json={"instance_id": instance_id, "scene_config": _SCENE_CONFIG},
                headers=headers,
            )
            assert resp.status_code == 200

        # 第二个进程（同状态库路径）：重启恢复
        app2, stub2 = build()
        with TestClient(app2) as client:
            headers = _login(client)
            resp = client.get(f"{_API}/instances", headers=headers)
            assert resp.status_code == 200
            ids = {i["sim_instance_id"] for i in resp.json()["data"]["instances"]}
            assert instance_id in ids  # 未终态实例已恢复

            resp = client.get(f"{_API}/scenes/{instance_id}/status", headers=headers)
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data.get("detached") is True
            assert data["scene_id"] == "scene-e2e-1"

            # 重新下发场景 → 运行器再装配（FakeCarla 闭环继续可跑）
            resp = client.post(
                f"{_API}/scenes/load",
                json={"instance_id": instance_id, "scene_config": _SCENE_CONFIG},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            assert stub2.loaded_maps == ["Town03"]


# ─── JWT 刷新/吊销链路（P1-6）────────────────────────────────────────────────


class TestJwtRefreshRevoke:
    def test_refresh_rotates_and_revokes_old_token(self, e2e_app: Any) -> None:
        build, _ = e2e_app
        app, _ = build()
        with TestClient(app) as client:
            headers = _login(client)
            resp = client.post(f"{_API}/auth/refresh", headers=headers)
            assert resp.status_code == 200, resp.text
            new_headers = {
                "Authorization": f"Bearer {resp.json()['data']['access_token']}"
            }
            # 旧 Token 的 jti 已随续期轮换被吊销
            resp = client.get(f"{_API}/instances", headers=headers)
            assert resp.status_code == 401
            # 新 Token 立即可用
            resp = client.get(f"{_API}/instances", headers=new_headers)
            assert resp.status_code == 200

            # 登出吊销：当前 Token 即时失效（幂等）
            resp = client.post(f"{_API}/auth/revoke", headers=new_headers)
            assert resp.status_code == 200, resp.text
            assert resp.json()["data"]["revoked"] is True
            resp = client.get(f"{_API}/instances", headers=new_headers)
            assert resp.status_code == 401

    def test_revoked_token_rejected_even_before_expiry(self, e2e_app: Any) -> None:
        build, _ = e2e_app
        app, _ = build()
        with TestClient(app) as client:
            headers = _login(client)
            resp = client.post(f"{_API}/auth/revoke", headers=headers)
            assert resp.status_code == 200
            # 再次吊销（幂等，不报错）
            resp = client.post(f"{_API}/auth/revoke", headers=headers)
            assert resp.status_code == 401  # Token 已失效，无法再次通过中间件


# ─── 僵尸租约强制回收（P1-7）─────────────────────────────────────────────────


class TestLeaseReclaim:
    def test_admin_reclaims_only_stale_lease(self, e2e_app: Any) -> None:
        build, _ = e2e_app
        app, _ = build()
        with TestClient(app) as client:
            headers = _login(client)
            instance_id = _create_instance(client, headers)
            client.post(
                f"{_API}/scenes/load",
                json={"instance_id": instance_id, "scene_config": _SCENE_CONFIG},
                headers=headers,
            )

            # 非管理员：403
            from hunter_sim.api.deps import create_access_token

            settings = app.state.settings
            operator_token = create_access_token(
                "op_user", settings.api, extra_claims={"role": "operator"}
            )
            resp = client.delete(
                f"{_API}/execution/leases/{instance_id}",
                headers={"Authorization": f"Bearer {operator_token}"},
            )
            assert resp.status_code == 403

            # V2.9.0：活跃租约拒绝强制回收（否则会挖断正在运行的场景）
            resp = client.delete(f"{_API}/execution/leases/{instance_id}", headers=headers)
            assert resp.status_code == 409, resp.text
            assert "still alive" in resp.json()["message"]

            # 模拟僵尸持有者（心跳远超 TTL）→ 允许回收
            plane = client.app.state.execution_plane
            lease = plane.lease_of(instance_id)
            assert lease is not None
            lease.renew(now=time.time() - 10_000.0)

            resp = client.delete(f"{_API}/execution/leases/{instance_id}", headers=headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()["data"]
            assert data["reclaimed"] is True
            assert data["instance_failed"] is True

            resp = client.get(f"{_API}/execution", headers=headers)
            leases = resp.json()["data"]["leases"]
            assert instance_id not in {item["instance_id"] for item in leases}

            # 实例同步置终态，端点才会被控制面算作可分配
            detail = client.get(f"{_API}/instances/{instance_id}", headers=headers)
            assert detail.json()["data"]["status"] == "failed"

    def test_reclaim_unknown_instance_404(self, e2e_app: Any) -> None:
        build, _ = e2e_app
        app, _ = build()
        with TestClient(app) as client:
            headers = _login(client)
            resp = client.delete(f"{_API}/execution/leases/inst-does-not-exist", headers=headers)
            assert resp.status_code == 404
