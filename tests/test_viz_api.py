"""可视化客户端端点测试（V3.1.0）。

覆盖挂在控制面 ``/viz`` 下的只读视图：

- 页面与静态资产白名单（路径穿越/未登记名字一律 404）；
- ``/viz/api/status`` 汇总运行态，且服务替身缺少 ``runtime_status`` 时能降级；
- 事件/传感器/单流帧（RGB 原样、深度 PNG、点云与 IMU JSON）与错误码；
- 日志快照过滤与 SSE 增量（``text/event-stream``）；
- ``/viz`` 端点不进 OpenAPI（平台契约零改动，V3.0.0 §16.1 单一控制面）。
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import pytest
from fastapi.testclient import TestClient

from hunter_sim.platform_adapter.app import create_app
from hunter_sim.sensor_sim.frame_hub import Frame
from hunter_sim.visualization.logbus import LogBus


def _record(instance_id: str = "inst-1", status: str = "running") -> Any:
    """实例记录替身（``_require_instance`` 只判空，降级视图需 ``to_view``）。"""
    record = SimpleNamespace(instance_id=instance_id, status=status)
    record.to_view = lambda: {"instance_id": instance_id, "status": status}
    return record


def _frame(stream: str, payload: bytes, meta: Optional[dict[str, Any]] = None) -> Frame:
    return Frame(stream=stream, ts=time.time(), seq=7, payload=payload, meta=meta or {})


class _ReadOnlyService:
    """只实现查询方法的替身：验证可视化端点对缺失只读能力的降级。"""

    def __init__(self) -> None:
        self.record = _record()

    def get(self, instance_id: str) -> Any:
        return self.record if instance_id == self.record.instance_id else None

    def list(self, scene_id: Optional[str] = None) -> list[Any]:
        return [self.record]


class _FakeService(_ReadOnlyService):
    """带事件/传感器/ROS/运行态的真实数据替身。"""

    def __init__(self) -> None:
        super().__init__()
        self.frames: dict[str, Frame] = {}
        self.event_items: list[dict[str, Any]] = [
            {"event_type": "collision", "event_level": "critical", "time_stamp": 1.5, "description": "碰撞"},
            {"event_type": "collision", "event_level": "critical", "time_stamp": 2.5, "description": "再次碰撞"},
            {"event_type": "lane_invasion", "event_level": "info", "time_stamp": 3.5, "description": "压线"},
        ]
        self.sensor_body: dict[str, Any] = {
            "attached": True,
            "counts": {"rgb": 3},
            "streams": {"rgb": {"count": 3, "fps": 10.0, "frame": None}},
        }
        self.ros_body: dict[str, Any] = {
            "enabled": True,
            "topics": {"/carla/lidar_points": 5},
        }
        self.runtime_calls = 0

    def events(self, instance_id: str) -> list[dict[str, Any]]:
        return list(self.event_items)

    def sensors(self, instance_id: str) -> dict[str, Any]:
        return dict(self.sensor_body)

    def ros(self, instance_id: str) -> dict[str, Any]:
        return dict(self.ros_body)

    def sensor_frame(self, instance_id: str, stream: str) -> Optional[Frame]:
        return self.frames.get(stream)

    def runtime_status(self) -> dict[str, Any]:
        self.runtime_calls += 1
        return {
            "instances": {"total": 1, "active": 1},
            "active_instance": {"instance_id": self.record.instance_id, "status": "running"},
            "carla": {"endpoint": "127.0.0.1:2000", "connected": True},
            "platform": {"enabled": False, "gateway_url": "", "kafka_bootstrap": "localhost:9092"},
            "sensor": self.sensor_body,
            "ros": self.ros_body,
        }


@pytest.fixture
def service() -> _FakeService:
    return _FakeService()


@pytest.fixture
def client(service: _FakeService) -> Iterator[TestClient]:
    with TestClient(create_app(service=service)) as test_client:
        yield test_client


class TestPageAndAssets:
    def test_viz_page_served(self, client: TestClient) -> None:
        response = client.get("/viz")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "HunterSim 仿真可视化" in response.text

    def test_assets_are_whitelisted(self, client: TestClient) -> None:
        script = client.get("/viz/assets/app.js")
        style = client.get("/viz/assets/style.css")

        assert script.status_code == 200
        assert "javascript" in script.headers["content-type"]
        assert "refreshStatus" in script.text
        assert style.status_code == 200
        assert "text/css" in style.headers["content-type"]

    def test_unlisted_asset_is_404(self, client: TestClient) -> None:
        assert client.get("/viz/assets/secret.txt").status_code == 404
        assert client.get("/viz/assets/..%2F..%2Fpyproject.toml").status_code == 404

    def test_viz_is_not_in_openapi(self, client: TestClient) -> None:
        """可视化端点不进入平台 OpenAPI 契约（单一控制面口径）。"""
        paths = client.get("/openapi.json").json()["paths"]

        assert [path for path in paths if path.startswith("/viz")] == []


class TestStatus:
    def test_runtime_snapshot_is_merged(self, client: TestClient, service: _FakeService) -> None:
        body = client.get("/viz/api/status").json()

        assert service.runtime_calls >= 1
        assert isinstance(body["manager"]["port"], int)
        assert body["manager"]["port"] > 0
        assert body["carla"]["connected"] is True
        assert body["instances"] == {"total": 1, "active": 1}
        assert body["sensor"]["attached"] is True
        assert body["log"]["capacity"] > 0
        assert isinstance(body["uptime_s"], (int, float))

    def test_missing_runtime_status_degrades(self) -> None:
        with TestClient(create_app(service=_ReadOnlyService())) as client:
            body = client.get("/viz/api/status").json()

        assert body["instances"] == {"total": 1, "active": 1}
        assert body["active_instance"]["instance_id"] == "inst-1"
        assert body["carla"]["connected"] is None


class TestEvents:
    def test_counts_and_items(self, client: TestClient) -> None:
        body = client.get("/viz/api/instances/inst-1/events").json()

        assert body["count"] == 3
        assert body["counts"] == {"collision": 2, "lane_invasion": 1}
        assert [item["event_type"] for item in body["items"]][-1] == "lane_invasion"

    def test_limit_returns_tail(self, client: TestClient) -> None:
        body = client.get("/viz/api/instances/inst-1/events?limit=1").json()

        assert body["count"] == 3
        assert [item["description"] for item in body["items"]] == ["压线"]

    def test_unknown_instance_is_404(self, client: TestClient) -> None:
        assert client.get("/viz/api/instances/ghost/events").status_code == 404

    def test_service_without_events_returns_empty(self) -> None:
        with TestClient(create_app(service=_ReadOnlyService())) as client:
            body = client.get("/viz/api/instances/inst-1/events").json()

        assert body == {"instance_id": "inst-1", "count": 0, "counts": {}, "items": []}


class TestSensors:
    def test_streams_and_ros_merged(self, client: TestClient) -> None:
        body = client.get("/viz/api/instances/inst-1/sensors").json()

        assert body["attached"] is True
        assert body["streams"]["rgb"]["count"] == 3
        assert body["ros"]["enabled"] is True
        assert body["ros"]["topics"] == {"/carla/lidar_points": 5}

    def test_rgb_frame_passthrough(self, client: TestClient, service: _FakeService) -> None:
        service.frames["rgb"] = _frame(
            "rgb", b"\xff\xd8fake-jpeg", {"content_type": "image/jpeg", "width": 100, "height": 50}
        )

        response = client.get("/viz/api/instances/inst-1/sensors/rgb")

        assert response.status_code == 200
        assert response.content == b"\xff\xd8fake-jpeg"
        assert response.headers["content-type"] == "image/jpeg"

    def test_missing_frame_is_409(self, client: TestClient) -> None:
        response = client.get("/viz/api/instances/inst-1/sensors/imu")

        assert response.status_code == 409
        assert "sensor_frame_unavailable" in response.json()["detail"]

    def test_unknown_stream_is_404(self, client: TestClient) -> None:
        assert client.get("/viz/api/instances/inst-1/sensors/radar").status_code == 404

    def test_lidar_frame_is_sampled_json(self, client: TestClient, service: _FakeService) -> None:
        pytest.importorskip("numpy")
        service.frames["lidar"] = _frame(
            "lidar", struct.pack("<6f", 1.0, 2.0, 0.0, 3.0, 4.0, 0.5), {"points": 2}
        )

        body = client.get("/viz/api/instances/inst-1/sensors/lidar").json()

        assert body["total"] == 2
        assert body["sampled"] == 2
        assert body["points"][0] == [1.0, 2.0, 0.0]

    def test_imu_frame_is_parsed(self, client: TestClient, service: _FakeService) -> None:
        service.frames["imu"] = _frame("imu", b"((1.0, 2.0, 3.0), (0.1, 0.2, 0.3))")

        body = client.get("/viz/api/instances/inst-1/sensors/imu").json()

        assert body["accel"] == [1.0, 2.0, 3.0]
        assert body["gyro"] == [0.1, 0.2, 0.3]

    def test_unparsable_imu_falls_back_to_raw(self, client: TestClient, service: _FakeService) -> None:
        service.frames["imu"] = _frame("imu", b"not-a-tuple")

        body = client.get("/viz/api/instances/inst-1/sensors/imu").json()

        assert body["raw"] == "not-a-tuple"

    def test_depth_frame_renders_png(self, client: TestClient, service: _FakeService) -> None:
        pytest.importorskip("numpy")
        payload = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
        service.frames["depth"] = _frame("depth", payload, {"width": 2, "height": 2})

        response = client.get("/viz/api/instances/inst-1/sensors/depth")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")

    def test_depth_without_numpy_is_503(
        self, client: TestClient, service: _FakeService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service.frames["depth"] = _frame("depth", struct.pack("<4f", 1.0, 2.0, 3.0, 4.0), {"width": 2, "height": 2})
        monkeypatch.setitem(sys.modules, "numpy", None)  # 强制 numpy 不可用

        response = client.get("/viz/api/instances/inst-1/sensors/depth")

        assert response.status_code == 503
        assert "sensor_render_unavailable" in response.json()["detail"]


class TestLogs:
    def _bus(self, client: TestClient) -> LogBus:
        bus = client.app.state.log_bus
        assert isinstance(bus, LogBus)
        return bus

    def test_snapshot_filters_by_level_and_keyword(self, client: TestClient) -> None:
        bus = self._bus(client)
        bus.publish("DEBUG", "hunter_sim.demo", "viz-demo trace")
        bus.publish("INFO", "hunter_sim.demo", "viz-demo ready")
        bus.publish("ERROR", "hunter_sim.demo", "viz-demo boom")

        body = client.get("/viz/api/logs?level=INFO&contains=viz-demo&limit=50").json()

        assert [item["level"] for item in body["items"]] == ["INFO", "ERROR"]
        assert body["min_level"] == "INFO"
        assert body["counts"]["total"] >= 3

    def test_since_seq_resumes(self, client: TestClient) -> None:
        bus = self._bus(client)
        cursor = bus.publish("INFO", "hunter_sim.demo", "viz-demo first").seq
        bus.publish("INFO", "hunter_sim.demo", "viz-demo second")

        body = client.get(f"/viz/api/logs?level=INFO&contains=viz-demo&since_seq={cursor}").json()

        assert [item["message"] for item in body["items"]] == ["viz-demo second"]
        assert body["next_since"] == cursor + 1

    def test_limit_must_be_positive(self, client: TestClient) -> None:
        assert client.get("/viz/api/logs?limit=0").status_code == 422


class TestLogStream:
    """SSE 端点：直接驱动异步生成器，避免测试等待 keep-alive。"""

    class _Request:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def is_disconnected(self) -> bool:
            return False

    def _endpoint(self, app: Any) -> Any:
        for route in app.routes:
            if getattr(route, "path", "") == "/viz/api/logs/stream":
                return route.endpoint
        raise AssertionError("log stream route not registered")

    async def test_stream_replays_history_then_pushes(self) -> None:
        service = _FakeService()
        app = create_app(service=service)
        bus = LogBus(max_records=10)
        app.state.adapter_service = service
        app.state.log_bus = bus
        app.state.started_at = time.time()
        request = self._Request(app)
        endpoint = self._endpoint(app)

        response = await endpoint(request=request, level="INFO", contains="", history=10)
        bus.publish("ERROR", "hunter_sim.demo", "viz-stream boom")

        assert response.media_type == "text/event-stream"
        iterator = response.body_iterator
        try:
            assert await asyncio.wait_for(iterator.__anext__(), 1.0) == b": connected\n\n"
            chunk = await asyncio.wait_for(iterator.__anext__(), 1.0)
        finally:
            await iterator.aclose()

        record = json.loads(chunk.decode("utf-8").removeprefix("data: "))
        assert record["message"] == "viz-stream boom"
        assert record["level"] == "ERROR"
