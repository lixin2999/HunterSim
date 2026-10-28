"""控制平面端点测试（8 个平台端点 + 健康检查 + 404/503）。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from hunter_sim.platform_adapter.app import create_app
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.models import (
    CreateInstanceRequest,
    InstanceRecord,
    SubmitScenarioRequest,
)
from hunter_sim.platform_adapter.store import InstanceStore


class _FakeService:
    """端点测试用编排替身（返回真实 ``InstanceRecord`` 视图）。"""

    def __init__(self) -> None:
        self.record = InstanceRecord(
            instance_id="inst-1",
            sim_id="sim-42",
            scene_id="scene-1",
            scene_name="测试场景",
            total_time_s=30.0,
            actors_count=2,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self.calls: list[tuple[str, Any]] = []

    def create(self, request: CreateInstanceRequest) -> InstanceRecord:
        self.calls.append(("create", request))
        self.record.sim_id = request.resolved_sim_id or self.record.sim_id
        self.record.scene_id = request.scene_id
        return self.record

    def submit(
        self, instance_id: str, request: SubmitScenarioRequest
    ) -> Optional[InstanceRecord]:
        self.calls.append(("submit", request))
        return self.record if instance_id == self.record.instance_id else None

    def get(self, instance_id: str) -> Optional[InstanceRecord]:
        return self.record if instance_id == self.record.instance_id else None

    def list(self, scene_id: Optional[str] = None) -> list[InstanceRecord]:
        self.calls.append(("list", scene_id))
        if scene_id and scene_id != self.record.scene_id:
            return []
        return [self.record]

    def result(self, instance_id: str) -> Optional[InstanceRecord]:
        return self.get(instance_id)

    def pause(self, instance_id: str) -> Optional[InstanceRecord]:
        return self.get(instance_id)

    def resume(self, instance_id: str) -> Optional[InstanceRecord]:
        return self.get(instance_id)

    def stop(self, instance_id: str) -> Optional[InstanceRecord]:
        return self.get(instance_id)

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "carla_connected": True,
            "carla_probe_error": None,
            "active_instance": {"instance_id": self.record.instance_id},
            "vil_state": None,
            "sensor": {"recording": False, "recording_bytes": 0, "streams": {}},
            "cloud": {"enabled": False},
        }

    def runtime_gauges(self) -> dict[str, Any]:
        return {"active_instances": 1.0, "carla_connected": True, "disk_usage_ratio": 0.25}


@pytest.fixture
def service() -> _FakeService:
    return _FakeService()


@pytest.fixture
def client(service: _FakeService) -> TestClient:
    with TestClient(create_app(service=service)) as test_client:
        yield test_client


class TestCreateAndQuery:
    def test_health(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        for key in (
            "carla_connected",
            "carla_probe_error",
            "active_instance",
            "vil_state",
            "sensor",
            "cloud",
        ):
            assert key in body, f"missing health field: {key}"
        assert body["active_instance"]["instance_id"] == "inst-1"

    def test_metrics_endpoint_exposes_registry(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "huntersim_active_instances" in response.text
        assert "huntersim_http_request_duration_seconds" in response.text

    def test_legacy_8080_business_paths_are_gone(self, client: TestClient) -> None:
        """V3.0.0：8080 治理面（用户/实例库/场景库/审计）已移除。"""
        for path in (
            "/api/v1/sim/users",
            "/api/v1/sim/scenes",
            "/api/v1/sim/auth/login",
            "/api/v1/sim/instances/inst-1/logs",
        ):
            assert client.get(path).status_code == 404

    def test_create_echoes_platform_sim_id(
        self, client: TestClient, service: _FakeService
    ) -> None:
        response = client.post(
            "/api/v1/instances",
            json={
                "scene_id": "scene-1",
                "scene_name": "测试场景",
                "sim_id": "sim-platform-1",
                "scene_config": {"map": {"map_id": "Town03"}},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["sim_id"] == "sim-platform-1"
        assert body["sim_instance_id"] == body["instance_id"] == body["id"] == "inst-1"
        assert body["status"] == "pending"
        assert body["carla_status"] == "CREATED"
        assert service.calls[0][0] == "create"

    def test_list_empty_returns_items(self, client: TestClient) -> None:
        response = client.get("/api/v1/instances", params={"scene_id": "missing"})
        assert response.status_code == 200
        assert response.json() == {"items": []}

    def test_list_forwards_scene_id(
        self, client: TestClient, service: _FakeService
    ) -> None:
        response = client.get("/api/v1/instances", params={"scene_id": "scene-1"})
        assert response.status_code == 200
        assert [item["instance_id"] for item in response.json()["items"]] == ["inst-1"]
        assert service.calls[-1] == ("list", "scene-1")

    def test_list_without_query_returns_all(self, client: TestClient) -> None:
        response = client.get("/api/v1/instances")
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1

    def test_get_instance_view_timestamps_are_iso(self, client: TestClient) -> None:
        response = client.get("/api/v1/instances/inst-1")
        assert response.status_code == 200
        body = response.json()
        # 平台用 datetime.fromisoformat 解析这些字段
        assert isinstance(datetime.fromisoformat(body["created_at"]), datetime)
        assert isinstance(datetime.fromisoformat(body["updated_at"]), datetime)

    def test_get_unknown_instance_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/instances/missing")
        assert response.status_code == 404


class TestScenarioAndResult:
    def test_submit_scenario(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/instances/inst-1/scenario",
            json={"scene_config": {"map": {"map_id": "Town03"}}},
        )
        assert response.status_code == 200
        assert response.json()["instance_id"] == "inst-1"

    def test_submit_unknown_instance_returns_404(self, client: TestClient) -> None:
        response = client.post("/api/v1/instances/missing/scenario", json={})
        assert response.status_code == 404

    def test_result_view(self, client: TestClient, service: _FakeService) -> None:
        service.record.result = {
            "success": True,
            "success_criteria_result": {"speed": True},
            "evaluation": {"grade": "A"},
            "artifacts": [{"name": "eval.json", "object_key": "k/eval.json"}],
        }
        response = client.get("/api/v1/instances/inst-1/result")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["success_criteria_result"] == {"speed": True}
        assert body["evaluation"] == {"grade": "A"}
        assert body["artifacts"] == [{"name": "eval.json", "object_key": "k/eval.json"}]
        assert body["failure_reason"] is None

    def test_result_non_terminal_returns_current_status(
        self, client: TestClient, service: _FakeService
    ) -> None:
        service.record.status = "pending"
        response = client.get("/api/v1/instances/inst-1/result")
        assert response.status_code == 200
        assert response.json()["status"] == "pending"

    def test_result_unknown_instance_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/instances/missing/result")
        assert response.status_code == 404


class TestControls:
    @pytest.mark.parametrize("action", ["pause", "resume", "stop"])
    def test_control_success(self, client: TestClient, action: str) -> None:
        response = client.post(f"/api/v1/instances/inst-1/{action}")
        assert response.status_code == 200
        body = response.json()
        assert body["sim_instance_id"] == body["instance_id"] == body["id"] == "inst-1"
        assert body["status"] == "pending"
        assert body["carla_status"] == "CREATED"

    @pytest.mark.parametrize("action", ["pause", "resume", "stop"])
    def test_control_unknown_instance_returns_404(
        self, client: TestClient, action: str
    ) -> None:
        response = client.post(f"/api/v1/instances/missing/{action}")
        assert response.status_code == 404

    def test_service_not_ready_returns_503(self, client: TestClient) -> None:
        client.app.state.adapter_service = None  # type: ignore[attr-defined]
        response = client.get("/api/v1/instances/inst-1")
        assert response.status_code == 503


class _FakeExecutor:
    """生命周期测试用执行器（只记录 start/stop）。"""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class TestLifespan:
    def test_restart_recovery_marks_non_terminal_failed(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "adapter.db")
        store = InstanceStore(db_path).connect()
        stale = InstanceRecord(
            instance_id="inst-old",
            sim_id="sim-old",
            scene_id="scene-old",
            status="pending",
            carla_status="LOADING",
        )
        store.insert(stale)
        executor = _FakeExecutor()
        app = create_app(
            adapter_settings=AdapterSettings(
                db_path=db_path, artifacts_dir=str(tmp_path / "artifacts")
            ),
            store=store,
            executor=executor,  # type: ignore[arg-type]
            plane=object(),  # type: ignore[arg-type]
        )

        with TestClient(app):
            recovered = store.get("inst-old")
            assert recovered is not None
            assert recovered.status == "failed"
            assert recovered.carla_status == "FAILED"
            assert recovered.message == "adapter restarted"
            assert recovered.finished_at is not None
            assert executor.started == 1
            assert app.state.adapter_service is not None

        assert executor.stopped == 1
