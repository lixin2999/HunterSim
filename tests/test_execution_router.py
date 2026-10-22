"""执行面观测路由与重启恢复集成测试（V2.4.0）。

覆盖：
- ``GET /api/v1/sim/execution`` 的引擎库存/租约/持久化信息与管理员权限；
- 服务重启后实例恢复、场景快照回落（``detached=true``）与 /health 字段。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from hunter_sim.common.models import ExecutionSettings, HunterSimSettings, StateSettings


def _settings(tmp_path: Any, *, endpoints: str = "") -> HunterSimSettings:
    return HunterSimSettings(
        env="dev",
        execution=ExecutionSettings(engine_endpoints=endpoints),
        state=StateSettings(enabled=True, db_path=str(tmp_path / "state.db")),
    )


class TestExecutionEndpoint:
    def test_admin_reads_engine_inventory(
        self, tmp_path: Any, auth_headers: dict[str, str]
    ) -> None:
        from hunter_sim.api.main import create_app

        app = create_app(_settings(tmp_path, endpoints="10.0.0.1:2000,10.0.0.2:2100"))
        with TestClient(app) as client:
            resp = client.get("/api/v1/sim/execution", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["capacity"] == 2
        assert {e["key"] for e in data["engines"]} == {"10.0.0.1:2000", "10.0.0.2:2100"}
        assert data["active_leases"] == 0
        assert data["leases"] == []
        assert data["runners"] == []
        assert data["persistence"]["enabled"] is True
        assert data["persistence"]["restored_instances"] == 0

    def test_single_engine_fallback(self, tmp_path: Any, auth_headers: dict[str, str]) -> None:
        from hunter_sim.api.main import create_app

        app = create_app(_settings(tmp_path))
        with TestClient(app) as client:
            data = client.get("/api/v1/sim/execution", headers=auth_headers).json()["data"]

        assert data["capacity"] == 1
        assert data["engines"][0]["key"] == "127.0.0.1:2000"

    def test_non_admin_forbidden(self, tmp_path: Any, user_headers: dict[str, str]) -> None:
        from hunter_sim.api.main import create_app

        app = create_app(_settings(tmp_path))
        with TestClient(app) as client:
            resp = client.get("/api/v1/sim/execution", headers=user_headers)

        assert resp.status_code == 403

    def test_missing_token_unauthorized(self, tmp_path: Any) -> None:
        from hunter_sim.api.main import create_app

        app = create_app(_settings(tmp_path))
        with TestClient(app) as client:
            resp = client.get("/api/v1/sim/execution")

        assert resp.status_code == 401


class TestRestartRecovery:
    def test_instances_and_scene_binding_survive_restart(
        self, tmp_path: Any, auth_headers: dict[str, str]
    ) -> None:
        from hunter_sim.api.main import create_app
        from hunter_sim.api.runtime import persist_scene
        from hunter_sim.scene_runner.scene_config import SceneConfig

        settings = _settings(tmp_path)
        first = create_app(settings)
        with TestClient(first) as client:
            created = client.post(
                "/api/v1/sim/instances",
                json={"mode": "sil", "map": "Town03", "quality": "low"},
                headers=auth_headers,
            )
            assert created.status_code == 201, created.text
            instance_id = created.json()["data"]["sim_instance_id"]

            # 场景下发成功后的真实持久化路径（此处直接构造 SceneConfig 避开 CARLA）
            config = SceneConfig(
                scene_id="scene-1",
                map_id="Town03",
                ego_vehicle={"spawn_point": {"x": 1.0, "y": 2.0, "z": 0.3, "yaw_deg": 90.0}},
            )
            persist_scene(first, instance_id, config)
            # 模拟重启前场景处于运行中：重启后应归一为 ready 且保留快照
            first.state.state_store.update_scene_snapshot(
                instance_id, "running", progress=0.5, elapsed_seconds=12.0
            )

        # 同一数据库启动第二个进程实例
        second = create_app(settings)
        with TestClient(second) as client:
            detail = client.get(
                f"/api/v1/sim/instances/{instance_id}", headers=auth_headers
            )
            assert detail.status_code == 200
            instance = detail.json()["data"]
            assert instance["sim_instance_id"] == instance_id
            assert instance["status"] == "created"

            status = client.get(
                f"/api/v1/sim/instances/{instance_id}/status", headers=auth_headers
            ).json()["data"]
            assert status["scene"]["detached"] is True
            assert status["scene"]["scene_id"] == "scene-1"
            assert status["scene"]["status"] == "ready"
            assert status["scene"]["elapsed_seconds"] == 12.0

            scene_status = client.get(
                f"/api/v1/sim/scenes/{instance_id}/status", headers=auth_headers
            )
            assert scene_status.status_code == 200
            assert scene_status.json()["data"]["detached"] is True

            health = client.get("/api/v1/sim/health").json()["data"]
            assert health["state_persistence"] is True
            assert health["restored_instances"] == 1

            execution = client.get(
                "/api/v1/sim/execution", headers=auth_headers
            ).json()["data"]
            assert execution["persistence"]["restored_instances"] == 1

    def test_destroyed_instance_is_not_restored(
        self, tmp_path: Any, auth_headers: dict[str, str]
    ) -> None:
        from hunter_sim.api.main import create_app

        settings = _settings(tmp_path)
        first = create_app(settings)
        with TestClient(first) as client:
            created = client.post(
                "/api/v1/sim/instances",
                json={"mode": "sil", "map": "Town03"},
                headers=auth_headers,
            )
            instance_id = created.json()["data"]["sim_instance_id"]
            destroyed = client.delete(
                f"/api/v1/sim/instances/{instance_id}", headers=auth_headers
            )
            assert destroyed.status_code == 200

        second = create_app(settings)
        with TestClient(second) as client:
            resp = client.get(
                f"/api/v1/sim/instances/{instance_id}", headers=auth_headers
            )
            assert resp.status_code == 404
            health = client.get("/api/v1/sim/health").json()["data"]
            assert health["restored_instances"] == 0
