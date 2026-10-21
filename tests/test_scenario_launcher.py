"""ScenarioRunnerLauncher 单测（V2.2.0，命令行构建 + 进程生命周期打桩）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter_sim.common.models import CarlaSettings
from hunter_sim.gui.scenario_launcher import ScenarioRunnerLauncher


class FakePopen:
    """记录命令、可控存活状态的 Popen 桩。"""

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = cmd
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.stdout = None

    def poll(self) -> int | None:  # noqa: D102
        return self.returncode

    def terminate(self) -> None:  # noqa: D102
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float = None) -> int:  # noqa: ANN001, D102
        return 0

    def kill(self) -> None:  # noqa: D102
        self.killed = True


def _carla() -> CarlaSettings:
    return CarlaSettings(host="10.0.0.5", rpc_port=2000, tm_port=8100, timeout_seconds=15.0)


def test_build_command_openscenario() -> None:
    """--openscenario + --sync + host/port/tm/timeout 全部落到命令。"""
    launcher = ScenarioRunnerLauncher(_carla(), scenario_runner_path="/opt/scenario_runner.py")
    cmd = launcher.build_command(openscenario=Path("/scenes/a.xosc"), sync=True)
    assert cmd[1] == "/opt/scenario_runner.py"
    assert "--host=10.0.0.5" in cmd
    assert "--port=2000" in cmd
    assert "--trafficManagerPort=8100" in cmd
    assert "--timeout=15.0" in cmd
    assert "--sync" in cmd
    assert "--openscenario=/scenes/a.xosc" in cmd
    assert "--waitForEgo" in cmd


def test_build_command_scenario_name() -> None:
    """内置场景名走 --scenario，异步模式不含 --sync。"""
    launcher = ScenarioRunnerLauncher(_carla())
    cmd = launcher.build_command(scenario="FollowLeadingVehicle", sync=False)
    assert "--scenario=FollowLeadingVehicle" in cmd
    assert "--sync" not in cmd
    assert cmd[1] == "scenario_runner.py"


def test_build_command_requires_target() -> None:
    """既无 openscenario 又无 scenario 抛 ValueError。"""
    launcher = ScenarioRunnerLauncher(_carla())
    with pytest.raises(ValueError):
        launcher.build_command()


def test_start_stop_lifecycle() -> None:
    """start 拉起桩进程、is_running 反映存活、stop 触发 terminate。"""
    captured: dict[str, FakePopen] = {}

    def factory(cmd: list[str]) -> FakePopen:
        proc = FakePopen(cmd)
        captured["proc"] = proc
        return proc

    launcher = ScenarioRunnerLauncher(_carla(), popen_factory=factory)
    assert launcher.is_running is False
    assert launcher.start(scenario="ControlLoss") is True
    assert launcher.is_running is True
    launcher.stop()
    assert captured["proc"].terminated is True
    assert launcher.is_running is False


def test_start_failure_returns_false() -> None:
    """工厂抛异常时 start 返回 False 且不残留进程。"""

    def boom(cmd: list[str]) -> FakePopen:
        raise OSError("no python")

    launcher = ScenarioRunnerLauncher(_carla(), popen_factory=boom)
    assert launcher.start(scenario="X") is False
    assert launcher.is_running is False
