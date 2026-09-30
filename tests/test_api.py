"""API 集成测试（PROMPT-TEST-001 / PROMPT-API-001）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class TestHealthEndpoints:
    """健康检查端点测试（无需认证）。"""

    def test_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sim/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert "status" in body["data"]

    def test_liveness_returns_alive(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sim/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

    def test_readiness_without_carla(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sim/health/ready")
        # CARLA 未连接时仍应返回 200（降级状态）
        assert resp.status_code == 200

    def test_metrics_endpoint_no_auth(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sim/metrics")
        assert resp.status_code == 200
        body = resp.text
        assert "huntersim_uptime_seconds" in body
        assert "huntersim_carla_connected" in body
        assert "huntersim_active_instances" in body


class TestAuthEndpoints:
    """认证相关测试。"""

    def test_instances_without_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sim/instances")
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 401

    def test_invalid_token_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/sim/instances",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401

    def test_valid_token_returns_200(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.get("/api/v1/sim/instances", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        assert "instances" in body["data"]


class TestInstanceEndpoints:
    """实例管理端点测试。"""

    def test_create_instance_no_body(self, client: TestClient, auth_headers: dict) -> None:
        """缺少必填字段时返回 422。"""
        resp = client.post("/api/v1/sim/instances", json={}, headers=auth_headers)
        assert resp.status_code == 422

    def test_get_nonexistent_instance(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.get("/api/v1/sim/instances/nonexistent-id", headers=auth_headers)
        assert resp.status_code == 404

    def test_list_empty_instances(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.get("/api/v1/sim/instances", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 0


class TestResourceEndpoints:
    """资源查询端点测试。"""

    def test_list_maps(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.get("/api/v1/sim/maps", headers=auth_headers)
        assert resp.status_code == 200
        maps = resp.json()["data"]["maps"]
        assert len(maps) >= 1
        assert any(m["map_id"] == "Town03" for m in maps)

    def test_list_environments(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.get("/api/v1/sim/environments", headers=auth_headers)
        assert resp.status_code == 200
        presets = resp.json()["data"]["presets"]
        names = [p["preset_name"] for p in presets]
        assert "sunny_noon" in names
        assert "night" in names
        assert len(presets) == 8

    def test_gpu_status(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.get("/api/v1/sim/resources/gpu", headers=auth_headers)
        assert resp.status_code == 200


class TestApiDocEndpoint:
    """API 文档端点测试。"""

    def test_swagger_ui_accessible(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sim/docs")
        assert resp.status_code == 200

    def test_openapi_json_valid(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sim/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert "info" in schema
        assert schema["info"]["title"] == "HunterSim API"
