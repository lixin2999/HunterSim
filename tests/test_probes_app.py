"""只读探针应用测试（``/health`` / ``/health/live`` / ``/health/ready`` / ``/metrics``）。

覆盖要点：
- 上游（``huntersim carla-manager``）不可达时仍返回 **200 + degraded**，字段显式置
  ``None``，绝不因 CARLA/Kafka 故障 500（容器 liveness 不应被反复重启）；
- 上游可达时透传 ``carla_connected`` / ``active_instance`` / ``vil_state`` /
  ``sensor`` / ``cloud``；
- ``/metrics`` 只导出本进程注册表（含 ``huntersim_probe_upstream_reachable``）。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from hunter_sim.common.models import HunterSimSettings, ProbeSettings
from hunter_sim.probes.app import create_probes_app

_HEALTH_KEYS = (
    "carla_connected",
    "carla_probe_error",
    "active_instance",
    "vil_state",
    "sensor",
    "cloud",
)


def _client(adapter_url: str = "") -> TestClient:
    settings = HunterSimSettings(probe=ProbeSettings(adapter_url=adapter_url))
    return TestClient(create_probes_app(settings))


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _install_httpx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: Optional[Any] = None,
    error: Optional[Exception] = None,
) -> list[str]:
    """把惰性 ``import httpx`` 替换为桩模块，返回被调用的 URL 列表。"""
    calls: list[str] = []

    def _get(url: str, **_kwargs: Any) -> Any:
        calls.append(url)
        if error is not None:
            raise error
        return response

    fake = SimpleNamespace(get=_get)
    monkeypatch.setitem(sys.modules, "httpx", fake)  # type: ignore[arg-type]
    return calls


class TestHealthDegraded:
    """上游不可达 / 未配置 / 异常：200 + degraded。"""

    def test_unconfigured_upstream_degrades_without_error(self) -> None:
        with _client("") as client:
            response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["upstream"]["reachable"] is False
        assert body["upstream"]["error"] == "adapter_url not configured"
        for key in _HEALTH_KEYS:
            assert body[key] is None, f"{key} should be None when upstream is down"
        assert body["version"]
        assert body["uptime_seconds"] >= 0

    def test_connection_error_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_httpx(monkeypatch, error=OSError("connection refused"))
        with _client("http://127.0.0.1:8090") as client:
            body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert "OSError" in body["upstream"]["error"]

    def test_http_error_status_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_httpx(monkeypatch, response=_FakeResponse({"status": "ok"}, status_code=503))
        with _client("http://127.0.0.1:8090") as client:
            body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert body["upstream"]["error"] == "HTTP 503"

    def test_invalid_json_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_httpx(monkeypatch, response=_FakeResponse(ValueError("bad json")))
        with _client("http://127.0.0.1:8090") as client:
            body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert "invalid json" in body["upstream"]["error"]

    def test_non_dict_payload_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_httpx(monkeypatch, response=_FakeResponse([1, 2, 3]))
        with _client("http://127.0.0.1:8090") as client:
            body = client.get("/health").json()
        assert body["status"] == "degraded"


class TestHealthUpstream:
    """上游可达：透传管理面聚合字段。"""

    @staticmethod
    def _snapshot() -> dict[str, Any]:
        return {
            "status": "ok",
            "carla_connected": True,
            "carla_probe_error": None,
            "active_instance": {"instance_id": "inst-1", "status": "running"},
            "vil_state": {"frames_per_second": 10.0, "interrupted": False},
            "sensor": {"recording": True, "recording_bytes": 1024},
            "cloud": {"enabled": True},
        }

    def test_passthrough_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _install_httpx(monkeypatch, response=_FakeResponse(self._snapshot()))
        with _client("http://127.0.0.1:8090/") as client:
            body = client.get("/health").json()
        assert calls == ["http://127.0.0.1:8090/health"]
        assert body["status"] == "ok"
        assert body["upstream"]["reachable"] is True
        assert body["upstream"]["error"] is None
        assert body["carla_connected"] is True
        assert body["active_instance"]["instance_id"] == "inst-1"
        assert body["vil_state"]["frames_per_second"] == 10.0
        assert body["sensor"]["recording"] is True
        assert body["cloud"]["enabled"] is True


class TestProbesEndpoints:
    def test_health_live_always_ok(self) -> None:
        with _client("") as client:
            assert client.get("/health/live").json() == {"status": "ok"}

    def test_health_ready_reflects_upstream(self) -> None:
        with _client("") as client:
            body = client.get("/health/ready").json()
        assert body["status"] == "degraded"
        assert body["upstream"]["reachable"] is False

    def test_ready_ok_when_upstream_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_httpx(monkeypatch, response=_FakeResponse({"status": "ok"}))
        with _client("http://127.0.0.1:8090") as client:
            body = client.get("/health/ready").json()
        assert body["status"] == "ok"

    def test_metrics_exposes_probe_registry(self) -> None:
        with _client("") as client:
            response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = response.text
        assert "huntersim_probe_upstream_reachable" in body
        assert "huntersim_uptime_seconds" in body
        # 标准导出器自带的进程指标
        assert "python_info" in body or "process_" in body

    def test_openapi_and_docs_disabled(self) -> None:
        with _client("") as client:
            assert client.get("/openapi.json").status_code == 404
            assert client.get("/docs").status_code == 404
