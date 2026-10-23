"""配置加载测试：`.env` 对各前缀分组生效、环境变量优先、未知键容错（2.1.2）。

历史问题：`.env` 中写 `CARLA_HOST` 会因 `HunterSimSettings` 的 `extra=forbid`
直接抛 ValidationError，且嵌套分组未配置 `env_file`，导致文档宣称的
“环境变量 > .env > 默认值” 对 `CARLA_*/KAFKA_*/VIL_*/API_*/RESOURCE_*` 不成立。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter_sim.common.models import HunterSimSettings

_ENV_KEYS = (
    "CARLA_HOST",
    "CARLA_RPC_PORT",
    "API_PORT",
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
        "HUNTER_SIM_ENV=staging\nCARLA_HOST=10.1.2.3\nCARLA_RPC_PORT=2100\nAPI_PORT=9090\n",
    )
    settings = HunterSimSettings()
    assert settings.env == "staging"
    assert settings.carla.host == "10.1.2.3"
    assert settings.carla.rpc_port == 2100
    assert settings.api.port == 9090


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


def test_cloud_and_gui_dotenv_apply(tmp_path: Path) -> None:
    """V2.2.0：HUNTER_CORE_* / GUI_* 前缀 `.env` 键分别被 cloud / gui 分组读取。"""
    _write_env(
        tmp_path,
        "HUNTER_CORE_ENABLED=true\n"
        "HUNTER_CORE_SIM_ID=SIM_X9\n"
        "HUNTER_CORE_KAFKA_BOOTSTRAP=192.168.31.35:9092\n"
        "HUNTER_CORE_JWT_TOKEN=abc.def.ghi\n"
        "GUI_THEME=light\n"
        "GUI_DEFAULT_MAP=Town05\n"
        "GUI_STREAM_FPS=24\n",
    )
    settings = HunterSimSettings()
    assert settings.cloud.enabled is True
    assert settings.cloud.sim_id == "SIM_X9"
    assert settings.cloud.kafka_bootstrap == "192.168.31.35:9092"
    assert settings.cloud.jwt_token is not None
    assert settings.cloud.jwt_token.get_secret_value() == "abc.def.ghi"
    assert settings.gui.theme == "light"
    assert settings.gui.default_map == "Town05"
    assert settings.gui.stream_fps == 24.0


def test_cloud_defaults_are_noop(tmp_path: Path) -> None:
    """未配置时 cloud 默认关闭、指向单机验证环境；gui 默认深色。"""
    settings = HunterSimSettings()
    assert settings.cloud.enabled is False
    assert settings.cloud.kafka_bootstrap == "192.168.31.35:9092"
    assert settings.cloud.sensor_data_topic_template == "carla.{sim_id}.sensor_data"
    assert settings.gui.theme == "dark"
    assert settings.gui.scenario_runner_path is None


def test_cloud_unknown_keys_ignored(tmp_path: Path) -> None:
    """HUNTER_CORE_ 前缀下的未知键不得触发校验错误。"""
    _write_env(tmp_path, "HUNTER_CORE_TYPO_FIELD=1\nGUI_TYPO=2\n")
    settings = HunterSimSettings()
    assert settings.cloud.enabled is False
