"""统一异常处理单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Callable

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from starlette.testclient import TestClient

from hunter_sim.api.error_handlers import register_exception_handlers
from hunter_sim.common.exceptions import (
    CarlaConnectionError,
    CarlaSimulationError,
    ConfigurationError,
    HunterSimError,
    InstanceStateError,
    KafkaConnectionError,
    ResourceError,
    ROS2ConnectionError,
    SensorSimulationError,
    SimulationError,
    SimTimeoutError,
    ValidationError,
)


class _Body(BaseModel):
    count: int


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    raisers: dict[str, Callable[[], None]] = {
        "carla_conn": lambda: (_ for _ in ()).throw(CarlaConnectionError("127.0.0.1", 2000, "refused")),
        "kafka_conn": lambda: (_ for _ in ()).throw(KafkaConnectionError("localhost:9092", "down")),
        "ros2_conn": lambda: (_ for _ in ()).throw(ROS2ConnectionError("node1", "no daemon")),
        "config": lambda: (_ for _ in ()).throw(ConfigurationError("load", "bad")),
        "validation": lambda: (_ for _ in ()).throw(ValidationError("f", 1, "positive")),
        "instance_state": lambda: (_ for _ in ()).throw(InstanceStateError("i1", "idle", "running")),
        "resource": lambda: (_ for _ in ()).throw(ResourceError("gpu", "exhausted")),
        "timeout": lambda: (_ for _ in ()).throw(SimTimeoutError("tick", 5.0)),
        "sensor_sim": lambda: (_ for _ in ()).throw(SensorSimulationError("lidar", "s1", "overflow")),
        "carla_sim": lambda: (_ for _ in ()).throw(CarlaSimulationError("spawn", "failed")),
        "simulation": lambda: (_ for _ in ()).throw(SimulationError("engine", "run", "boom")),
        "unknown": lambda: (_ for _ in ()).throw(HunterSimError("mod", "op", "misc")),
        "http404": lambda: (_ for _ in ()).throw(HTTPException(404, "no such")),
        "http403": lambda: (_ for _ in ()).throw(HTTPException(403, "denied")),
        "http418": lambda: (_ for _ in ()).throw(HTTPException(418, "teapot")),
        "crash": lambda: (_ for _ in ()).throw(ValueError("unhandled")),
    }

    for name, fn in raisers.items():
        def _make(f: Callable[[], None]) -> Callable[[], None]:
            def _route() -> None:
                f()
            return _route
        app.add_api_route(f"/raise/{name}", _make(fn), methods=["GET"])

    @app.post("/echo")
    async def _echo(body: _Body) -> dict[str, int]:
        return {"count": body.count}

    return app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


@pytest.mark.parametrize(
    "name, code, status",
    [
        ("carla_conn", 1003, 503),
        ("kafka_conn", 1004, 503),
        ("ros2_conn", 1005, 503),
        ("config", 1001, 400),
        ("validation", 1006, 422),
        ("instance_state", 1012, 409),
        ("resource", 1010, 503),
        ("timeout", 1011, 504),
        ("sensor_sim", 1009, 500),
        ("carla_sim", 1008, 500),
        ("simulation", 1007, 500),
        ("unknown", 9999, 500),
    ],
)
def test_hunter_sim_errors(client: TestClient, name: str, code: int, status: int) -> None:
    resp = client.get(f"/raise/{name}")
    assert resp.status_code == status
    assert resp.json()["code"] == code
    assert resp.json()["data"] is None


def test_http_404_mapped(client: TestClient) -> None:
    resp = client.get("/raise/http404")
    assert resp.status_code == 404
    assert resp.json()["code"] == 1014


def test_http_403_mapped(client: TestClient) -> None:
    resp = client.get("/raise/http403")
    assert resp.status_code == 403
    assert resp.json()["code"] == 1015


def test_http_default_passthrough(client: TestClient) -> None:
    resp = client.get("/raise/http418")
    assert resp.status_code == 418
    assert resp.json()["code"] == 418


def test_unhandled_exception(client: TestClient) -> None:
    resp = client.get("/raise/crash")
    assert resp.status_code == 500
    assert resp.json()["code"] == 9999
    assert resp.json()["message"] == "Internal server error"


def test_request_validation_error(client: TestClient) -> None:
    resp = client.post("/echo", json={"count": "not-an-int"})
    assert resp.status_code == 422
    assert resp.json()["code"] == 1013
    assert "Request validation failed" in resp.json()["message"]


def test_valid_request_passes(client: TestClient) -> None:
    resp = client.post("/echo", json={"count": 5})
    assert resp.status_code == 200
    assert resp.json() == {"count": 5}
