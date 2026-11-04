"""V2.0 一键开通/节点级回传通道测试（方案 §2.5、接入清单 §1.5）。

覆盖四处新增能力：
- ``build_node_result`` 结果消息形态；
- ``SimNodeSettings`` 节点级 Topic 渲染与接入开关默认打开；
- ``NodeResultProducer`` 配置门控 + no-op 降级 + 经工厂注入的发布；
- ``AdapterSettings`` 下发通道 TLS/mTLS 字段与空白证书归一；
- ``SimNodeClient.download_bundle`` 三步接入 step1（下载 + 解压 + 失败降级）；
- ``SimulationExecutor._publish_node_result`` 终态挂载与幂等门控；
- ``huntersim doctor`` 的 sim_node 缺凭据由 fail 降级为 warn。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from hunter_sim.cli.main import _check_sim_node
from hunter_sim.cloud.node_result_producer import NodeResultProducer, build_node_result
from hunter_sim.common.models import CloudSettings, HunterSimSettings, SimNodeSettings
from hunter_sim.platform_adapter import executor as executor_module
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.platform_adapter.executor import SimulationExecutor
from hunter_sim.platform_adapter.models import InstanceRecord
from hunter_sim.platform_adapter.sim_node import SimNodeClient
from hunter_sim.platform_adapter.store import InstanceStore


# ── 结果消息与 Topic 渲染 ─────────────────────────────────────────────────────


def test_build_node_result_shape() -> None:
    """结果摘要消息字段齐全、type 固定、默认补全空容器。"""
    msg = build_node_result(
        node_id="sim-node-01",
        sim_id="sim-42",
        scene_id="scene-1",
        status="succeeded",
        success=True,
    )
    assert msg["type"] == "simulation_result"
    assert msg["node_id"] == "sim-node-01"
    assert msg["sim_id"] == "sim-42"
    assert msg["status"] == "succeeded"
    assert msg["success"] is True
    assert msg["success_criteria_result"] == {}
    assert msg["metrics"] == {}
    assert msg["artifacts"] == []
    assert msg["provider"] == "carla"
    assert isinstance(msg["timestamp"], float)


def test_sim_node_render_result_topics() -> None:
    """Topic 模板按 node_id 渲染；node_id 为空返回空串（不可用）。"""
    node = SimNodeSettings(node_id="sim-node-01")
    assert node.render_result_topic() == "simulation.sim-node-01.result"
    assert node.render_result_dlq_topic() == "simulation.sim-node-01.result.dlq"
    blank = SimNodeSettings(node_id="")
    assert blank.render_result_topic() == ""
    assert blank.render_result_dlq_topic() == ""


def test_integration_defaults_are_on() -> None:
    """V3.2.2：cloud / sim_node / 节点级回传默认全开（无凭据即静默跳过）。"""
    settings = HunterSimSettings()
    assert settings.cloud.enabled is True
    assert settings.sim_node.enabled is True
    assert settings.sim_node.result_enabled is True


# ── NodeResultProducer 门控与发布 ─────────────────────────────────────────────


def test_node_result_producer_unusable_when_no_node_id() -> None:
    """无 node_id / 关闭回传时 usable=False，connect 降级、publish 安全返回 False。"""
    cloud = CloudSettings(enabled=True, kafka_bootstrap="10.0.0.1:9093")
    node = SimNodeSettings(node_id="", enabled=True, result_enabled=True)
    producer = NodeResultProducer(cloud, node)
    assert producer.usable is False
    assert producer.connect() is False
    assert producer.available is False
    assert producer.publish(build_node_result(
        node_id="", sim_id="s", scene_id="x", status="succeeded", success=True
    )) is False


def test_node_result_producer_publish_via_factory() -> None:
    """经工厂注入的桩生产者：Topic/key 正确、计数打点、返回 True。"""
    sent: list[dict[str, Any]] = []

    class _StubKafka:
        def send(self, topic: str, key: bytes, value: bytes) -> None:
            sent.append({"topic": topic, "key": key, "value": value})

        def flush(self, timeout: float = 3.0) -> None:
            pass

        def close(self) -> None:
            pass

    cloud = CloudSettings(enabled=True, kafka_bootstrap="10.0.0.1:9093")
    node = SimNodeSettings(node_id="sim-node-01", enabled=True, result_enabled=True)
    producer = NodeResultProducer(cloud, node, producer_factory=lambda _c: _StubKafka())
    assert producer.connect() is True
    assert producer.topic == "simulation.sim-node-01.result"
    ok = producer.publish(build_node_result(
        node_id="sim-node-01", sim_id="sim-42", scene_id="scene-1",
        status="succeeded", success=True,
    ))
    assert ok is True
    assert sent and sent[0]["topic"] == "simulation.sim-node-01.result"
    assert sent[0]["key"] == b"sim-42"
    producer.close()


def test_node_result_producer_publish_failure_swallowed() -> None:
    """底层 send 抛异常时 publish 返回 False 且不抛出（不阻断结果落库）。"""

    class _Boom:
        def send(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("broker down")

        def flush(self, timeout: float = 3.0) -> None:
            pass

        def close(self) -> None:
            pass

    cloud = CloudSettings(enabled=True, kafka_bootstrap="10.0.0.1:9093")
    node = SimNodeSettings(node_id="sim-node-01")
    producer = NodeResultProducer(cloud, node, producer_factory=lambda _c: _Boom())
    producer.connect()
    result = producer.publish(build_node_result(
        node_id="sim-node-01", sim_id="s", scene_id="x", status="failed", success=False
    ))
    assert result is False


# ── AdapterSettings 下发 mTLS ────────────────────────────────────────────────


def test_adapter_tls_disabled_by_default() -> None:
    """未配证书时 TLS 关闭，空白证书路径归一为 None。"""
    adapter = AdapterSettings(
        ssl_certfile="", ssl_keyfile="   ", ssl_ca_certfile="",
    )
    assert adapter.ssl_certfile is None
    assert adapter.ssl_keyfile is None
    assert adapter.tls_enabled is False


def test_adapter_tls_enabled_with_keypair(tmp_path: Path) -> None:
    """服务端证书与私钥成对时 tls_enabled 为真。"""
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("cert")
    key.write_text("key")
    adapter = AdapterSettings(ssl_certfile=str(cert), ssl_keyfile=str(key))
    assert adapter.tls_enabled is True


# ── SimNodeClient.download_bundle（三步接入 step1）──────────────────────────


class _Response:
    def __init__(self, status_code: int, content: bytes = b"", text: str = "") -> None:
        self.status_code = status_code
        self.content = content
        self.text = text


class _StubClient:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.last_url: Optional[str] = None

    def request(self, method: str, url: str, headers: Any = None, timeout: Any = None) -> _Response:
        self.last_url = url
        return self.response

    def close(self) -> None:
        pass


def _make_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("kafka.properties", "sasl.username=sim-sim-node-01\nsasl.password=\n")
        zf.writestr("ca-cert.pem", "CA")
    return buf.getvalue()


def test_download_bundle_success(tmp_path: Path) -> None:
    """200 响应落盘 zip 并解压，返回解压目录且内含 kafka.properties。"""
    client = _StubClient(_Response(200, content=_make_zip_bytes()))
    node = SimNodeSettings(node_id="sim-node-01", service_token="service:sim-node-01")
    sim_client = SimNodeClient(node, base_url="http://platform", client_factory=lambda: client)
    result = sim_client.download_bundle(tmp_path / "connect")
    assert result is not None and result.is_dir()
    assert (result / "kafka.properties").is_file()
    assert client.last_url is not None and client.last_url.endswith("/sim-nodes/sim-node-01/bundle")


def test_download_bundle_http_error_returns_none(tmp_path: Path) -> None:
    """非 2xx（未开通/无权限）返回 None，不抛异常。"""
    client = _StubClient(_Response(404, text="not found"))
    node = SimNodeSettings(node_id="sim-node-01", service_token="service:sim-node-01")
    sim_client = SimNodeClient(node, base_url="http://platform", client_factory=lambda: client)
    assert sim_client.download_bundle(tmp_path / "connect") is None


def test_download_bundle_requires_node_id(tmp_path: Path) -> None:
    """缺 node_id 时拒绝下载并返回 None。"""
    node = SimNodeSettings(node_id="")
    sim_client = SimNodeClient(node, base_url="http://platform", client_factory=lambda: _StubClient(_Response(200)))
    assert sim_client.download_bundle(tmp_path) is None


# ── 执行器终态挂载 ────────────────────────────────────────────────────────────


class _StubResultProducer:
    registry: list["_StubResultProducer"] = []

    def __init__(self, cloud: CloudSettings, node: SimNodeSettings) -> None:
        self.node = node
        self.published: list[dict[str, Any]] = []
        self._available = False
        self.closed = False
        _StubResultProducer.registry.append(self)

    def connect(self) -> bool:
        self._available = True
        return True

    @property
    def available(self) -> bool:
        return self._available

    def publish(self, message: dict[str, Any]) -> bool:
        self.published.append(message)
        return True

    def close(self, timeout: float = 3.0) -> None:
        self.closed = True


@pytest.fixture
def store(tmp_path: Path) -> InstanceStore:
    instance_store = InstanceStore(str(tmp_path / "adapter.db")).connect()
    yield instance_store
    instance_store.close()


@pytest.fixture
def node_executor(store: InstanceStore, tmp_path: Path) -> SimulationExecutor:
    settings = HunterSimSettings(
        cloud=CloudSettings(enabled=True, kafka_bootstrap="10.0.0.1:9093"),
        sim_node=SimNodeSettings(node_id="sim-node-01", enabled=True, result_enabled=True),
    )
    adapter = AdapterSettings(
        db_path=str(tmp_path / "adapter.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    return SimulationExecutor(settings, adapter, store, plane=SimpleNamespace())


def test_executor_publishes_terminal_result(
    store: InstanceStore, node_executor: SimulationExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """实例进入 succeeded 终态后向节点级 Topic 回传摘要。"""
    _StubResultProducer.registry.clear()
    monkeypatch.setattr(executor_module, "NodeResultProducer", _StubResultProducer)
    record = InstanceRecord(instance_id="inst-1", sim_id="sim-42", scene_id="scene-1")
    store.insert(record)
    store.patch(
        "inst-1",
        status="succeeded",
        result={"success": True, "evaluation": {"grade": "A"}, "artifacts": [], "success_criteria_result": {}},
    )
    node_executor._publish_node_result("inst-1")
    assert len(_StubResultProducer.registry) == 1
    published = _StubResultProducer.registry[0].published
    assert published and published[0]["status"] == "succeeded"
    assert published[0]["sim_id"] == "sim-42"


def test_executor_skips_non_terminal(
    store: InstanceStore, node_executor: SimulationExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非终态实例不回传（不构造生产者）。"""
    _StubResultProducer.registry.clear()
    monkeypatch.setattr(executor_module, "NodeResultProducer", _StubResultProducer)
    store.insert(InstanceRecord(instance_id="inst-2", sim_id="sim-7", scene_id="scene-1", status="running"))
    node_executor._publish_node_result("inst-2")
    assert _StubResultProducer.registry == []


# ── doctor：缺凭据降级为 warn ─────────────────────────────────────────────────


def test_doctor_sim_node_missing_creds_is_warn() -> None:
    """V3.2.2：sim_node 启用但缺 node_id/令牌时记 warn，不阻断 up。"""
    settings = HunterSimSettings(
        sim_node=SimNodeSettings(enabled=True, node_id="", service_token=None),
    )
    result = _check_sim_node(settings)
    assert result.status == "warn"


def test_doctor_sim_node_disabled_is_skip() -> None:
    """显式关闭接入时检查项为 skip。"""
    settings = HunterSimSettings(sim_node=SimNodeSettings(enabled=False, node_id=""))
    assert _check_sim_node(settings).status == "skip"
