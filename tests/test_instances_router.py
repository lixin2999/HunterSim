"""实例管理路由完整生命周期测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from hunter_sim.common.models import InstanceStatus


def _create(client: TestClient, auth_headers: dict, mode: str = "sil") -> str:
    resp = client.post(
        "/api/v1/sim/instances",
        json={"mode": mode, "map_id": "Town05", "quality": "medium"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    return resp.json()["data"]["sim_instance_id"]


def _to_ready(app: Any, instance_id: str) -> None:
    mgr = app.state.instance_manager
    mgr.transition(instance_id, InstanceStatus.LOADING)
    mgr.transition(instance_id, InstanceStatus.READY)


class TestCreateAndList:
    def test_create_returns_instance(self, client: TestClient, auth_headers: dict) -> None:
        iid = _create(client, auth_headers)
        assert iid

    def test_create_bad_mode_422(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.post(
            "/api/v1/sim/instances", json={"mode": "bogus"}, headers=auth_headers
        )
        assert resp.status_code == 422

    def test_get_after_create(self, client: TestClient, auth_headers: dict) -> None:
        iid = _create(client, auth_headers)
        resp = client.get(f"/api/v1/sim/instances/{iid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["sim_instance_id"] == iid

    def test_list_filter_by_status(self, client: TestClient, auth_headers: dict) -> None:
        _create(client, auth_headers)  # 至少一个 CREATED
        resp = client.get(
            "/api/v1/sim/instances", params={"status": "created"}, headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] >= 1
        # 过滤为不存在的状态时返回空
        resp2 = client.get(
            "/api/v1/sim/instances", params={"status": "destroyed"}, headers=auth_headers
        )
        assert all(i["status"] != "destroyed" for i in resp2.json()["data"]["instances"])


class TestLifecycleTransitions:
    def test_start_illegal_from_created(
        self, client: TestClient, auth_headers: dict, app: Any
    ) -> None:
        iid = _create(client, auth_headers)
        # CREATED -> RUNNING 非法 → 409
        resp = client.post(f"/api/v1/sim/instances/{iid}/start", headers=auth_headers)
        assert resp.status_code == 409

    def test_full_lifecycle(self, client: TestClient, auth_headers: dict, app: Any) -> None:
        iid = _create(client, auth_headers)
        _to_ready(app, iid)

        assert client.post(f"/api/v1/sim/instances/{iid}/start", headers=auth_headers).json()["data"]["status"] == "running"
        assert client.post(f"/api/v1/sim/instances/{iid}/pause", headers=auth_headers).json()["data"]["status"] == "paused"
        assert client.post(f"/api/v1/sim/instances/{iid}/resume", headers=auth_headers).json()["data"]["status"] == "running"
        assert client.post(f"/api/v1/sim/instances/{iid}/stop", headers=auth_headers).json()["data"]["status"] == "completed"

        dele = client.delete(f"/api/v1/sim/instances/{iid}", headers=auth_headers)
        assert dele.status_code == 200
        assert dele.json()["data"]["destroyed"] is True

    def test_get_after_destroy_404(
        self, client: TestClient, auth_headers: dict, app: Any
    ) -> None:
        iid = _create(client, auth_headers)
        client.delete(f"/api/v1/sim/instances/{iid}", headers=auth_headers)
        assert client.get(f"/api/v1/sim/instances/{iid}", headers=auth_headers).status_code == 404

    def test_destroy_missing_idempotent(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        resp = client.delete("/api/v1/sim/instances/does-not-exist", headers=auth_headers)
        assert resp.status_code == 404
