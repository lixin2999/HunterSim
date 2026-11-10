"""``huntersim up`` 一键启动编排测试（V3.1.0）。

编排器依赖全部可注入（``popen`` / ``opener`` / ``probe`` / ``sleeper`` / ``emit``），
因此这里不启动真实进程、不打开浏览器、不占用端口：

- 服务清单：控制面必启、探针面可选，地址取自配置；
- 就绪等待：子进程提前退出即失败，必需服务不就绪返回 1，可选服务失败只提示；
- 生命周期：任一同管子进程退出返回其退出码，Ctrl+C 统一 terminate（必要时 kill）；
- 崩溃自愈（V3.5.4）：子进程非零退出按退避自动重启且不牵连其余服务，
  正常退出（code=0）/ Ctrl+C / ``--no-restart`` 均不重启，连续崩溃触顶后停止守护；
- 就绪回调：``on_ready`` 在服务全部就绪后执行一次，抛异常只提示；``up --demo`` 用它建冒烟场景；
- CLI：``up`` 参数默认值、预检 fail 中止、``--force`` / ``--no-browser`` / ``--no-probes`` / ``--demo``。
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
from typing import Any, Callable, Optional, Sequence

import pytest

from hunter_sim.cli import main as cli
from hunter_sim.cli.main import EXIT_FAIL, CheckResult, build_parser, main
from hunter_sim.common.models import HunterSimSettings, ProbeSettings
from hunter_sim.platform_adapter.config import AdapterSettings
from hunter_sim.visualization import launcher as launcher_module
from hunter_sim.visualization.launcher import (
    Launcher,
    ServiceSpec,
    build_service_specs,
    describe_exit_code,
    http_ok,
)

_VIZ_URL = "http://127.0.0.1:8090/viz"


def _settings(adapter_port: int = 8090, probe_port: int = 8080, host: str = "127.0.0.1") -> HunterSimSettings:
    return HunterSimSettings(
        env="dev",
        adapter=AdapterSettings(host=host, port=adapter_port),
        probe=ProbeSettings(host=host, port=probe_port),
    )


def _manager() -> ServiceSpec:
    return ServiceSpec(
        name="carla-manager",
        host="0.0.0.0",
        port=8090,
        argv=("python", "-m", "hunter_sim.cli.main", "carla-manager"),
    )


def _probes() -> ServiceSpec:
    return ServiceSpec(
        name="probes",
        host="127.0.0.1",
        port=8080,
        argv=("python", "-m", "hunter_sim.cli.main", "probes"),
        required=False,
    )


class _Process:
    """子进程替身：``poll`` 按给定序列返回，用于模拟「运行中 → 退出」。"""

    def __init__(self, codes: Optional[list[Optional[int]]] = None) -> None:
        self._codes = list(codes or [None])
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.wait_raises = False

    def poll(self) -> Optional[int]:
        if len(self._codes) > 1:
            return self._codes.pop(0)
        return self._codes[0]

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: Optional[float] = None) -> int:
        self.wait_calls += 1
        if self.wait_raises:
            raise subprocess.TimeoutExpired("svc", timeout or 0.0)
        return 0

    def kill(self) -> None:
        self.killed = True


def _launcher(
    *,
    popen: Optional[Callable[[Sequence[str]], Any]] = None,
    probe: Optional[Callable[[str], bool]] = None,
    opener: Optional[Callable[[str], Any]] = None,
    sleeper: Optional[Callable[[float], None]] = None,
    emit: Optional[Callable[[str], None]] = None,
) -> Launcher:
    return Launcher(
        popen=popen if popen is not None else (lambda argv: _Process()),
        probe=probe if probe is not None else (lambda url: True),
        opener=opener if opener is not None else (lambda url: True),
        sleeper=sleeper if sleeper is not None else (lambda seconds: None),
        emit=emit if emit is not None else (lambda text: None),
    )


def _interrupt(_: float) -> None:
    """守护循环里的「Ctrl+C」。"""
    raise KeyboardInterrupt


def _probe_starts_cold(ports: Sequence[str] = (":8090", ":8080")) -> Callable[[str], bool]:
    """每个端口的首次预探测返回「未运行」（触发拉起），之后一律视为就绪。"""
    cold: set[str] = set(ports)

    def probe(url: str) -> bool:
        for port in list(cold):
            if port in url:
                cold.discard(port)
                return False
        return True

    return probe


class _BytesResponse:
    """``urllib`` 响应替身：只提供 ``read`` 与上下文管理。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_BytesResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


class TestServiceSpec:
    def test_wildcard_host_falls_back_to_loopback(self) -> None:
        spec = _manager()

        assert spec.url == "http://127.0.0.1:8090"
        assert spec.health_url() == "http://127.0.0.1:8090/health"

    def test_explicit_host_is_kept(self) -> None:
        spec = ServiceSpec(name="probes", host="10.0.0.8", port=8080, argv=())

        assert spec.url == "http://10.0.0.8:8080"


class TestBuildServiceSpecs:
    def test_manager_is_required_and_first(self) -> None:
        specs = build_service_specs(_settings(), python="python", include_probes=False)

        assert [spec.name for spec in specs] == ["carla-manager"]
        assert specs[0].required is True
        assert specs[0].port == 8090
        assert specs[0].argv[:4] == ("python", "-m", "hunter_sim.cli.main", "carla-manager")

    def test_probes_is_optional(self) -> None:
        specs = build_service_specs(_settings(), python="python", include_probes=True)

        assert [spec.name for spec in specs] == ["carla-manager", "probes"]
        assert specs[1].required is False
        assert specs[1].port == 8080

    def test_addresses_follow_settings(self) -> None:
        specs = build_service_specs(_settings(adapter_port=18090, probe_port=18080, host="127.0.0.1"))

        assert [spec.port for spec in specs] == [18090, 18080]
        assert all("127.0.0.1" in spec.url for spec in specs)


class TestHttpProbe:
    def test_2xx_is_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Response:
            status = 204

            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *args: Any) -> bool:
                return False

        monkeypatch.setattr(
            launcher_module.urllib.request, "urlopen", lambda url, timeout=0: _Response()
        )

        assert http_ok("http://127.0.0.1:8090/health") is True

    def test_network_error_is_not_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(url: str, timeout: float = 0.0) -> Any:
            raise urllib.error.URLError("down")

        monkeypatch.setattr(launcher_module.urllib.request, "urlopen", boom)

        assert http_ok("http://127.0.0.1:8090/health") is False


class TestRunLifecycle:
    def test_starts_service_opens_browser_and_reports_exit(self) -> None:
        emitted: list[str] = []
        launched: list[list[str]] = []
        opened: list[str] = []
        responses = iter([False, True])  # 预探测=未运行；就绪轮询=就绪
        process = _Process(codes=[None, 0])  # 就绪后退出（返回 0）
        launcher = _launcher(
            popen=lambda argv: (launched.append(list(argv)), process)[1],
            probe=lambda url: next(responses),
            opener=lambda url: (opened.append(url), True)[1],
            emit=emitted.append,
        )

        code = launcher.run([_manager()], _VIZ_URL)

        assert code == 0
        assert launched == [["python", "-m", "hunter_sim.cli.main", "carla-manager"]]
        assert opened == [_VIZ_URL]
        assert any("就绪" in line for line in emitted)
        assert any("已退出" in line for line in emitted)

    def test_running_service_is_reused(self) -> None:
        emitted: list[str] = []
        launcher = _launcher(probe=lambda url: True, sleeper=_interrupt, emit=emitted.append)

        assert launcher.run([_manager()], _VIZ_URL) == 0
        assert any("复用现有进程" in line for line in emitted)
        assert any("未接管任何子进程" in line for line in emitted)

    def test_required_service_not_ready_returns_one(self) -> None:
        emitted: list[str] = []
        launcher = _launcher(probe=lambda url: False, emit=emitted.append)

        assert launcher.run([_manager()], _VIZ_URL) == 1
        assert any("未在" in line and "就绪" in line for line in emitted)

    def test_optional_service_failure_does_not_block(self) -> None:
        emitted: list[str] = []
        specs = build_service_specs(_settings(), python="python", include_probes=True)
        opened: list[str] = []

        def probe(url: str) -> bool:
            return ":8090" in url  # 控制面已在运行，探针面未就绪

        launcher = _launcher(
            popen=lambda argv: _Process(codes=[0]),
            probe=probe,
            opener=lambda url: (opened.append(url), True)[1],
            emit=emitted.append,
        )

        assert launcher.run(specs, _VIZ_URL) == 0
        assert opened == [_VIZ_URL]
        assert any("非必需" in line for line in emitted)

    def test_interrupt_stops_children(self) -> None:
        emitted: list[str] = []
        process = _Process(codes=[None])
        responses = iter([False, True])
        launcher = _launcher(
            popen=lambda argv: process,
            probe=lambda url: next(responses),
            sleeper=_interrupt,
            emit=emitted.append,
        )

        assert launcher.run([_manager()], _VIZ_URL) == 0
        assert process.terminated is True
        assert any("已停止" in line for line in emitted)

    def test_finished_child_is_not_terminated(self) -> None:
        process = _Process(codes=[0])
        launcher = _launcher(popen=lambda argv: process, probe=lambda url: False)

        assert launcher.run([_manager()], _VIZ_URL) == 1
        assert process.terminated is False

    def test_terminate_timeout_escalates_to_kill(self) -> None:
        process = _Process(codes=[None])
        process.wait_raises = True
        responses = iter([False, True])
        launcher = _launcher(
            popen=lambda argv: process,
            probe=lambda url: next(responses),
            sleeper=_interrupt,
        )

        assert launcher.run([_manager()], _VIZ_URL) == 0
        assert process.killed is True

    def test_browser_failure_is_reported_not_fatal(self) -> None:
        emitted: list[str] = []
        responses = iter([False, True])

        def opener(url: str) -> Any:
            raise RuntimeError("no desktop")

        launcher = _launcher(
            popen=lambda argv: _Process(codes=[None]),
            probe=lambda url: next(responses),
            opener=opener,
            sleeper=_interrupt,
            emit=emitted.append,
        )

        assert launcher.run([_manager()], _VIZ_URL) == 0
        assert any("手动访问" in line for line in emitted)

    def test_viz_url_is_printed_when_browser_disabled(self) -> None:
        emitted: list[str] = []
        responses = iter([False, True])
        launcher = _launcher(
            popen=lambda argv: _Process(codes=[None]),
            probe=lambda url: next(responses),
            sleeper=_interrupt,
            emit=emitted.append,
        )

        assert launcher.run([_manager()], _VIZ_URL, open_browser=False) == 0
        assert any(_VIZ_URL in line for line in emitted)


class TestGuardAutoRestart:
    """崩溃自愈（V3.5.4）：场景跑完不得把整个仿真端一起带走。

    退出码 3221226505 = 0xC0000409，是 CARLA 原生 fail-fast 带走 ``carla-manager``
    的现场特征：它不是 Python 异常，管理面内部接不住，只能由 ``up`` 在外层自愈。
    """

    CRASH = 3221226505

    def test_native_crash_restarts_manager_without_touching_probes(self) -> None:
        emitted: list[str] = []
        launched: list[list[str]] = []
        manager = _Process(codes=[None, self.CRASH])
        probes = _Process(codes=[None])
        reborn = _Process(codes=[0])
        pool = iter([manager, probes, reborn])
        probes_alive_at_restart: list[bool] = []

        def emit(line: str) -> None:
            emitted.append(line)
            if "自动重启" in line:
                probes_alive_at_restart.append(not probes.terminated)

        launcher = Launcher(
            popen=lambda argv: (launched.append(list(argv)), next(pool))[1],
            probe=_probe_starts_cold(),
            opener=lambda url: True,
            sleeper=lambda seconds: None,
            emit=emit,
        )

        assert launcher.run([_manager(), _probes()], _VIZ_URL) == 0
        assert len(launched) == 3  # manager + probes + 重启后的 manager
        assert probes_alive_at_restart == [True]  # 控制面崩溃不牵连探针面
        assert any("异常退出" in line and "自动重启" in line for line in emitted)
        assert any(
            "0xC0000409" in line and "fail-fast" in line for line in emitted
        ), "原生崩溃码未翻译成现场可读原因"
        assert any("已重启并就绪" in line and "carla-manager" in line for line in emitted)

    @pytest.mark.parametrize(
        ("code", "hex_text", "needle"),
        [
            (3221226505, "0xC0000409", "fail-fast"),  # 并发操作同一 Client 连接
            (3221225725, "0xC00000FD", "栈溢出"),  # 异常世界状态下深层递归
            (3221225477, "0xC0000005", "已销毁"),  # 引用已销毁 Actor
        ],
    )
    def test_crash_hint_is_appended_per_exit_code(
        self, code: int, hex_text: str, needle: str
    ) -> None:
        """现场两种崩溃码都要在 ``[up]`` 行里直接读到原因（不靠查表）。

        回归：日志里只有「异常退出（code=3221226505）」，拿着十进制大数无从下手。
        """
        emitted: list[str] = []
        manager = _Process(codes=[None, code])
        launcher = Launcher(
            popen=lambda argv: manager,
            probe=_probe_starts_cold(ports=(":8090",)),
            opener=lambda url: True,
            sleeper=lambda seconds: None,
            emit=emitted.append,
            restart_on_failure=False,
        )

        launcher.run([_manager()], _VIZ_URL)

        assert any(hex_text in line and needle in line for line in emitted), emitted

    def test_clean_exit_gets_no_crash_hint(self) -> None:
        """退出码 0 / 未知码不得编造崩溃原因。"""
        assert describe_exit_code(0) == ""
        assert describe_exit_code(1) == ""
        assert describe_exit_code("n/a") == ""

    def test_two_complement_form_maps_to_same_code(self) -> None:
        """经 shell 转发为补码负数时，仍译成同一个码。"""
        assert describe_exit_code(-1073740791) == describe_exit_code(3221226505)
        assert "0xC0000409" in describe_exit_code(-1073740791)

    def test_restarted_manager_keeps_waiting_for_next_scene(self) -> None:
        emitted: list[str] = []
        launched: list[list[str]] = []
        pool = iter([_Process(codes=[None, self.CRASH]), _Process(codes=[0])])
        launcher = Launcher(
            popen=lambda argv: (launched.append(list(argv)), next(pool))[1],
            probe=_probe_starts_cold(),
            opener=lambda url: True,
            sleeper=lambda seconds: None,
            emit=emitted.append,
        )

        assert launcher.run([_manager()], _VIZ_URL) == 0
        # 重启后回到守护循环，继续等下一个场景（而不是直接退出 up）
        assert any("继续等待平台下发场景" in line for line in emitted)
        assert len(launched) == 2

    def test_clean_exit_is_not_restarted(self) -> None:
        emitted: list[str] = []
        launched: list[list[str]] = []
        process = _Process(codes=[None, 0])
        launcher = Launcher(
            popen=lambda argv: (launched.append(list(argv)), process)[1],
            probe=_probe_starts_cold(),
            opener=lambda url: True,
            sleeper=lambda seconds: None,
            emit=emitted.append,
        )

        assert launcher.run([_manager()], _VIZ_URL) == 0
        assert len(launched) == 1
        assert any("已退出（code=0）" in line for line in emitted)

    def test_restart_can_be_disabled(self) -> None:
        emitted: list[str] = []
        launched: list[list[str]] = []
        process = _Process(codes=[None, self.CRASH])
        launcher = Launcher(
            popen=lambda argv: (launched.append(list(argv)), process)[1],
            probe=_probe_starts_cold(),
            opener=lambda url: True,
            sleeper=lambda seconds: None,
            emit=emitted.append,
            restart_on_failure=False,
        )

        assert launcher.run([_manager()], _VIZ_URL) == self.CRASH
        assert len(launched) == 1
        assert any("已退出" in line for line in emitted)
        assert not any("自动重启" in line for line in emitted)

    def test_repeated_crashes_eventually_stop_guarding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(launcher_module, "_RESTART_MAX_ATTEMPTS", 2)
        emitted: list[str] = []
        launched: list[list[str]] = []
        pool = iter(
            [
                _Process(codes=[None, self.CRASH]),
                _Process(codes=[self.CRASH]),
                _Process(codes=[self.CRASH]),
            ]
        )
        launcher = Launcher(
            popen=lambda argv: (launched.append(list(argv)), next(pool))[1],
            probe=_probe_starts_cold(),
            opener=lambda url: True,
            sleeper=lambda seconds: None,
            emit=emitted.append,
        )

        assert launcher.run([_manager()], _VIZ_URL) == self.CRASH
        assert len(launched) == 3  # 首次 + 两次重启后触顶
        assert any("停止守护" in line and "CarlaUE4" in line for line in emitted)

    def test_stable_service_gets_fresh_restart_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(launcher_module, "_RESTART_MAX_ATTEMPTS", 2)
        monkeypatch.setattr(launcher_module, "SERVICE_STABLE_SECONDS", 2.0)
        emitted: list[str] = []
        launched: list[list[str]] = []
        pool = iter(
            [
                _Process(codes=[None] * 5 + [self.CRASH]),
                _Process(codes=[None] * 5 + [self.CRASH]),
                _Process(codes=[0]),
            ]
        )
        launcher = Launcher(
            popen=lambda argv: (launched.append(list(argv)), next(pool))[1],
            probe=_probe_starts_cold(),
            opener=lambda url: True,
            sleeper=lambda seconds: None,
            emit=emitted.append,
        )

        # 两次崩溃都发生在「已稳定」之后，因此不应被当作重启风暴而停止守护
        assert launcher.run([_manager()], _VIZ_URL) == 0
        assert len(launched) == 3
        assert not any("停止守护" in line for line in emitted)


class TestRunOnReady:
    """就绪回调：``up --demo`` 依赖它在就绪后提交冒烟场景。"""

    def test_callback_runs_once_after_ready(self) -> None:
        calls: list[str] = []
        responses = iter([False, True])
        launcher = _launcher(
            popen=lambda argv: _Process(codes=[None]),
            probe=lambda url: next(responses),
            sleeper=_interrupt,
        )

        assert launcher.run([_manager()], _VIZ_URL, on_ready=lambda: calls.append("demo")) == 0
        assert calls == ["demo"]

    def test_callback_runs_when_service_is_reused(self) -> None:
        calls: list[str] = []
        launcher = _launcher(probe=lambda url: True, sleeper=_interrupt)

        assert launcher.run([_manager()], _VIZ_URL, on_ready=lambda: calls.append("demo")) == 0
        assert calls == ["demo"]

    def test_callback_failure_is_reported_not_fatal(self) -> None:
        emitted: list[str] = []
        responses = iter([False, True])

        def broken() -> None:
            raise RuntimeError("CARLA 未就绪")

        launcher = _launcher(
            popen=lambda argv: _Process(codes=[None]),
            probe=lambda url: next(responses),
            sleeper=_interrupt,
            emit=emitted.append,
        )

        assert launcher.run([_manager()], _VIZ_URL, on_ready=broken) == 0
        assert any("就绪后附加动作失败" in line and "CARLA 未就绪" in line for line in emitted)

    def test_callback_is_skipped_when_required_service_fails(self) -> None:
        calls: list[str] = []

        code = _launcher(probe=lambda url: False).run(
            [_manager()], _VIZ_URL, on_ready=lambda: calls.append("demo")
        )

        assert code == 1
        assert calls == []


class _FakeLauncher:
    """拦截 ``Launcher`` 的替身，记录 ``up`` 传入的清单与参数。"""

    recorded: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).recorded_kwargs = kwargs

    def run(
        self,
        specs: Sequence[ServiceSpec],
        viz_url: str,
        *,
        open_browser: bool = True,
        ready_timeout_s: float = 0.0,
        on_ready: Optional[Callable[[], None]] = None,
    ) -> int:
        type(self).recorded = {
            "specs": list(specs),
            "url": viz_url,
            "open_browser": open_browser,
            "on_ready": on_ready,
        }
        return 0


class TestUpCommand:
    def test_parser_defaults(self) -> None:
        args = build_parser().parse_args(["up"])

        assert args.handler is cli._cmd_up
        assert args.path == "."
        assert args.log_level == "info"
        assert (args.skip_doctor, args.strict, args.force, args.no_browser, args.no_probes) == (
            False,
            False,
            False,
            False,
            False,
        )
        assert args.no_restart is False
        assert args.demo is False

    def test_aborts_when_doctor_fails(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_load_settings", lambda: (_settings(), None))
        monkeypatch.setattr(
            cli,
            "run_doctor",
            lambda *args, **kwargs: [CheckResult("carla_reachable", "fail", "down", "start it")],
        )
        monkeypatch.setattr(launcher_module, "Launcher", _FakeLauncher)

        assert main(["up"]) == EXIT_FAIL
        assert "预检存在 fail 项" in capsys.readouterr().err

    def test_force_continues_and_passes_viz_url(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _FakeLauncher.recorded = {}
        monkeypatch.setattr(cli, "_load_settings", lambda: (_settings(), None))
        monkeypatch.setattr(
            cli,
            "run_doctor",
            lambda *args, **kwargs: [CheckResult("carla_reachable", "fail", "down", "start it")],
        )
        monkeypatch.setattr(launcher_module, "Launcher", _FakeLauncher)

        assert main(["up", "--force", "--no-browser"]) == 0
        recorded = _FakeLauncher.recorded
        assert recorded["url"] == "http://127.0.0.1:8090/viz"
        assert recorded["open_browser"] is False
        assert [spec.name for spec in recorded["specs"]] == ["carla-manager", "probes"]
        assert "仿真端启动计划" in capsys.readouterr().out

    def test_no_probes_and_skip_doctor(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _FakeLauncher.recorded = {}
        checked: list[int] = []

        def unexpected(*args: Any, **kwargs: Any) -> list[CheckResult]:
            checked.append(1)
            return []

        monkeypatch.setattr(cli, "_load_settings", lambda: (_settings(), None))
        monkeypatch.setattr(cli, "run_doctor", unexpected)
        monkeypatch.setattr(launcher_module, "Launcher", _FakeLauncher)

        assert main(["up", "--skip-doctor", "--no-probes"]) == 0
        assert checked == []
        assert [spec.name for spec in _FakeLauncher.recorded["specs"]] == ["carla-manager"]
        assert "已跳过预检" in capsys.readouterr().out

    def test_demo_registers_ready_callback(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _FakeLauncher.recorded = {}
        created: list[str] = []
        monkeypatch.setattr(cli, "_load_settings", lambda: (_settings(), None))
        monkeypatch.setattr(cli, "_run_demo", lambda settings: created.append("demo"))
        monkeypatch.setattr(launcher_module, "Launcher", _FakeLauncher)

        assert main(["up", "--skip-doctor", "--demo", "--no-browser"]) == 0
        on_ready = _FakeLauncher.recorded["on_ready"]
        assert callable(on_ready)
        assert created == []  # 仅登记回调，由 Launcher 在就绪后执行
        on_ready()
        assert created == ["demo"]
        capsys.readouterr()

    def test_default_up_has_no_ready_callback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _FakeLauncher.recorded = {}
        monkeypatch.setattr(cli, "_load_settings", lambda: (_settings(), None))
        monkeypatch.setattr(launcher_module, "Launcher", _FakeLauncher)

        assert main(["up", "--skip-doctor", "--no-browser"]) == 0
        assert _FakeLauncher.recorded["on_ready"] is None

    def test_crash_restart_is_on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _FakeLauncher.recorded = {}
        monkeypatch.setattr(cli, "_load_settings", lambda: (_settings(), None))
        monkeypatch.setattr(launcher_module, "Launcher", _FakeLauncher)

        assert main(["up", "--skip-doctor", "--no-browser"]) == 0
        assert _FakeLauncher.recorded_kwargs["restart_on_failure"] is True

    def test_no_restart_disables_crash_self_healing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _FakeLauncher.recorded = {}
        monkeypatch.setattr(cli, "_load_settings", lambda: (_settings(), None))
        monkeypatch.setattr(launcher_module, "Launcher", _FakeLauncher)

        assert main(["up", "--skip-doctor", "--no-browser", "--no-restart"]) == 0
        assert _FakeLauncher.recorded_kwargs["restart_on_failure"] is False
        assert "--no-restart" in capsys.readouterr().out


class TestDemoScene:
    """``up --demo`` 的请求构造与提交（不联真实控制面）。"""

    def test_payload_matches_platform_contract(self) -> None:
        payload = cli._demo_payload("demo-smoke-1", duration_s=30.0)
        config = payload["scene_config"]

        assert payload["scene_id"] == "demo-smoke-1"
        assert payload["mode"] == "sil"
        assert config["map"]["map_id"] == "Town03"
        assert set(config["map"]["spawn_point"]) == {"x", "y", "z", "yaw"}
        assert config["ego_vehicle"]["model"] == "hunter_se"
        assert config["sensors"] == [
            {"profile": "hunter_se_full", "enabled": True, "ros_enabled": False, "record": False}
        ]
        assert config["duration"] == 30.0

    def test_manager_base_url_falls_back_to_loopback(self) -> None:
        assert cli._manager_base_url(_settings(host="0.0.0.0")) == "http://127.0.0.1:8090"
        assert cli._manager_base_url(_settings(adapter_port=18090, host="10.0.0.8")) == (
            "http://10.0.0.8:18090"
        )

    def test_create_posts_utf8_json_and_returns_instance_id(self) -> None:
        captured: dict[str, Any] = {}

        def opener(request: Any, timeout: float = 0.0) -> _BytesResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return _BytesResponse(b'{"instance_id": "inst-demo"}')

        instance_id = cli._create_demo_instance(_settings(), opener=opener, scene_id="demo-smoke-1")

        assert instance_id == "inst-demo"
        request = captured["request"]
        assert request.full_url == "http://127.0.0.1:8090/api/v1/instances"
        assert request.get_method() == "POST"
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["content-type"] == "application/json; charset=utf-8"
        body = json.loads(request.data.decode("utf-8"))
        assert body["scene_id"] == "demo-smoke-1"
        # ensure_ascii=False + 显式 UTF-8：中文场景名不得退化为 “????”
        assert "一键冒烟" in body["scene_name"]
        assert captured["timeout"] == cli._DEMO_CREATE_TIMEOUT_S

    def test_scene_id_defaults_to_timestamp(self) -> None:
        seen: dict[str, Any] = {}

        def opener(request: Any, timeout: float = 0.0) -> _BytesResponse:
            seen.update(json.loads(request.data.decode("utf-8")))
            return _BytesResponse(b'{"sim_instance_id": "inst-sim"}')

        assert cli._create_demo_instance(_settings(), opener=opener) == "inst-sim"
        assert str(seen["scene_id"]).startswith("demo-smoke-")

    def test_http_failure_raises_runtime_error(self) -> None:
        def opener(request: Any, timeout: float = 0.0) -> _BytesResponse:
            raise urllib.error.URLError("connection refused")

        with pytest.raises(RuntimeError, match="创建冒烟场景失败"):
            cli._create_demo_instance(_settings(), opener=opener)

    def test_missing_instance_id_raises_runtime_error(self) -> None:
        def opener(request: Any, timeout: float = 0.0) -> _BytesResponse:
            return _BytesResponse(b'{"detail": "boom"}')

        with pytest.raises(RuntimeError, match="缺少 instance_id"):
            cli._create_demo_instance(_settings(), opener=opener)

    def test_unparsable_response_raises_runtime_error(self) -> None:
        def opener(request: Any, timeout: float = 0.0) -> _BytesResponse:
            return _BytesResponse(b"not-json")

        with pytest.raises(RuntimeError, match="响应不可解析"):
            cli._create_demo_instance(_settings(), opener=opener)

    def test_run_demo_prints_instance_and_viz_url(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli, "_create_demo_instance", lambda settings: "inst-demo")

        cli._run_demo(_settings())

        out = capsys.readouterr().out
        assert "inst-demo" in out
        assert "http://127.0.0.1:8090/viz" in out
