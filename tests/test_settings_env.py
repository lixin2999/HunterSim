"""配置加载测试：`.env` 对各前缀分组生效、环境变量优先、未知键容错（2.1.2）。

历史问题：`.env` 中写 `CARLA_HOST` 会因 `HunterSimSettings` 的 `extra=forbid`
直接抛 ValidationError，且嵌套分组未配置 `env_file`，导致文档宣称的
“环境变量 > .env > 默认值” 对 `CARLA_*/KAFKA_*/VIL_*/ADAPTER_*/PROBE_*` 不成立。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter_sim.common.models import HunterSimSettings

_ENV_KEYS = (
    "CARLA_HOST",
    "CARLA_RPC_PORT",
    "ADAPTER_PORT",
    "PROBE_PORT",
    "HUNTER_SIM_ENV",
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主机环境变量与工作目录：`.env` 相对服务进程 cwd 解析。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _write_env(tmp_path: Path, content: str) -> None:
    (tmp_path / ".env").write_text(content, encoding="utf-8")


def test_dotenv_applies_to_nested_prefixes(tmp_path: Path) -> None:
    """`.env` 中的各前缀键应分别被对应分组读取。"""
    _write_env(
        tmp_path,
        "HUNTER_SIM_ENV=staging\nCARLA_HOST=10.1.2.3\nCARLA_RPC_PORT=2100\n"
        "ADAPTER_PORT=9091\nPROBE_PORT=9092\n",
    )
    settings = HunterSimSettings()
    assert settings.env == "staging"
    assert settings.carla.host == "10.1.2.3"
    assert settings.carla.rpc_port == 2100
    assert settings.adapter.port == 9091
    assert settings.probe.port == 9092


def test_env_var_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实环境变量优先级高于 `.env`。"""
    _write_env(tmp_path, "CARLA_HOST=10.1.2.3\n")
    monkeypatch.setenv("CARLA_HOST", "172.16.0.9")
    assert HunterSimSettings().carla.host == "172.16.0.9"


def test_unknown_dotenv_keys_do_not_crash(tmp_path: Path) -> None:
    """未知键（含其他前缀的键）不得触发 extra_forbidden 校验错误。"""
    _write_env(tmp_path, "CARLA_HOST=10.1.2.3\nSOME_TYPO=1\nCARLA_UNKNOWN_FIELD=x\n")
    settings = HunterSimSettings()
    assert settings.carla.host == "10.1.2.3"


def test_missing_dotenv_uses_defaults(tmp_path: Path) -> None:
    """无 `.env` 时回落代码默认值（127.0.0.1:2000）。"""
    settings = HunterSimSettings()
    assert settings.carla.host == "127.0.0.1"
    assert settings.carla.rpc_port == 2000


def test_cloud_adapter_and_probe_dotenv_apply(tmp_path: Path) -> None:
    """V3.0.0：HUNTER_CORE_* / ADAPTER_* / PROBE_* 前缀键分别被对应分组读取。"""
    _write_env(
        tmp_path,
        "HUNTER_CORE_ENABLED=true\n"
        "HUNTER_CORE_SIM_ID=SIM_X9\n"
        "HUNTER_CORE_KAFKA_BOOTSTRAP=192.168.31.35:9092\n"
        "HUNTER_CORE_JWT_TOKEN=abc.def.ghi\n"
        "ADAPTER_STREAM_FPS=24\n"
        "ADAPTER_RECORDINGS_DIR=data/rec\n"
        "PROBE_PORT=8099\n",
    )
    settings = HunterSimSettings()
    assert settings.cloud.enabled is True
    assert settings.cloud.sim_id == "SIM_X9"
    assert settings.cloud.kafka_bootstrap == "192.168.31.35:9092"
    assert settings.cloud.jwt_token is not None
    assert settings.cloud.jwt_token.get_secret_value() == "abc.def.ghi"
    assert settings.adapter.stream_fps == 24.0
    assert settings.adapter.recordings_dir == "data/rec"
    assert settings.probe.port == 8099


def test_hunter_core_mtls_cert_env_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V3.0.3：mTLS 客户端证书路径经 HUNTER_CORE_CLIENT_{CERT,KEY}_PATH 注入。"""
    monkeypatch.setenv("HUNTER_CORE_CA_CERT_PATH", "/certs/ca-cert.pem")
    monkeypatch.setenv("HUNTER_CORE_CLIENT_CERT_PATH", "/certs/client-cert.pem")
    monkeypatch.setenv("HUNTER_CORE_CLIENT_KEY_PATH", "/certs/client-key.pem")
    settings = HunterSimSettings()
    assert settings.cloud.ca_cert_path == "/certs/ca-cert.pem"
    assert settings.cloud.client_cert_path == "/certs/client-cert.pem"
    assert settings.cloud.client_key_path == "/certs/client-key.pem"


def test_cloud_adapter_and_probe_defaults(tmp_path: Path) -> None:
    """未配置时 cloud 默认关闭、指向单机验证环境；adapter/probe 默认 8090/8080。"""
    settings = HunterSimSettings()
    assert settings.cloud.enabled is False
    assert settings.cloud.kafka_bootstrap == "192.168.31.35:9092"
    assert settings.cloud.sensor_data_topic_template == "carla.{sim_id}.sensor_data"
    assert settings.adapter.port == 8090
    assert settings.adapter.stream_fps == 10.0
    assert settings.probe.port == 8080
    assert settings.probe.adapter_url == "http://127.0.0.1:8090"


def test_removed_prefixes_are_gone() -> None:
    """V3.0.0：8080 治理面与 GUI 配置块已删除，不得再出现在聚合配置中。"""
    fields = set(HunterSimSettings.model_fields)
    assert {"api", "state", "platform", "gui"}.isdisjoint(fields)
    assert {"adapter", "probe", "carla", "kafka", "vil", "cloud", "resource"} <= fields


def test_cloud_unknown_keys_ignored(tmp_path: Path) -> None:
    """HUNTER_CORE_ 前缀下的未知键不得触发校验错误。"""
    _write_env(tmp_path, "HUNTER_CORE_TYPO_FIELD=1\nADAPTER_TYPO=2\n")
    settings = HunterSimSettings()
    assert settings.cloud.enabled is False
