"""批量场景测试路由测试（设计文档 §12.1、§9.5、§11.2）。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

_SCENE_A: dict[str, Any] = {
    "scene_id": "scene-a",
    "scene_name": "场景A",
    "map_id": "Town05",
    "ego_vehicle": {"spawn_point": {"x": 1.0, "y": 2.0, "yaw_deg": 0.0}},
}
_SCENE_B: dict[str, Any] = {
    "scene_id": "scene-b",
    "scene_name": "场景B",
    "map_id": "Town05",
    "ego_vehicle": {"spawn_point": {"x": 3.0, "y": 4.0, "yaw_deg": 90.0}},
}


@pytest.fixture
def _no_tasks(app: Any) -> Any:
    """每个测试前清空任务注册表，测试后移除执行器。"""
    from hunter_sim.api.routers.scenarios import get_batch_tasks

    get_batch_tasks().clear()
    yield app
    get_batch_tasks().clear()
    if hasattr(app.state, "scenario_batch_executor"):
        del app.state.scenario_batch_executor


class TestBatchTaskRegistration:
    def test_create_batch_task(self, client: TestClient, auth_headers: dict, _no_tasks: Any) -> None:
        resp = client.post(
            "/api/v1/sim/scenarios/batch",
            json={"task_id": "task-001", "scenes": [_SCENE_A, _SCENE_B]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["task_id"] == "task-001"
        assert data["status"] == "pending"  # 未注入执行器
        assert data["total_scenes"] == 2

    def test_create_auto_task_id(self, client: TestClient, auth_headers: dict, _no_tasks: Any) -> None:
        resp = client.post(
            "/api/v1/sim/scenarios/batch",
            json={"scenes": [_SCENE_A]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["task_id"].startswith("batch_")

    def test_create_duplicate_task_409(
        self, client: TestClient, auth_headers: dict, _no_tasks: Any
    ) -> None:
        body = {"task_id": "task-dup", "scenes": [_SCENE_A]}
        assert client.post("/api/v1/sim/scenarios/batch", json=body, headers=auth_headers).status_code == 200
        resp = client.post("/api/v1/sim/scenarios/batch", json=body, headers=auth_headers)
        assert resp.status_code == 409

    def test_empty_scenes_422(self, client: TestClient, auth_headers: dict, _no_tasks: Any) -> None:
        resp = client.post(
            "/api/v1/sim/scenarios/batch", json={"scenes": []}, headers=auth_headers
        )
        assert resp.status_code == 422

    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.post("/api/v1/sim/scenarios/batch", json={"scenes": [_SCENE_A]})
        assert resp.status_code == 401


class TestBatchReport:
    def test_report_pending_task(self, client: TestClient, auth_headers: dict, _no_tasks: Any) -> None:
        client.post(
            "/api/v1/sim/scenarios/batch",
            json={"task_id": "task-r1", "scenes": [_SCENE_A, _SCENE_B]},
            headers=auth_headers,
        )
        resp = client.get("/api/v1/sim/scenarios/task-r1/report", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "pending"
        assert data["total_scenes"] == 2
        assert data["evaluated"] == 0

    def test_report_after_executor(self, client: TestClient, auth_headers: dict, _no_tasks: Any) -> None:
        """注入执行器后（BackgroundTasks 在 TestClient 中同步执行），报告含通过率。"""

        def _executor(task: dict[str, Any]) -> None:
            task["status"] = "completed"
            task["results"] = [
                {"scene_id": "scene-a", "passed": True, "grade": "S"},
                {"scene_id": "scene-b", "passed": False, "grade": "D"},
            ]

        _no_tasks.state.scenario_batch_executor = _executor
        client.post(
            "/api/v1/sim/scenarios/batch",
            json={"task_id": "task-r2", "scenes": [_SCENE_A, _SCENE_B]},
            headers=auth_headers,
        )
        resp = client.get("/api/v1/sim/scenarios/task-r2/report", headers=auth_headers)
        data = resp.json()["data"]
        assert data["status"] == "completed"
        assert data["evaluated"] == 2
        assert data["pass_rate"] == 0.5
        assert data["failed_scenes"] == ["scene-b"]

    def test_report_unknown_task_404(self, client: TestClient, auth_headers: dict, _no_tasks: Any) -> None:
        resp = client.get("/api/v1/sim/scenarios/nope/report", headers=auth_headers)
        assert resp.status_code == 404
