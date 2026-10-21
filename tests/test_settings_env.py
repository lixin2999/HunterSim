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
