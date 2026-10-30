"""部署预检 CLI 测试（V3.0.0）。

覆盖 ``huntersim doctor`` 的判定口径与退出码语义：
哪些项算 fail（阻断上线）、哪些只 warn/skip、``--strict`` 如何把 warn 计失败，
以及 ``--json`` 结构与 ``version``/``probes``/无子命令的解析。
网络与磁盘探测全部打桩，不依赖真实 CARLA/nvidia-smi/Kafka。
"""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from typing import Any

import pytest

from hunter_sim.cli import main as cli
from hunter_sim.cli.main import (
    EXIT_BAD_ARGS,
    EXIT_FAIL,
    EXIT_OK,
    CheckResult,
    _check_adapter_db,
    _check_disk,
    _check_kafka,
    _check_mcap,
    _check_platform,
    _check_ports,
    _check_recordings_dir,
    _counts,
    _render_text,
    build_parser,
    main,
    run_doctor,
)
from hunter_sim.common.models import (
    AdapterSettings,
    CarlaSettings,
    ExecutionSettings,
    HunterSimSettings,
    KafkaSettings,
    ProbeSettings,
)


def _settings(
    *,
    env: str = "dev",
    db_path: str = "data/adapter/carla_instances.db",
    recordings_dir: str = "data/adapter/recordings",
    adapter_port: int = 8090,
    probe_port: int = 8080,
    supervisor_enabled: bool = False,
    supervisor_binary_path: str = "",
    bootstrap_servers: str = "localhost:9092",
) -> HunterSimSettings:
    return HunterSimSettings(
        env=env,
        carla=CarlaSettings(
            host="127.0.0.1",
            rpc_port=2000,
            supervisor_enabled=supervisor_enabled,
            supervisor_binary_path=supervisor_binary_path,
        ),
        execution=ExecutionSettings(engine_endpoints=""),
        kafka=KafkaSettings(bootstrap_servers=bootstrap_servers),
        adapter=AdapterSettings(
            db_path=db_path, recordings_dir=recordings_dir, port=adapter_port
        ),
        probe=ProbeSettings(port=probe_port),
    )


def _free_port() -> int:
    """取一个当前空闲的本地端口，避免端口检查项误判。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gpu_passed() -> SimpleNamespace:
    return SimpleNamespace(
        passed=True,
        os_ok=True,
        gpu_driver_ok=True,
        gpu_name="RTX 4080",
        gpu_driver_version="550.00",
        cuda_ok=True,
        cuda_version="12.4",
        gpu_memory_ok=True,
        gpu_memory_gb=16.0,
        gpu_compute_capability="8.9",
        errors=[],
    )


@pytest.fixture(autouse=True)
def _no_external_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认不发真实网络/子进程探测（CARLA/Kafka 视为可达、GPU 环境视为通过）。"""
    monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: True)
    monkeypatch.setattr(cli, "check_environment", _gpu_passed)


def _by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {item.name: item for item in results}


class TestDoctorChecks:
    def test_happy_path_has_no_failure(self, tmp_path: Any) -> None:
        settings = _settings(
            db_path=str(tmp_path / "db" / "instances.db"),
            recordings_dir=str(tmp_path / "rec"),
            adapter_port=_free_port(),
            probe_port=_free_port(),
        )
        results = run_doctor(settings, base_path=".")
        assert [item.name for item in results if item.status == "fail"] == []
        assert _by_name(results)["config"].status == "ok"
        assert _by_name(results)["python"].status == "ok"
        assert _by_name(results)["adapter_db"].status == "ok"
        assert _by_name(results)["recordings_dir"].status == "ok"
        assert _by_name(results)["carla_reachable"].status == "ok"

    def test_carla_unreachable_fails_without_supervisor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: False)
        results = _by_name(run_doctor(_settings()))

        assert results["carla_reachable"].status == "fail"
        assert "CARLA_SUPERVISOR_ENABLED" in results["carla_reachable"].hint

    def test_carla_unreachable_downgrades_when_self_heal_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配了进程托管就只是 warn：服务启动时会自动拉起，不必人工先起引擎。"""
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: False)
        results = _by_name(
            run_doctor(_settings(supervisor_enabled=True, supervisor_binary_path="CarlaUE4.exe"))
        )

        assert results["carla_reachable"].status == "warn"
        assert "自动拉起" in results["carla_reachable"].hint

    def test_multi_engine_endpoints_are_all_probed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, int]] = []

        def spy(host: str, port: int, timeout: float = 1.5) -> bool:
            seen.append((host, port))
            return True

        monkeypatch.setattr(cli, "_tcp_open", spy)
        settings = _settings()
        settings.execution.engine_endpoints = "10.0.0.1:2000,10.0.0.2:2100"
        results = run_doctor(settings)

        # Kafka 检查也会走 _tcp_open，这里只断言引擎端点的探测顺序
        assert [item for item in seen if item[0].startswith("10.0.0.")] == [
            ("10.0.0.1", 2000),
            ("10.0.0.2", 2100),
        ]
        assert [item.status for item in results if item.name == "carla_reachable"] == ["ok", "ok"]

    def test_config_load_failure_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_load_settings", lambda: (None, "ValidationError: bad enum"))
        results = run_doctor(None)

        assert len(results) == 1
        assert results[0].name == "config"
        assert results[0].status == "fail"
        assert "bad enum" in results[0].detail

    def test_strict_promotes_warn_to_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: False)
        loose = _by_name(run_doctor(_settings()))
        assert loose["kafka"].status == "warn"

        strict = _by_name(run_doctor(_settings(), strict=True))
        assert strict["kafka"].status == "fail"


class TestStorageChecks:
    def test_adapter_db_writable(self, tmp_path: Any) -> None:
        db_path = tmp_path / "adapter" / "instances.db"
        result = _check_adapter_db(_settings(db_path=str(db_path)))

        assert result.status == "ok"
        assert db_path.exists()

    def test_adapter_db_failure_is_fail_not_exception(self, tmp_path: Any) -> None:
        blocker = tmp_path / "occupied"
        blocker.mkdir()  # 把库路径指向目录 → 建库必然失败
        result = _check_adapter_db(_settings(db_path=str(blocker)))

        assert result.status == "fail"
        assert "写权限" in result.hint

    def test_recordings_dir_requires_free_space(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gb = 1024**3
        target = tmp_path / "rec"
        monkeypatch.setattr(
            cli.shutil,
            "disk_usage",
            lambda path: SimpleNamespace(total=100 * gb, free=1 * gb),
        )
        result = _check_recordings_dir(_settings(recordings_dir=str(target)))

        assert result.status == "fail"
        assert "RETENTION_HOURS" in result.hint

    def test_recordings_dir_unusable_reports_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_: Any) -> Any:
            raise OSError("volume not ready")

        monkeypatch.setattr(cli.shutil, "disk_usage", boom)
        result = _check_recordings_dir(_settings())

        assert result.status == "fail"
        assert "volume not ready" in result.detail

    def test_disk_thresholds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gb = 1024**3
        cases = {1.0: "fail", 10.0: "warn", 500.0: "ok"}
        for free_gb, expected in cases.items():
            monkeypatch.setattr(
                cli.shutil,
                "disk_usage",
                lambda path: SimpleNamespace(total=1 * gb, free=free_gb * gb),
            )
            assert _check_disk(".").status == expected

    def test_disk_check_skips_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_: str) -> Any:
            raise OSError("volume not ready")

        monkeypatch.setattr(cli.shutil, "disk_usage", boom)
        result = _check_disk(".")
        assert result.status == "skip"
        assert "volume not ready" in result.detail


class TestMcapAndKafkaChecks:
    def test_mcap_ok_when_dependencies_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
        assert _check_mcap().status == "ok"

    def test_mcap_missing_warns_and_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)
        result = _check_mcap()

        assert result.status == "warn"
        assert "no-op" in result.detail

    def test_kafka_reachable(self) -> None:
        assert _check_kafka(_settings()).status == "ok"

    def test_kafka_unreachable_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: False)
        result = _check_kafka(_settings())

        assert result.status == "warn"
        assert "KAFKA_BOOTSTRAP_SERVERS" in result.hint

    def test_kafka_empty_bootstrap_is_skip(self) -> None:
        assert _check_kafka(_settings(bootstrap_servers="")).status == "skip"

    def test_kafka_bad_format_warns(self) -> None:
        result = _check_kafka(_settings(bootstrap_servers="not-a-host-port"))
        assert result.status == "warn"
        assert "host:port" in result.hint


class TestPlatformCheck:
    """平台接入项：未启用即 skip，网关不可达只 warn（不阻断本机场景）。"""

    def test_disabled_is_skip(self) -> None:
        result = _check_platform(_settings())

        assert result.status == "skip"
        assert "HUNTER_CORE_ENABLED" in result.detail

    def test_reachable_gateway_is_ok(self) -> None:
        settings = _settings()
        settings.cloud.enabled = True
        settings.cloud.gateway_url = "http://10.0.0.5:8080"

        result = _check_platform(settings)

        assert result.status == "ok"
        assert "10.0.0.5:8080" in result.detail

    def test_unreachable_gateway_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: False)
        settings = _settings()
        settings.cloud.enabled = True
        settings.cloud.gateway_url = "http://10.0.0.5:8080"

        result = _check_platform(settings)

        assert result.status == "warn"
        assert "遥测/上传将降级" in result.detail

    def test_malformed_url_warns(self) -> None:
        settings = _settings()
        settings.cloud.enabled = True
        settings.cloud.gateway_url = "not-a-url"

        result = _check_platform(settings)

        assert result.status == "warn"
        assert "不可解析" in result.detail


class TestPortChecks:
    def test_adapter_and_probe_ports_both_checked(self) -> None:
        results = _check_ports(_settings(adapter_port=_free_port(), probe_port=_free_port()))
        assert [item.name for item in results] == ["adapter_port", "probe_port"]
        assert [item.status for item in results] == ["ok", "ok"]

    def test_busy_port_is_warn_when_reachable(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            settings = _settings(adapter_port=int(held.getsockname()[1]))
            assert _check_ports(settings)[0].status == "warn"

    def test_busy_port_is_fail_when_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: False)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            settings = _settings(adapter_port=int(held.getsockname()[1]))
            assert _check_ports(settings)[0].status == "fail"


class TestGpuCheckIsDefensive:
    def test_gpu_env_collapsed_when_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "check_environment", lambda: _gpu_passed())
        results = cli._check_gpu()

        assert [item.name for item in results] == ["gpu_env"]
        assert results[0].status == "ok"

    def test_gpu_env_expands_details_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failed = SimpleNamespace(
            passed=False,
            os_ok=True,
            gpu_driver_ok=False,
            gpu_name="",
            gpu_driver_version="",
            cuda_ok=False,
            cuda_version="",
            gpu_memory_ok=False,
            gpu_memory_gb=0.0,
            gpu_compute_capability=None,
            errors=["no nvidia-smi"],
        )
        monkeypatch.setattr(cli, "check_environment", lambda: failed)
        results = {item.name: item for item in cli._check_gpu()}

        assert set(results) == {"os", "gpu_driver", "cuda", "gpu_memory"}
        assert results["gpu_driver"].status == "warn"
        assert "510" in results["gpu_driver"].hint

    def test_probe_exception_becomes_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise RuntimeError("nvidia-smi missing")

        monkeypatch.setattr(cli, "check_environment", boom)
        results = cli._check_gpu()

        assert len(results) == 1
        assert results[0].status == "skip"

    def test_cuda_detail_separates_compute_capability(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 计算能力 8.9 不得被当作 CUDA 版本展示/判定（RTX 40 系误报 CUDA < 11）
        partial = SimpleNamespace(
            passed=False,
            os_ok=True,
            gpu_driver_ok=False,
            gpu_name="NVIDIA GeForce RTX 4070",
            gpu_driver_version="617.42",
            cuda_ok=True,
            cuda_version="13.0",
            gpu_memory_ok=True,
            gpu_memory_gb=12.0,
            gpu_compute_capability="8.9",
            errors=[],
        )
        monkeypatch.setattr(cli, "check_environment", lambda: partial)
        results = {item.name: item for item in cli._check_gpu()}

        assert results["cuda"].status == "ok"
        assert "CUDA 13.0" in results["cuda"].detail
        assert "计算能力 8.9" in results["cuda"].detail
        assert results["cuda"].hint == ""


class TestRenderingAndExitCode:
    def test_counts_cover_all_statuses(self) -> None:
        counts = _counts(
            [
                CheckResult("a", "ok", ""),
                CheckResult("b", "warn", ""),
                CheckResult("c", "fail", ""),
                CheckResult("d", "skip", ""),
            ]
        )
        assert counts == {"ok": 1, "warn": 1, "fail": 1, "skip": 1}

    def test_render_text_shows_hint_and_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: False)
        text = _render_text([_check_kafka(_settings())])

        assert "kafka" in text
        assert "→" in text  # 非 ok 项带修复建议
        assert "Summary:" in text

    def test_parser_defaults(self) -> None:
        args = build_parser().parse_args(["doctor"])
        assert (args.json, args.strict, args.path) == (False, False, ".")

    def test_probes_parser_defaults(self) -> None:
        args = build_parser().parse_args(["probes"])
        assert args.command == "probes"
        assert (args.host, args.port, args.adapter_url) == ("", 0, "")
        assert args.handler is cli._cmd_probes

    def test_serve_subcommand_is_gone(self) -> None:
        """V3.0.0：``serve`` 改名 ``probes``，旧名不应再被接受。"""
        with pytest.raises(SystemExit):
            build_parser().parse_args(["serve"])

    def test_doctor_json_output_and_zero_exit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Any
    ) -> None:
        settings = _settings(
            db_path=str(tmp_path / "db" / "instances.db"),
            recordings_dir=str(tmp_path / "rec"),
            adapter_port=_free_port(),
            probe_port=_free_port(),
        )
        monkeypatch.setattr(cli, "_load_settings", lambda: (settings, None))

        assert main(["doctor", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)

        assert set(payload) == {"version", "env", "checks", "counts"}
        assert payload["env"] == "dev"
        assert payload["counts"]["fail"] == 0
        assert {"name", "status", "detail", "hint"} <= set(payload["checks"][0])

    def test_doctor_exits_one_when_carla_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_load_settings",
            lambda: (
                _settings(
                    db_path=str(tmp_path / "db" / "i.db"),
                    recordings_dir=str(tmp_path / "rec"),
                ),
                None,
            ),
        )
        monkeypatch.setattr(cli, "_tcp_open", lambda host, port, timeout=1.5: False)

        assert main(["doctor"]) == EXIT_FAIL

    def test_doctor_strict_exits_one_on_warnings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_load_settings",
            lambda: (
                _settings(
                    db_path=str(tmp_path / "db" / "i.db"),
                    recordings_dir=str(tmp_path / "rec"),
                    bootstrap_servers="127.0.0.1:1",
                ),
                None,
            ),
        )
        monkeypatch.setattr(
            cli,
            "_tcp_open",
            lambda host, port, timeout=1.5: port != 1,  # Kafka 不可达 → warn
        )

        assert main(["doctor"]) == EXIT_OK  # warn 不阻断
        assert main(["doctor", "--strict"]) == EXIT_FAIL

    def test_doctor_text_output_carries_version(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Any
    ) -> None:
        monkeypatch.setattr(
            cli,
            "_load_settings",
            lambda: (
                _settings(
                    db_path=str(tmp_path / "db" / "i.db"),
                    recordings_dir=str(tmp_path / "rec"),
                    adapter_port=_free_port(),
                    probe_port=_free_port(),
                ),
                None,
            ),
        )
        monkeypatch.setattr(cli, "_version_text", lambda: "3.0.0")

        assert main(["doctor"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "HunterSim doctor (version=3.0.0)" in out
        assert "Summary:" in out

    def test_version_command(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(cli, "_version_text", lambda: "3.0.0")

        assert main(["version"]) == EXIT_OK
        assert capsys.readouterr().out.strip() == "3.0.0"

    def test_missing_subcommand_returns_bad_args(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == EXIT_BAD_ARGS
        assert "usage" in capsys.readouterr().out.lower()
