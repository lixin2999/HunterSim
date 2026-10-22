"""API 集成测试（PROMPT-TEST-001 / PROMPT-API-001）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestHealthEndpoints:
    """健康检查端点测试（无需认证）。"""

    def test_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sim/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        assert "status" in data
        # 2.1.2：降级原因可直接从响应读取，无需翻启动日志
        assert isinstance(data["carla_sdk_installed"], bool)
        assert "carla_probe_error" in data

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

    def test_metrics_exports_monitored_gauges(self, client: TestClient) -> None:
        """§15.4 告警监控项：服务注册实时值后 /metrics 输出对应 gauge。

        V2.6.0：/metrics 改由标准 prometheus_client 导出（浮点按最小精度
        序列化），因此按行解析样本值比对，不再依赖手工拼接的固定小数格式。
        """
        client.app.state.vil_latency_ms = 420.0
        client.app.state.simulation_fps = 18.5

        def _gauge_value(body: str, name: str) -> float:
            for line in body.splitlines():
                if line.startswith(name + " "):
                    return float(line.split(" ", 1)[1])
            raise AssertionError(f"metric {name} not exported")

        try:
            body = client.get("/api/v1/sim/metrics").text
            assert _gauge_value(body, "huntersim_vil_latency_ms") == 420.0
            assert _gauge_value(body, "huntersim_simulation_fps") == 18.5
        finally:
            client.app.state.vil_latency_ms = None
            client.app.state.simulation_fps = None

    def test_performance_targets(self, client: TestClient) -> None:
        """§13.1 性能目标与 §15.4 告警阈值查询端点（免鉴权）。"""
        resp = client.get("/api/v1/sim/health/performance-targets")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["performance_targets"]["simulation_fps"]["target"] == ">= 30"
        assert data["performance_targets"]["vil_end_to_end_latency"]["unit"] == "ms"
        assert data["alert_thresholds"]["carla_rpc_response_time"] == "> 5s"
        assert data["alert_thresholds"]["disk_usage"] == "> 85%"


class TestCarlaProbeDiagnostics:
    """CARLA 连通性诊断（2.1.2）：/health 输出失败目标与异常摘要。"""

    def test_health_exposes_probe_error_from_state(self) -> None:
        """独立 app 实例：首个巡检周期（默认 10s）前注入错误，避免与后台线程竞争。"""
        from hunter_sim.api.main import create_app
        from hunter_sim.common.models import HunterSimSettings

        app = create_app(HunterSimSettings(env="dev"))
        with TestClient(app) as c:
            app.state.carla_probe_error = "127.0.0.1:2000 不可达：ConnectionRefusedError: refused"
            data = c.get("/api/v1/sim/health").json()["data"]
            assert data["carla_probe_error"].startswith("127.0.0.1:2000")
            assert isinstance(data["carla_sdk_installed"], bool)

    def test_probe_success_clears_error(self) -> None:
        from types import SimpleNamespace

        from hunter_sim.api.main import _refresh_carla_state
        from hunter_sim.common.models import HunterSimSettings

        class _HealthyClient:
            def get_server_version(self) -> str:
                return "0.9.16"

        state = SimpleNamespace(
            carla_client=_HealthyClient(),
            carla_sdk_installed=True,
            carla_connected=False,
            carla_probe_error="旧错误",
            carla_probe_evaluated=False,
        )
        _refresh_carla_state(state, HunterSimSettings())
        assert state.carla_connected is True
        assert state.carla_probe_error is None

    def test_probe_failure_records_target_and_exception(self) -> None:
        from types import SimpleNamespace

        from hunter_sim.api.main import _refresh_carla_state
        from hunter_sim.common.models import CarlaSettings, HunterSimSettings

        class _BrokenClient:
            def get_server_version(self) -> str:
                raise RuntimeError("connect timeout")

        state = SimpleNamespace(
            carla_client=_BrokenClient(),
            carla_sdk_installed=True,
            carla_connected=False,
            carla_probe_error=None,
            carla_probe_evaluated=False,
        )
        settings = HunterSimSettings(carla=CarlaSettings(host="10.0.0.5", rpc_port=2100))
        _refresh_carla_state(state, settings)
        assert state.carla_connected is False
        assert "10.0.0.5:2100" in state.carla_probe_error
        assert "RuntimeError" in state.carla_probe_error

    def test_probe_without_sdk_hints_install(self) -> None:
        from types import SimpleNamespace

        from hunter_sim.api.main import _refresh_carla_state
        from hunter_sim.common.models import HunterSimSettings

        state = SimpleNamespace(
            carla_client=None,
            carla_sdk_installed=False,
            carla_connected=False,
            carla_probe_error=None,
            carla_probe_evaluated=False,
        )
        _refresh_carla_state(state, HunterSimSettings())
        assert state.carla_connected is False
        assert "SDK" in state.carla_probe_error


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


_VALID_XODR = """<?xml version="1.0"?>
<OpenDRIVE>
  <header revHeader="1.6" name="CustomTown">
    <geoReference><![CDATA[[+proj=tmerc]]]></geoReference>
  </header>
  <road id="1" name="R1" length="100.0" junction="-1">
    <lanes>
      <laneSection id="0">
        <lane id="-1" type="driving"/>
      </laneSection>
    </lanes>
  </road>
</OpenDRIVE>
"""


class TestMapUploadEndpoint:
    """自定义地图上传端点测试（设计文档 §10.4.2）。"""

    def test_upload_valid_map_and_list(self, client: TestClient, auth_headers: dict, tmp_path) -> None:
        client.app.state.resources_dir = str(tmp_path)
        resp = client.post(
            "/api/v1/sim/maps/upload",
            json={"map_id": "myTown", "content": _VALID_XODR},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["map_id"] == "myTown"
        assert data["is_custom"] is True
        assert data["road_count"] == 1
        assert data["reference"] == "custom/myTown"  # 附录 B：创建实例时引用名
        assert data["review_status"] == "approved"  # §14.3 格式审核通过
        assert (tmp_path / "maps" / "custom" / "myTown.xodr").exists()

        # 自定义地图应出现在地图列表中，引用名为 custom/{map_name}（附录 B）
        resp2 = client.get("/api/v1/sim/maps", headers=auth_headers)
        maps = {m["map_id"]: m for m in resp2.json()["data"]["maps"]}
        assert maps["custom/myTown"]["is_custom"] is True
        assert maps["custom/myTown"]["name"] == "myTown"

    def test_upload_multipart_form_data(self, client: TestClient, auth_headers: dict, tmp_path) -> None:
        """附录 B 主接口形式：multipart/form-data（file + map_id 字段）。"""
        client.app.state.resources_dir = str(tmp_path)
        resp = client.post(
            "/api/v1/sim/maps/upload",
            files={"file": ("park.xodr", _VALID_XODR, "application/xml")},
            data={"map_id": "park"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["map_id"] == "park"
        assert data["reference"] == "custom/park"
        assert (tmp_path / "maps" / "custom" / "park.xodr").exists()

    def test_upload_multipart_map_id_from_filename(
        self, client: TestClient, auth_headers: dict, tmp_path
    ) -> None:
        """multipart 未提供 map_id 时从文件名推导。"""
        client.app.state.resources_dir = str(tmp_path)
        resp = client.post(
            "/api/v1/sim/maps/upload",
            files={"file": ("garage_v2.xodr", _VALID_XODR, "application/xml")},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["map_id"] == "garage_v2"

    def test_upload_multipart_missing_file_422(self, client: TestClient, auth_headers: dict) -> None:
        # 仅含普通字段的 multipart 请求（无 file 文件部分）
        resp = client.post(
            "/api/v1/sim/maps/upload",
            files={"map_id": (None, "nope")},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_upload_form_urlencoded_rejected(self, client: TestClient, auth_headers: dict) -> None:
        """既非 multipart 也非 JSON 的请求体 → 400。"""
        resp = client.post(
            "/api/v1/sim/maps/upload",
            data={"map_id": "nope"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_upload_invalid_xodr_rejected(self, client: TestClient, auth_headers: dict, tmp_path) -> None:
        client.app.state.resources_dir = str(tmp_path)
        resp = client.post(
            "/api/v1/sim/maps/upload",
            json={"map_id": "badTown", "content": "<OpenDRIVE><unclosed>"},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        # 非法文件不应落盘
        assert not (tmp_path / "maps" / "custom" / "badTown.xodr").exists()

    def test_upload_path_traversal_map_id_rejected(
        self, client: TestClient, auth_headers: dict, tmp_path
    ) -> None:
        client.app.state.resources_dir = str(tmp_path)
        resp = client.post(
            "/api/v1/sim/maps/upload",
            json={"map_id": "../evil", "content": _VALID_XODR},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_upload_requires_auth(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/sim/maps/upload",
            json={"map_id": "town", "content": _VALID_XODR},
        )
        assert resp.status_code == 401

    def test_upload_requires_admin(self, client: TestClient, user_headers: dict, tmp_path) -> None:
        """审查项 E：非管理员上传 → 403（§14.2 资源入库需管理员）。"""
        client.app.state.resources_dir = str(tmp_path)
        resp = client.post(
            "/api/v1/sim/maps/upload",
            json={"map_id": "town", "content": _VALID_XODR},
            headers=user_headers,
        )
        assert resp.status_code == 403

    def test_upload_map_id_trailing_newline_rejected(
        self, client: TestClient, auth_headers: dict, tmp_path
    ) -> None:
        r"""审查项 E：fullmatch 拒绝尾换行绕过（^...$ + match 可被 'evil\n' 穿透）。"""
        client.app.state.resources_dir = str(tmp_path)
        resp = client.post(
            "/api/v1/sim/maps/upload",
            json={"map_id": "evil\n", "content": _VALID_XODR},
            headers=auth_headers,
        )
        assert resp.status_code == 422


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
