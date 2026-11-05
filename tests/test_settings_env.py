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
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_SECURITY_PROTOCOL",
    "KAFKA_SASL_MECHANISM",
    "KAFKA_CLIENT_CERT_PATH",
    "KAFKA_CLIENT_KEY_PATH",
    "HUNTER_CORE_JWT_TOKEN",
    "HUNTER_CORE_SIM_NODE_ID",
    "HUNTER_CORE_SIM_NODE_SERVICE_TOKEN",
    "HUNTER_CORE_SIM_NODE_MANAGER_PORT",
    "HUNTER_CORE_KAFKA_BOOTSTRAP",
    "HUNTER_CORE_SASL_USERNAME",
    "HUNTER_CORE_SASL_PASSWORD",
    "HUNTER_CORE_SECURITY_PROTOCOL",
    "HUNTER_CORE_SASL_MECHANISM",
    "HUNTER_CORE_CA_CERT_PATH",
    "HUNTER_CORE_CLIENT_CERT_PATH",
    "HUNTER_CORE_CLIENT_KEY_PATH",
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
    """未配置时 cloud/sim_node 默认打开（V3.2.2），adapter/probe 默认 8090/8080。"""
    settings = HunterSimSettings()
    assert settings.cloud.enabled is True
    assert settings.sim_node.enabled is True
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
    assert settings.cloud.enabled is True


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
        """守护模板本身：`.env.example` 拷贝为 `.env` 后必须能直接加载。

        V3.2.2 极简模板面向“本机零配置”，平台字段留空（``KEY=``）不得中断加载；
        接入开关靠代码默认（不再在模板里逐条写 ENABLED=true）。
        """
        shutil.copy(_REPO_ROOT / ".env.example", tmp_path / ".env")
        settings = HunterSimSettings()
        # 留空的回访端口/节点标识不得阻断加载（本机零配置可直接起）
        assert settings.sim_node.manager_port is None
        assert settings.sim_node.node_id == ""
        assert settings.sim_node.service_token is None
        # 接入开关靠代码默认，拷完模板即为开（无凭据时静默跳过）
        assert settings.cloud.enabled is True
        assert settings.sim_node.enabled is True

    def test_template_integration_switches_are_documented(self) -> None:
        """模板口径守护（V3.2.2 极简版）：默认靠代码，模板不重列接入开关与调优项。

        旧版把两个 ENABLED 写死在模板里，默认值一变就要同步改模板；V3.2.2 改为：
        默认值在代码，模板只保留本机首启项与平台凭据位，并显式说明“无凭据静默跳过”。
        """
        text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        # 不再把两个开关当默认值写死（靠代码默认；仅在“关闭”提示里以注释出现）
        active_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert not any(line.startswith("HUNTER_CORE_ENABLED=") for line in active_lines)
        assert not any(
            line.startswith("HUNTER_CORE_SIM_NODE_ENABLED=") for line in active_lines
        )
        # 必须说明默认全开 + 无凭据静默跳过，并保留三步接入入口
        assert "默认全开" in text
        assert "静默跳过" in text
        assert "huntersim connect" in text
        # 调优项不再罗列：模板行数应大幅少于旧版（极简契约）
        assert len(text.splitlines()) < 100


class TestKafkaInheritance:
    """V3.3.0：VIL 消费端 Kafka 连接参数自动继承 CloudSettings（消除 .env 重复）。"""

    def test_no_cloud_sasl_keeps_kafka_defaults(self, tmp_path: Path) -> None:
        """未配置平台 SASL 时，KafkaSettings 维持默认 localhost:9092 PLAINTEXT。"""
        _write_env(tmp_path, "CARLA_HOST=127.0.0.1\n")
        settings = HunterSimSettings()
        assert settings.kafka.bootstrap_servers == "localhost:9092"
        assert settings.kafka.security_protocol == "PLAINTEXT"
        assert settings.kafka.username is None

    def test_cloud_sasl_propagates_to_kafka(self, tmp_path: Path) -> None:
        """配了 HUNTER_CORE_SASL_USERNAME 后，KafkaSettings 自动继承整套连接参数。"""
        _write_env(
            tmp_path,
            "HUNTER_CORE_KAFKA_BOOTSTRAP=192.168.1.100:9093\n"
            "HUNTER_CORE_SASL_USERNAME=test_user\n"
            "HUNTER_CORE_SASL_PASSWORD=test_pass\n"
            "HUNTER_CORE_CA_CERT_PATH=/certs/ca.pem\n"
            "HUNTER_CORE_CLIENT_CERT_PATH=/certs/client.pem\n"
            "HUNTER_CORE_CLIENT_KEY_PATH=/certs/client-key.pem\n",
        )
        settings = HunterSimSettings()
        assert settings.kafka.bootstrap_servers == "192.168.1.100:9093"
        assert settings.kafka.security_protocol == "SASL_SSL"
        assert settings.kafka.sasl_mechanism == "SCRAM-SHA-512"
        assert settings.kafka.username is not None
        assert settings.kafka.username.get_secret_value() == "test_user"
        assert settings.kafka.password is not None
        assert settings.kafka.password.get_secret_value() == "test_pass"
        assert settings.kafka.ca_cert_path == "/certs/ca.pem"
        assert settings.kafka.client_cert_path == "/certs/client.pem"
        assert settings.kafka.client_key_path == "/certs/client-key.pem"

    def test_explicit_kafka_bootstrap_not_overridden(self, tmp_path: Path) -> None:
        """用户显式配置了 KAFKA_BOOTSTRAP_SERVERS 时，不被 cloud 继承覆盖。"""
        _write_env(
            tmp_path,
            "HUNTER_CORE_KAFKA_BOOTSTRAP=192.168.1.100:9093\n"
            "HUNTER_CORE_SASL_USERNAME=test_user\n"
            "HUNTER_CORE_SASL_PASSWORD=test_pass\n"
            "KAFKA_BOOTSTRAP_SERVERS=10.0.0.1:9092\n"
            "KAFKA_SECURITY_PROTOCOL=SASL_PLAINTEXT\n",
        )
        settings = HunterSimSettings()
        # 显式设定的不被覆盖
        assert settings.kafka.bootstrap_servers == "10.0.0.1:9092"
        assert settings.kafka.security_protocol == "SASL_PLAINTEXT"
        # 未显式设定的仍继承
        assert settings.kafka.sasl_mechanism == "SCRAM-SHA-512"
        assert settings.kafka.username is not None
        assert settings.kafka.username.get_secret_value() == "test_user"

    def test_blank_cloud_sasl_does_not_trigger(self, tmp_path: Path) -> None:
        """HUNTER_CORE_SASL_USERNAME= 留空时不触发继承（等同未配置）。"""
        _write_env(
            tmp_path,
            "HUNTER_CORE_KAFKA_BOOTSTRAP=192.168.1.100:9093\n"
            "HUNTER_CORE_SASL_USERNAME=\n"
            "HUNTER_CORE_SASL_PASSWORD=\n",
        )
        settings = HunterSimSettings()
        # 留空 = blank_is_unset → 不进入 model_fields_set → 不触发继承
        assert settings.kafka.bootstrap_servers == "localhost:9092"
        assert settings.kafka.security_protocol == "PLAINTEXT"
