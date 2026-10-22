"""CARLA 进程托管与自愈测试（V2.8.0 P2-15）。

以注入的进程工厂与就绪探测桩驱动状态机，不启动真实 CarlaUE4：
覆盖端点筛选（仅回环）、启动/就绪超时/退避、崩溃自动重启与窗口封顶、
手动重启、关停终止、事件回调与指标打点、快照字段。
"""

from __future__ import annotations

import time
from typing import Any, Optional

import pytest

from hunter_sim.common.metrics import CARLA_SUPERVISOR_EVENTS_TOTAL, counter_value
from hunter_sim.common.models import CarlaSettings
from hunter_sim.engine.carla_supervisor import (
    EVENT_EXHAUSTED,
    EVENT_EXIT,
    EVENT_GIVEUP,
    EVENT_READY_TIMEOUT,
    EVENT_RESTART,
    EVENT_START,
    EVENT_STOP,
    CarlaProcessSupervisor,
    ManagedTarget,
)


class FakeProcess:
    """最小 Popen 桩：可控存活/退出码，并记录终止调用。"""

    def __init__(self, pid: int = 4000, alive: bool = True, returncode: int = -1) -> None:
        self.pid = pid
        self.returncode: Optional[int] = None if alive else returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> Optional[int]:
        return self.returncode

    def crash(self, code: int = -1) -> None:
        self.returncode = code

    def terminate(self) -> None:
        self.terminated = True
        self.crash(0)

    def kill(self) -> None:
        self.killed = True
        self.crash(-9)

    def wait(self, timeout: Optional[float] = None) -> int:
        return int(self.returncode if self.returncode is not None else 0)


class SupervisorHarness:
    """把 popen 工厂与就绪探测收敛到可断言的桩上。"""

    def __init__(self, supervisor: CarlaProcessSupervisor) -> None:
        self.supervisor = supervisor
        self.processes: list[FakeProcess] = []
        self.commands: list[list[str]] = []
        self.ready = True
        self.fail_launch = False

    def popen(self, cmd: Any) -> FakeProcess:
        self.commands.append(list(cmd))
        if self.fail_launch:
            raise OSError("binary not found")
        process = FakeProcess(pid=4000 + len(self.processes))
        self.processes.append(process)
        return process

    def probe(self, host: str, port: int) -> bool:
        return self.ready


def _settings(**overrides: Any) -> CarlaSettings:
    values: dict[str, Any] = {
        "host": "127.0.0.1",
        "rpc_port": 2000,
        "stream_port": 2001,
        "supervisor_enabled": True,
        "supervisor_binary_path": r"C:\CARLA\CarlaUE4.exe",
        "supervisor_max_restarts": 3,
        "supervisor_restart_backoff_seconds": 0.01,
        "supervisor_ready_timeout_seconds": 0.2,
        "supervisor_graceful_shutdown_seconds": 0.1,
    }
    values.update(overrides)
    return CarlaSettings(**values)


def _supervisor(
    settings: Optional[CarlaSettings] = None,
    endpoints: Optional[list[Any]] = None,
    popen_factory: Optional[Any] = None,
    probe: Optional[Any] = None,
    event_sink: Optional[Any] = None,
) -> CarlaProcessSupervisor:
    return CarlaProcessSupervisor(
        settings or _settings(),
        endpoints
        if endpoints is not None
        else [ManagedTarget("127.0.0.1:2000", "127.0.0.1", 2000, 2001)],
        event_sink=event_sink,
        popen_factory=popen_factory,
        probe=probe,
        poll_interval_seconds=0.2,
    )


@pytest.fixture
def harness() -> SupervisorHarness:
    """默认启用自愈、二进制已配置的单回环端点 supervisor。"""
    stub = SupervisorHarness(_supervisor())
    stub.supervisor = _supervisor(popen_factory=stub.popen, probe=stub.probe)
    return stub


def _runtime(stub: SupervisorHarness, key: str = "127.0.0.1:2000") -> Any:
    return stub.supervisor._runtime[key]


class TestScopeAndEnablement:
    def test_only_loopback_endpoints_are_managed(self) -> None:
        supervisor = _supervisor(
            settings=_settings(supervisor_binary_path="/opt/carla/CarlaUE4.sh"),
            endpoints=[
                ManagedTarget("127.0.0.1:2000", "127.0.0.1", 2000, 2001),
                ManagedTarget("10.0.0.9:2000", "10.0.0.9", 2000, 2001),
                ManagedTarget("localhost:2100", "localhost", 2100, 2101),
            ],
        )
        assert supervisor.managed_endpoints == ["127.0.0.1:2000", "localhost:2100"]

    def test_target_projection_from_endpoint_object_and_dict(self) -> None:
        class _Ep:
            host = "127.0.0.1"
            rpc_port = 2002
            stream_port = 2003

        assert ManagedTarget.from_endpoint(_Ep()).key == "127.0.0.1:2002"
        projected = ManagedTarget.from_endpoint({"host": "10.1.1.1", "rpc_port": 3000})
        assert (projected.host, projected.rpc_port, projected.stream_port) == ("10.1.1.1", 3000, 3001)

    def test_enablement_requires_switch_and_binary(self, harness: SupervisorHarness) -> None:
        assert harness.supervisor.enabled is True

        assert _supervisor(settings=_settings(supervisor_binary_path="")).enabled is False
        assert _supervisor(settings=_settings(supervisor_enabled=False)).enabled is False

    def test_build_command_carries_ports_and_extra_args(self) -> None:
        settings = CarlaSettings(
            supervisor_enabled=True,
            supervisor_binary_path="/opt/carla/CarlaUE4.sh",
            supervisor_extra_args="-RenderOffScreen -quality-level=Medium -carla-rpc-port=9999",
        )
        supervisor = CarlaProcessSupervisor(settings, [])
        cmd = supervisor._build_command(ManagedTarget("k", "127.0.0.1", 2000, 2001))

        assert cmd[0] == "/opt/carla/CarlaUE4.sh"
        assert "-carla-rpc-port=2000" in cmd  # 端口由目标决定，配置追加项不覆盖前两位
        assert "-streaming-port=2001" in cmd
        assert "-RenderOffScreen" in cmd
        assert "-quality-level=Medium" in cmd

    def test_build_command_without_binary_returns_empty(self) -> None:
        supervisor = _supervisor(settings=_settings(supervisor_binary_path=""), endpoints=[])
        assert supervisor._build_command(ManagedTarget("k", "127.0.0.1", 2000, 2001)) == []


class TestStartAndReadiness:
    def test_successful_start_marks_running(self, harness: SupervisorHarness) -> None:
        runtime = _runtime(harness)
        before = counter_value(CARLA_SUPERVISOR_EVENTS_TOTAL, event=EVENT_START)

        assert harness.supervisor._attempt_start(runtime, restart=False) is True

        assert runtime.state == "running"
        assert runtime.pid == 4000
        assert harness.commands and "-carla-rpc-port=2000" in harness.commands[0]
        assert counter_value(CARLA_SUPERVISOR_EVENTS_TOTAL, event=EVENT_START) == before + 1
        assert harness.supervisor.snapshot()["recent_events"][-1]["event"] == EVENT_START

    def test_ready_timeout_terminates_and_backs_off(self, harness: SupervisorHarness) -> None:
        harness.ready = False
        runtime = _runtime(harness)

        assert harness.supervisor._attempt_start(runtime, restart=False) is False

        assert runtime.state == "backoff"
        assert runtime.process is None
        assert harness.processes[0].terminated is True
        assert runtime.next_attempt_at > time.time() - 1
        events = [item["event"] for item in harness.supervisor.snapshot()["recent_events"]]
        assert EVENT_READY_TIMEOUT in events

    def test_launch_failure_backs_off(self, harness: SupervisorHarness) -> None:
        harness.fail_launch = True
        runtime = _runtime(harness)

        assert harness.supervisor._attempt_start(runtime, restart=False) is False

        assert runtime.state == "backoff"
        assert runtime.process is None
        assert harness.processes == []
        assert "launch failed" in harness.supervisor.snapshot()["recent_events"][-1]["detail"]

    def test_unconfigured_start_is_observation_only(self, harness: SupervisorHarness) -> None:
        harness.supervisor._cfg = _settings(supervisor_binary_path="")
        runtime = _runtime(harness)

        assert harness.supervisor._attempt_start(runtime, restart=False) is False
        assert runtime.state == "unconfigured"
        assert harness.commands == []

    def test_ensure_started_launches_idle_endpoints(self, harness: SupervisorHarness) -> None:
        harness.supervisor.ensure_started()
        assert _runtime(harness).state == "running"
        assert len(harness.commands) == 1

        harness.supervisor.ensure_started()  # 已存活则不重复拉起
        assert len(harness.commands) == 1


class TestCrashRecovery:
    def test_crash_triggers_auto_restart(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.ensure_started()
        runtime = _runtime(harness)
        first = harness.processes[0]
        first.crash(-11)

        supervisor._check_once(runtime)

        assert runtime.state == "running"
        assert runtime.total_restarts == 1
        assert len(harness.processes) == 2  # 重新拉起一个新进程
        assert runtime.last_exit_code == -11
        events = [item["event"] for item in supervisor.snapshot()["recent_events"]]
        assert EVENT_EXIT in events and EVENT_RESTART in events

    def test_restart_capped_within_window(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.ensure_started()
        runtime = _runtime(harness)
        now = time.time()
        runtime.restarts.extend([now, now, now])  # 已达 max_restarts=3

        harness.processes[0].crash(-1)
        supervisor._check_once(runtime)

        assert runtime.state == "exhausted"
        assert len(harness.processes) == 1  # 不再盲目重启
        assert supervisor.snapshot()["recent_events"][-1]["event"] == EVENT_EXHAUSTED

    def test_window_pruning_restores_restart_budget(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.ensure_started()
        runtime = _runtime(harness)
        stale = time.time() - 400.0  # 超出 300s 窗口
        runtime.restarts.extend([stale, stale, stale])

        harness.processes[0].crash(-1)
        supervisor._check_once(runtime)

        assert runtime.state == "running"
        assert len(harness.processes) == 2

    def test_disabled_supervisor_reports_disabled_state(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.ensure_started()
        supervisor._cfg = _settings(supervisor_enabled=False)
        runtime = _runtime(harness)

        harness.processes[0].crash(-1)
        supervisor._check_once(runtime)

        assert runtime.state == "disabled"
        assert len(harness.processes) == 1  # 开关关闭时不接管重启

    def test_missing_binary_gives_up(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.ensure_started()
        supervisor._cfg = _settings(supervisor_binary_path="")
        runtime = _runtime(harness)

        harness.processes[0].crash(-1)
        supervisor._check_once(runtime)

        assert runtime.state == "unconfigured"
        assert supervisor.snapshot()["recent_events"][-1]["event"] == EVENT_GIVEUP

    def test_alive_process_is_left_untouched(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.ensure_started()
        runtime = _runtime(harness)

        supervisor._check_once(runtime)

        assert runtime.state == "running"
        assert len(harness.processes) == 1


class TestManualRestartAndShutdown:
    def test_restart_terminates_then_relanches(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.ensure_started()
        first = harness.processes[0]

        assert supervisor.restart("127.0.0.1:2000") is True

        assert first.terminated is True
        assert len(harness.processes) == 2
        assert _runtime(harness).state == "running"

    def test_manual_restart_does_not_consume_auto_budget(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.ensure_started()
        supervisor.restart("127.0.0.1:2000")

        assert _runtime(harness).total_restarts == 0
        assert len(_runtime(harness).restarts) == 0

    def test_restart_unknown_endpoint_returns_false(self, harness: SupervisorHarness) -> None:
        assert harness.supervisor.restart("127.0.0.1:9999") is False

    def test_stop_terminates_managed_processes(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.start()
        supervisor.ensure_started()
        process = harness.processes[0]

        supervisor.stop()

        assert process.terminated is True
        assert supervisor._thread is None
        assert _runtime(harness).process is None
        assert supervisor.snapshot()["recent_events"][-1]["event"] == EVENT_STOP

    def test_start_is_idempotent(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        supervisor.start()
        thread = supervisor._thread
        supervisor.start()

        assert supervisor._thread is thread
        supervisor.stop()

    def test_stop_without_start_is_safe(self, harness: SupervisorHarness) -> None:
        harness.supervisor.stop()
        assert harness.commands == []


class TestEventSink:
    def test_events_reach_audit_sink(self, harness: SupervisorHarness) -> None:
        seen: list[tuple[str, str, str]] = []
        harness.supervisor._event_sink = lambda event, key, detail: seen.append((event, key, detail))

        harness.supervisor.ensure_started()

        assert seen and seen[0][0] == EVENT_START
        assert seen[0][1] == "127.0.0.1:2000"

    def test_failing_sink_does_not_break_self_heal(self, harness: SupervisorHarness) -> None:
        def broken_sink(*_: Any) -> None:
            raise RuntimeError("audit unavailable")

        harness.supervisor._event_sink = broken_sink
        harness.supervisor.ensure_started()

        assert _runtime(harness).state == "running"


class TestSnapshot:
    def test_snapshot_fields(self, harness: SupervisorHarness) -> None:
        harness.supervisor.ensure_started()
        snapshot = harness.supervisor.snapshot()

        assert snapshot["enabled"] is True
        assert snapshot["self_heal_ready"] is True
        assert snapshot["max_restarts"] == 3
        assert snapshot["restart_window_seconds"] == 300.0
        assert snapshot["binary_path"] == r"C:\CARLA\CarlaUE4.exe"
        assert snapshot["endpoints"][0]["endpoint"] == "127.0.0.1:2000"
        assert snapshot["endpoints"][0]["state"] == "running"
        assert snapshot["endpoints"][0]["total_restarts"] == 0

    def test_recent_events_are_bounded(self, harness: SupervisorHarness) -> None:
        supervisor = harness.supervisor
        runtime = _runtime(harness)
        for index in range(60):
            supervisor._emit(runtime, EVENT_EXIT, f"noise-{index}")

        assert len(supervisor.snapshot()["recent_events"]) == 50
        assert supervisor.snapshot()["recent_events"][-1]["detail"] == "noise-59"
