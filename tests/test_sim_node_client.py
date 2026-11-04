"""仿真节点控制面客户端测试（HunterCore node 模式）。

覆盖注册请求体/鉴权头、心跳实例上报、租约过期后自动重注册、
配置不完整时不启动，以及控制面 ``/health`` 的节点快照与 doctor 判定。
网络全部由 ``httpx.MockTransport`` 打桩，不依赖真实平台。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from hunter_sim.cli import main as cli
from hunter_sim.cli.main import _check_sim_node
from hunter_sim.common.models import (
    AdapterSettings,
    HunterSimSettings,
    SimNodeSettings,
)
from hunter_sim.platform_adapter.app import create_app
from hunter_sim.platform_adapter.sim_node import SimNodeClient

_PLATFORM = "http://platform.example:8080"
_NODE_ID = "sim-node-01"
_TOKEN = "service-token-abc"


def _settings(**overrides: Any) -> SimNodeSettings:
    payload: dict[str, Any] = {
        "enabled": True,
        "node_id": _NODE_ID,
        "service_token": _TOKEN,
        "maps": "Town03, Town01, Town03",
        "capacity": 2,
        "carla_version": "0.9.16",
        "labels": {"room": "lab-a"},
        "runtime_hint": "http://sim-node-01:8090",
    }
    payload.update(overrides)
    return SimNodeSettings(**payload)


def _node_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "node_id": _NODE_ID,
        "online": True,
        "endpoint_ip": "192.168.31.20",
        "endpoint_source": "real-ip",
        "manager_port": 8090,
        "protocol": "http",
        "lease_seconds": 90,
        "running_count": 0,
    }
    data.update(overrides)
    return data


class _Recorder:
    """MockTransport 处理器：记录方法与 JSON 请求体。"""

    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        self.calls.append(
            (request.method, str(request.url), body, dict(request.headers))
        )
        status, payload = self._responses.pop(0) if self._responses else (200, {})
        return httpx.Response(status, json=payload)


def _client(recorder: _Recorder, **overrides: Any) -> SimNodeClient:
    return SimNodeClient(
        _settings(**overrides),
        base_url=_PLATFORM + "/",
        manager_port=8090,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(recorder)),
    )


class TestRegister:
    def test_register_sends_capability_and_uses_service_token(self) -> None:
        recorder = _Recorder([(200, {"code": 0, "message": "ok", "data": _node_data()})])
        client = _client(recorder)

        assert client.register() is True
        method, url, body, headers = recorder.calls[0]
        assert method == "PUT"
        assert url == f"{_PLATFORM}/api/v1/scene/sim-nodes/{_NODE_ID}"
        assert headers["authorization"] == f"Bearer {_TOKEN}"
        assert body == {
            "capacity": 2,
            "protocol": "http",
            "manager_port": 8090,
            "maps": ["Town03", "Town01"],
            "carla_version": "0.9.16",
            "labels": {"room": "lab-a"},
            "runtime_hint": "http://sim-node-01:8090",
        }
        # 地址只能由平台自学习，请求体不得出现任何地址字段
        assert "endpoint_ip" not in body and "ip" not in body

        snapshot = client.snapshot()
        assert snapshot["online"] is True
        assert snapshot["endpoint_ip"] == "192.168.31.20"
        assert snapshot["endpoint_source"] == "real-ip"
        assert snapshot["lease_seconds"] == 90

    def test_empty_maps_means_no_map_filter(self) -> None:
        recorder = _Recorder([(200, {"code": 0, "data": _node_data()})])
        client = _client(recorder, maps="")

        assert client.register() is True
        assert "maps" not in recorder.calls[0][2]

    def test_platform_error_is_reported_and_retried(self) -> None:
        recorder = _Recorder(
            [
                (403, {"code": 1002, "message": "无权限：节点归属其它调用方"}),
                (200, {"code": 0, "data": _node_data()}),
            ]
        )
        client = _client(recorder)

        assert client.register() is False
        assert client.snapshot()["online"] is False
        assert client.snapshot()["last_error"].startswith("403/1002")
        assert client.tick() is True  # 下一轮仍先注册
        assert [call[0] for call in recorder.calls] == ["PUT", "PUT"]


class TestHeartbeat:
    def test_heartbeat_reports_running_instances(self) -> None:
        recorder = _Recorder(
            [
                (200, {"code": 0, "data": _node_data()}),
                (200, {"code": 0, "data": _node_data(running_count=1)}),
            ]
        )
        client = SimNodeClient(
            _settings(),
            base_url=_PLATFORM,
            manager_port=8090,
            running_instance_ids=lambda: ["inst-1", "inst-2"],
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(recorder)),
        )
        assert client.register() is True
        assert client.heartbeat() is True

        method, url, body, _ = recorder.calls[1]
        assert method == "POST"
        assert url == f"{_PLATFORM}/api/v1/scene/sim-nodes/{_NODE_ID}/heartbeat"
        assert body == {"running_instance_ids": ["inst-1", "inst-2"]}

    def test_expired_lease_re_registers_on_next_tick(self) -> None:
        recorder = _Recorder(
            [
                (200, {"code": 0, "data": _node_data()}),
                (404, {"code": 3001, "message": "仿真节点未注册或租约已过期"}),
                (200, {"code": 0, "data": _node_data()}),
            ]
        )
        client = _client(recorder)

        assert client.register() is True
        assert client.tick() is False  # heartbeat → 3001
        assert client.tick() is True  # 回到注册
        assert [call[0] for call in recorder.calls] == ["PUT", "POST", "PUT"]
        assert recorder.calls[2][1].endswith(f"/sim-nodes/{_NODE_ID}")

    def test_running_provider_failure_does_not_break_heartbeat(self) -> None:
        recorder = _Recorder([(200, {"code": 0, "data": _node_data()})])
        client = SimNodeClient(
            _settings(),
            base_url=_PLATFORM,
            manager_port=8090,
            running_instance_ids=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(recorder)),
        )

        assert client.heartbeat() is True
        assert recorder.calls[0][2] == {"running_instance_ids": []}

    def test_heartbeat_filters_blank_and_caps_running_ids(self) -> None:
        recorder = _Recorder([(200, {"code": 0, "data": _node_data()})])
        client = SimNodeClient(
            _settings(),
            base_url=_PLATFORM,
            manager_port=8090,
            running_instance_ids=lambda: ["", "  ", *[f"inst-{i}" for i in range(40)]],
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(recorder)),
        )

        assert client.heartbeat() is True
        reported = recorder.calls[0][2]["running_instance_ids"]
        assert len(reported) == 32
        assert reported[0] == "inst-0"
        assert all(str(item).strip() for item in reported)


class TestLifecycleAndConfig:
    def test_start_requires_token(self) -> None:
        client = SimNodeClient(
            _settings(service_token=None),
            base_url=_PLATFORM,
            manager_port=8090,
        )
        assert client.start() is False
        assert client.snapshot()["online"] is False

    def test_disabled_client_does_nothing(self) -> None:
        client = SimNodeClient(
            _settings(enabled=False),
            base_url=_PLATFORM,
            manager_port=8090,
        )
        assert client.start() is False
        assert client.tick() is False

    def test_invalid_node_id_rejected_by_settings(self) -> None:
        with pytest.raises(ValueError):
            _settings(node_id="192.168.31.20")

    def test_node_id_reads_documented_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`HUNTER_CORE_SIM_NODE_` 前缀 + 字段 `node_id` 会拼成 `..._NODE_ID`。

        回归：直接用 ``Field`` 时 pydantic-settings 会找 ``..._NODE_NODE_ID``，
        文档与 .env 里的 ``HUNTER_CORE_SIM_NODE_ID`` 会被静默忽略。
        """
        monkeypatch.setenv("HUNTER_CORE_SIM_NODE_ID", "sim-node-42")
        assert SimNodeSettings(_env_file=None).node_id == "sim-node-42"


class _FakeNodeClient:
    """应用生命周期测试替身。"""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> bool:
        self.started += 1
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped += 1

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": True, "node_id": _NODE_ID, "online": True}


class _HealthService:
    def health_snapshot(self) -> dict[str, Any]:
        return {"carla_connected": False}


class TestAppIntegration:
    def test_lifespan_starts_and_stops_injected_client(self) -> None:
        fake = _FakeNodeClient()
        app = create_app(
            service=_HealthService(),  # type: ignore[arg-type]
            sim_node_client=fake,  # type: ignore[arg-type]
        )
        with TestClient(app) as client:
            assert fake.started == 1
            body = client.get("/health").json()
            assert body["sim_node"]["online"] is True
        assert fake.stopped == 1


class TestDoctor:
    def test_disabled_is_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = HunterSimSettings(sim_node=SimNodeSettings(enabled=False))
        result = _check_sim_node(settings)
        assert result.status == "skip"

    def test_missing_token_is_warn(self) -> None:
        # V3.2.2：默认打开但缺服务令牌时静默跳过，记 warn（不阻断 huntersim up）
        settings = HunterSimSettings(sim_node=_settings(service_token=None))
        result = _check_sim_node(settings)
        assert result.status == "warn"
        assert "SERVICE_TOKEN" in result.detail

    def test_loopback_listener_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: True)
        settings = HunterSimSettings(
            sim_node=_settings(),
            adapter=AdapterSettings(host="127.0.0.1", port=8090),
        )
        result = _check_sim_node(settings)
        assert result.status == "warn"
        assert "ADAPTER_HOST" in result.hint

    def test_reachable_platform_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: True)
        settings = HunterSimSettings(
            sim_node=_settings(base_url="http://platform.example"),
            adapter=AdapterSettings(host="0.0.0.0", port=8090),
        )
        result = _check_sim_node(settings)
        assert result.status == "ok"
        assert _NODE_ID in result.detail

    def test_gateway_8080_warns_about_learned_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: True)
        settings = HunterSimSettings(
            sim_node=_settings(base_url=_PLATFORM),
            adapter=AdapterSettings(host="0.0.0.0", port=8090),
        )
        result = _check_sim_node(settings)
        assert result.status == "warn"
        assert "8080" in result.detail
