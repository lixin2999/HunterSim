"""实例管理路由完整生命周期测试（PROMPT-TEST-001）。

覆盖设计文档 §12.1/§12.2 接口与 §14.2 安全要求：
创建/销毁需管理员角色、单用户并发配额、车辆模型白名单（§14.3）。
"""

from __future__ import annotations

from typing import Any, Generator

import pytest
from fastapi.testclient import TestClient

from hunter_sim.common.models import InstanceStatus


@pytest.fixture(autouse=True)
def _cleanup_instances(client: TestClient) -> Generator[None, None, None]:
    """每个测试后销毁全部实例，释放 GPU 容量与用户配额名额。"""
    yield
    app = client.app
    mgr = app.state.instance_manager
    quota = getattr(app.state, "quota_manager", None)
    for inst in mgr.list_instances():
        mgr.destroy_instance(inst.sim_instance_id)
        if quota is not None and inst.user_id:
            quota.release(inst.user_id)
    app.state.scene_runners = {}


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


class _FakeWeather:
    def __init__(self) -> None:
        self.preset_called = None

    def apply_preset(self, name: str, transition_seconds: float = 0.0) -> None:
        self.preset_called = name


class _FakeCoord:
    def __init__(self) -> None:
        self.calibration = None

    def update_calibration(self, params: Any) -> None:
        self.calibration = params


class _InstanceFakeRunner:
    """最小场景运行器桩，驱动 /instances/{id} 下的场景类端点。"""

    def __init__(self) -> None:
        self.loaded: list = []
        self.weather_manager = _FakeWeather()
        self.coordinate_transformer = _FakeCoord()
        self.screenshot = None

    def load_scene(self, config: Any) -> None:
        self.loaded.append(config)

    def get_latest_screenshot(self):
        return self.screenshot


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


class TestDocInterfaces:
    """设计文档 §12.1/§12.2 标准接口测试。"""

    def test_create_doc_format(self, client: TestClient, auth_headers: dict) -> None:
        """文档风格请求（map/Medium/vehicle_model）与 §12.2 响应结构。"""
        resp = client.post(
            "/api/v1/sim/instances",
            json={
                "mode": "vil",
                "map": "Town03",
                "vehicle_model": "hunter.se",
                "quality": "Medium",
                "vehicle_id": "HUNTER-001",
                "scene_id": "scene_001",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["sim_instance_id"]
        assert data["carla_server"] == {
            "host": "127.0.0.1", "rpc_port": 2000, "stream_port": 2001,
        }
        assert data["stream_url"].startswith("webrtc://")
        assert data["stream_url"].endswith(data["sim_instance_id"])

    def test_create_with_replay_config(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.post(
            "/api/v1/sim/instances",
            json={
                "mode": "replay",
                "map": "Town03",
                "replay_config": {
                    "vehicle_id": "HUNTER-001",
                    "start_time": "2026-08-19T10:00:00Z",
                    "end_time": "2026-08-19T10:05:00Z",
                },
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201

    def test_instance_status_model(self, client: TestClient, auth_headers: dict) -> None:
        """GET /instances/{id}/status 返回 §10.2.2 数据模型。"""
        iid = _create(client, auth_headers, mode="vil")
        resp = client.get(f"/api/v1/sim/instances/{iid}/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["sim_instance_id"] == iid
        assert data["mode"] == "vil"
        assert data["resources"]["memory_limit"] == "8Gi"
        assert "carla_server" in data

    def test_stream_endpoint(self, client: TestClient, auth_headers: dict) -> None:
        iid = _create(client, auth_headers)
        resp = client.get(f"/api/v1/sim/instances/{iid}/stream", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["protocol"] == "webrtc"
        assert body["stream_url"].endswith(f"/sim/{iid}")

    def test_stream_missing_404(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.get("/api/v1/sim/instances/no-such/stream", headers=auth_headers)
        assert resp.status_code == 404


class TestInstanceSceneOps:
    """/instances/{id} 下的场景/天气/标定端点（§12.1/§12.3/§12.4）。"""

    def _inject_runner(self, app: Any, iid: str) -> Any:
        fake = _InstanceFakeRunner()
        app.state.scene_runners = {iid: fake}
        return fake

    def test_assign_scene(self, client: TestClient, auth_headers: dict, app: Any) -> None:
        iid = _create(client, auth_headers)
        fake = self._inject_runner(app, iid)
        resp = client.post(
            f"/api/v1/sim/instances/{iid}/scene",
            json={
                "scene_id": "scene-001",
                "scene_config": {
                    "scene_id": "scene-001",
                    "scene_name": "测试场景",
                    "map_id": "Town05",
                    "ego_vehicle": {"spawn_point": {"x": 1.0, "y": 2.0, "yaw_deg": 90.0}},
                },
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["scene_id"] == "scene-001"
        assert len(fake.loaded) == 1
        app.state.scene_runners = {}

    def test_assign_scene_id_mismatch_422(
        self, client: TestClient, auth_headers: dict, app: Any
    ) -> None:
        iid = _create(client, auth_headers)
        self._inject_runner(app, iid)
        resp = client.post(
            f"/api/v1/sim/instances/{iid}/scene",
            json={
                "scene_id": "other-id",
                "scene_config": {
                    "scene_id": "scene-001",
                    "scene_name": "n",
                    "map_id": "Town05",
                    "ego_vehicle": {"spawn_point": {"x": 1.0, "y": 2.0, "yaw_deg": 0.0}},
                },
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        app.state.scene_runners = {}

    def test_weather_via_instances_path(
        self, client: TestClient, auth_headers: dict, app: Any
    ) -> None:
        iid = _create(client, auth_headers)
        fake = self._inject_runner(app, iid)
        resp = client.post(
            f"/api/v1/sim/instances/{iid}/weather",
            json={"preset": "sunny_noon"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert fake.weather_manager.preset_called == "sunny_noon"
        app.state.scene_runners = {}

    def test_vil_calibrate(self, client: TestClient, auth_headers: dict, app: Any) -> None:
        iid = _create(client, auth_headers, mode="vil")
        fake = self._inject_runner(app, iid)
        resp = client.post(
            f"/api/v1/sim/instances/{iid}/vil/calibrate",
            json={"map_x": 100.5, "map_y": 50.2, "map_heading": 90.0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        params = fake.coordinate_transformer.calibration
        assert params.x0 == 100.5
        assert round(params.yaw0, 4) == -1.5708
        app.state.scene_runners = {}

    def test_screenshot_via_instances_path(
        self, client: TestClient, auth_headers: dict, app: Any
    ) -> None:
        iid = _create(client, auth_headers)
        fake = self._inject_runner(app, iid)
        fake.screenshot = "BASE64DATA"
        resp = client.get(f"/api/v1/sim/instances/{iid}/screenshot", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["image_base64"] == "BASE64DATA"
        app.state.scene_runners = {}


class TestSecurityInterfaces:
    """设计文档 §14.2 接口安全：权限控制与资源配额；§14.3 资源白名单。"""

    def test_create_requires_admin(self, client: TestClient, user_headers: dict) -> None:
        """非管理员创建实例 → 403。"""
        resp = client.post(
            "/api/v1/sim/instances",
            json={"mode": "sil", "map": "Town03"},
            headers=user_headers,
        )
        assert resp.status_code == 403

    def test_destroy_requires_admin(self, client: TestClient, auth_headers: dict, user_headers: dict) -> None:
        """非管理员销毁实例 → 403；管理员正常销毁。"""
        iid = _create(client, auth_headers)
        resp = client.delete(f"/api/v1/sim/instances/{iid}", headers=user_headers)
        assert resp.status_code == 403
        assert client.delete(f"/api/v1/sim/instances/{iid}", headers=auth_headers).status_code == 200

    def test_user_quota_limit(self, client: TestClient, auth_headers: dict, app: Any) -> None:
        """单用户并发配额默认 5（§14.2），超额创建返回 503。"""
        quota = app.state.quota_manager
        assert quota.get_usage("test_user")["max"] == 5
        for _ in range(5):
            resp = client.post(
                "/api/v1/sim/instances",
                json={"mode": "sil", "map": "Town03", "quality": "low"},
                headers=auth_headers,
            )
            assert resp.status_code == 201
        resp6 = client.post(
            "/api/v1/sim/instances",
            json={"mode": "sil", "map": "Town03", "quality": "low"},
            headers=auth_headers,
        )
        assert resp6.status_code == 503
        assert "max 5" in resp6.text.lower() or "quota" in resp6.text.lower()

    def test_destroy_releases_quota(self, client: TestClient, auth_headers: dict, app: Any) -> None:
        """销毁实例后配额名额释放，可再次创建。"""
        quota = app.state.quota_manager
        iid = _create(client, auth_headers)
        assert quota.get_usage("test_user")["user_count"] == 1
        client.delete(f"/api/v1/sim/instances/{iid}", headers=auth_headers)
        assert quota.get_usage("test_user")["user_count"] == 0

    def test_vehicle_model_whitelist(self, client: TestClient, auth_headers: dict) -> None:
        """非白名单车辆模型 → 422（§14.3 资源白名单）。"""
        resp = client.post(
            "/api/v1/sim/instances",
            json={"mode": "sil", "map": "Town03", "vehicle_model": "vehicle.evil_car"},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert "whitelist" in resp.text
