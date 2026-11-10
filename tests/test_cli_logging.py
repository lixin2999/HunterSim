"""CLI 服务入口的日志装配测试（V3.6.0）。

覆盖两件此前一直缺位的事：
- ``configure_logging`` 从未被任何入口调用，导致 ``HUNTER_SIM_LOG_LEVEL`` /
  ``HUNTER_SIM_LOG_JSON`` / ``HUNTER_SIM_LOG_FILE`` 与线程名、访问日志降噪
  在真实进程里不生效；
- ``uvicorn.run`` 仍带自己的 dictConfig，会输出无前缀的 ``INFO:     ...`` 行，
  必须以 ``log_config=None`` 关掉，日志出口统一交给 ``configure_logging``。

不启动真实服务：uvicorn 与 app 工厂均以替身注入。
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any, Iterator

import pytest

from hunter_sim.cli import main as cli
from hunter_sim.common.models import HunterSimSettings


@pytest.fixture
def recorded_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[dict[str, Any]]]:
    """拦截 ``configure_logging``，记录入口实际下发的参数（不动全局日志状态）。"""
    calls: list[dict[str, Any]] = []

    def _fake(log_level: str = "INFO", **kwargs: Any) -> int:
        calls.append({"log_level": log_level, **kwargs})
        return logging.INFO

    monkeypatch.setattr("hunter_sim.common.utils.configure_logging", _fake)
    yield calls


def _args(**overrides: Any) -> types.SimpleNamespace:
    base: dict[str, Any] = {"log_level": "info"}
    base.update(overrides)
    return types.SimpleNamespace(**base)


class TestApplyLoggingConfig:
    def test_cli_debug_wins_over_env_level(self, recorded_levels: list[dict[str, Any]]) -> None:
        """显式 ``--log-level debug`` 用于现场排障，优先于 HUNTER_SIM_LOG_LEVEL。"""
        settings = HunterSimSettings.model_construct(
            log_level="WARNING", log_json=False, log_file="", log_max_bytes=1024, log_backup_count=1
        )

        cli._apply_logging_config(_args(log_level="debug"), settings)  # noqa: SLF001

        assert recorded_levels[0]["log_level"] == "DEBUG"

    def test_env_level_used_when_cli_stays_default(
        self, recorded_levels: list[dict[str, Any]]
    ) -> None:
        settings = HunterSimSettings.model_construct(
            log_level="WARNING", log_json=False, log_file="", log_max_bytes=1024, log_backup_count=1
        )

        cli._apply_logging_config(_args(), settings)  # noqa: SLF001

        assert recorded_levels[0]["log_level"] == "WARNING"

    def test_json_and_file_flags_are_forwarded(
        self, recorded_levels: list[dict[str, Any]]
    ) -> None:
        settings = HunterSimSettings.model_construct(
            log_level="INFO",
            log_json=True,
            log_file="logs/huntersim.log",
            log_max_bytes=4096,
            log_backup_count=3,
        )

        cli._apply_logging_config(_args(), settings)  # noqa: SLF001

        payload = recorded_levels[0]
        assert payload["json_logs"] is True
        assert payload["log_file"] == "logs/huntersim.log"
        assert payload["log_max_bytes"] == 4096
        assert payload["log_backup_count"] == 3

    def test_falls_back_to_cli_level_when_settings_broken(
        self, recorded_levels: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置校验失败也不能让服务无日志出口（入口仍要装配一次）。"""
        monkeypatch.setattr(cli, "_load_settings", lambda: (None, "ValidationError: bad enum"))

        cli._apply_logging_config(_args(log_level="warning"))  # noqa: SLF001

        assert recorded_levels[0] == {"log_level": "WARNING"}


class TestLogLevelAliases:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("critical", "CRITICAL"),
            ("error", "ERROR"),
            ("WARNING", "WARNING"),
            ("info", "INFO"),
            ("debug", "DEBUG"),
            ("trace", "DEBUG"),  # stdlib 无 TRACE
            ("nonsense", "INFO"),
        ],
    )
    def test_alias_map(self, given: str, expected: str) -> None:
        level = cli._LOG_LEVEL_NAMES.get(given.lower(), "INFO")  # noqa: SLF001

        assert level == expected


class TestUvicornStartupFlags:
    """两个服务子命令都必须以 ``log_config=None`` 启动 uvicorn。"""

    @pytest.fixture
    def fake_uvicorn(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        module = types.ModuleType("uvicorn")

        def _run(app: Any, **kwargs: Any) -> None:  # noqa: ARG001
            captured.update(kwargs)

        module.run = _run  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "uvicorn", module)
        return captured

    def test_carla_manager_uses_unified_logging(
        self,
        fake_uvicorn: dict[str, Any],
        recorded_levels: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "hunter_sim.platform_adapter.app.create_app", lambda **kwargs: object()
        )
        settings = HunterSimSettings.model_construct(
            log_level="ERROR", log_json=False, log_file="", log_max_bytes=1024, log_backup_count=1
        )
        monkeypatch.setattr(cli, "_load_settings", lambda: (settings, None))
        args = _manager_args()

        assert cli._cmd_carla_manager(args) == cli.EXIT_OK  # noqa: SLF001
        assert fake_uvicorn["log_config"] is None
        assert recorded_levels[0]["log_level"] == "ERROR"

    def test_probes_uses_unified_logging(
        self,
        fake_uvicorn: dict[str, Any],
        recorded_levels: list[dict[str, Any]],  # noqa: ARG001
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = HunterSimSettings()
        monkeypatch.setattr(cli, "_load_settings", lambda: (settings, None))
        monkeypatch.setattr(
            "hunter_sim.probes.app.create_probes_app", lambda _settings: object()
        )
        args = _probes_args()

        assert cli._cmd_probes(args) == cli.EXIT_OK  # noqa: SLF001
        assert fake_uvicorn["log_config"] is None


def _manager_args() -> types.SimpleNamespace:
    """``carla-manager`` 参数替身（只给本用例关心的字段）。"""
    return types.SimpleNamespace(host="", port=0, db_path="", log_level="info")


def _probes_args() -> types.SimpleNamespace:
    """``probes`` 参数替身。"""
    return types.SimpleNamespace(host="", port=0, adapter_url="", log_level="info")
