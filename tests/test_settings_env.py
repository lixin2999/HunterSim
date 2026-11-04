"""配置加载测试：`.env` 对各前缀分组生效、环境变量优先、未知键容错（2.1.2）。

历史问题：`.env` 中写 `CARLA_HOST` 会因 `HunterSimSettings` 的 `extra=forbid`
直接抛 ValidationError，且嵌套分组未配置 `env_file`，导致文档宣称的
“环境变量 > .env > 默认值” 对 `CARLA_*/KAFKA_*/VIL_*/ADAPTER_*/PROBE_*` 不成立。

V3.2.1 补充：模板里留空的 Optional 字段（`KEY=`）不得中断整份配置加载；模板预置
开启的平台接入开关必须与注释、文档同口径。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hunter_sim.common.models import HunterSimSettings

_ENV_KEYS = (
    "CARLA_HOST",
    "CARLA_RPC_PORT",
    "ADAPTER_PORT",
    "PROBE_PORT",
    "HUNTER_SIM_ENV",
    # 下面几项由空值/模板用例断言默认值，宿主机环境变量不得掺入
    "KAFKA_USERNAME",
    "KAFKA_PASSWORD",
    "KAFKA_CA_CERT_PATH",
    "HUNTER_CORE_JWT_TOKEN",
    "HUNTER_CORE_SIM_NODE_ID",
    "HUNTER_CORE_SIM_NODE_SERVICE_TOKEN",
    "HUNTER_CORE_SIM_NODE_MANAGER_PORT",
    # 模板口径用例断言的是模板取值，宿主机变量会覆盖 .env
    "HUNTER_CORE_ENABLED",
    "HUNTER_CORE_SIM_NODE_ENABLED",
)
_REPO_ROOT = Path(__file__).resolve().parent.parent


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


class TestBlankOptionalValues:
    """留空（`KEY=`）= 未配置：V3.2.1 回归现场问题。

    现场按文档 `copy .env.example .env` 后，`HUNTER_CORE_SIM_NODE_MANAGER_PORT=`
    会被 pydantic-settings 当空串送入 `Optional[int]` 校验，以 `int_parsing`
    报错并使 `huntersim up` / `huntersim doctor` 直接起不来。
    """

    def test_blank_optional_int_falls_back_to_default(self, tmp_path: Path) -> None:
        """留空的可选端口解析为 `None`（由调用方回落 `ADAPTER_PORT`），不抛错。"""
        _write_env(tmp_path, "HUNTER_CORE_SIM_NODE_MANAGER_PORT=\n")
        assert HunterSimSettings().sim_node.manager_port is None

    def test_blank_optional_secrets_and_paths_are_none(self, tmp_path: Path) -> None:
        """留空的凭据/证书路径统一为 `None`，不出现空口令与空路径。"""
        _write_env(
            tmp_path,
            "KAFKA_USERNAME=\nKAFKA_PASSWORD=   \nKAFKA_CA_CERT_PATH=\n"
            "HUNTER_CORE_JWT_TOKEN=\nHUNTER_CORE_SIM_NODE_SERVICE_TOKEN=\n",
        )
        settings = HunterSimSettings()
        assert settings.kafka.username is None
        assert settings.kafka.password is None
        assert settings.kafka.ca_cert_path is None
        assert settings.cloud.jwt_token is None
        assert settings.sim_node.service_token is None

    def test_non_blank_optional_values_still_parse(self, tmp_path: Path) -> None:
        """归一只针对空白值：正常配置仍按声明类型与范围生效。"""
        _write_env(
            tmp_path,
            "HUNTER_CORE_SIM_NODE_MANAGER_PORT=9100\nKAFKA_USERNAME=vil_reader\n"
            "HUNTER_CORE_JWT_TOKEN=abc.def.ghi\n",
        )
        settings = HunterSimSettings()
        assert settings.sim_node.manager_port == 9100
        assert settings.kafka.username is not None
        assert settings.kafka.username.get_secret_value() == "vil_reader"
        assert settings.cloud.jwt_token is not None
        assert settings.cloud.jwt_token.get_secret_value() == "abc.def.ghi"

    def test_out_of_range_port_still_rejected(self, tmp_path: Path) -> None:
        """空白归一不得绕过范围校验（端口越界仍是配置错误）。"""
        _write_env(tmp_path, "HUNTER_CORE_SIM_NODE_MANAGER_PORT=70000\n")
        with pytest.raises(ValueError):
            HunterSimSettings()

    def test_shipped_env_template_loads(self, tmp_path: Path) -> None:
        """守护模板本身：`.env.example` 拷贝为 `.env` 后必须能直接加载。"""
        shutil.copy(_REPO_ROOT / ".env.example", tmp_path / ".env")
        settings = HunterSimSettings()
        # 模板里留空的回访端口不得阻断加载，且真实值照常读出
        assert settings.sim_node.manager_port is None
        assert settings.sim_node.node_id == "sim-node-01"
        # 模板面向平台联调机：两个接入开关预置为开启（代码字段默认仍是 False）
        assert settings.cloud.enabled is True
        assert settings.sim_node.enabled is True

    def test_template_integration_switches_are_documented(self) -> None:
        """模板口径守护：预置开启必须与注释同口径（防止取值与注释、文档背离）。

        V3.2.1 现场确认：模板把 `HUNTER_CORE_ENABLED` / `HUNTER_CORE_SIM_NODE_ENABLED`
        置为 `true`，而旧注释与文档写的是「默认关闭」，新机器拷完模板就会撞上
        `sim_node` 缺服务令牌的 `fail`。现在两处必须同时成立。
        """
        text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "HUNTER_CORE_ENABLED=true" in text
        assert "HUNTER_CORE_SIM_NODE_ENABLED=true" in text
        # 两个分节标题都要写明「默认开启，需补凭据」
        assert text.count("本模板默认开启，需补凭据") == 2
        # 文件头必须给出首启清单，且服务令牌标注为必填
        assert "必须补齐" in text
        assert "开启接入时必填" in text
