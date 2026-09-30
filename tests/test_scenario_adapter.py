"""ScenarioRunner 适配器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Any

import pytest

from hunter_sim.common.exceptions import CarlaSimulationError
from hunter_sim.common.models import SimMode
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
    SceneConfig,
    SpawnPoint,
)
from hunter_sim.scene_runner.scenario_adapter import ScenarioRunnerAdapter


def _config() -> SceneConfig:
    return SceneConfig(
        scene_id="s1",
        scene_name="演示",
        map_id="Town03",
        mode=SimMode.SIL,
        ego_vehicle=EgoVehicleConfig(spawn_point=SpawnPoint(x=1.0, y=2.0)),
    )


class _StubManager:
    def __init__(self) -> None:
        self.loaded: Any = None
        self.ran = False
        self.stopped = False
        self.cleaned = False

    def load_scenario(self, cfg: dict[str, Any]) -> None:
        self.loaded = cfg

    def run_scenario(self) -> None:
        self.ran = True

    def stop_scenario(self) -> None:
        self.stopped = True

    def cleanup(self) -> None:
        self.cleaned = True


class TestPrepare:
    def test_prepare_delegates_to_converter(self) -> None:
        d = ScenarioRunnerAdapter().prepare(_config())
        assert d["name"] == "s1"
        assert d["actors"][0]["name"] == "hero"


class TestStartStop:
    def test_start_without_runner_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = ScenarioRunnerAdapter()
        # 模拟 scenario_runner 不可用（导入后 manager 仍为 None）
        monkeypatch.setattr(adapter, "_try_import_scenario_manager", lambda: None)
        with pytest.raises(CarlaSimulationError):
            adapter.start(_config())

    def test_start_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = ScenarioRunnerAdapter()
        stub = _StubManager()

        def _fake_import() -> None:
            adapter._manager = stub

        monkeypatch.setattr(adapter, "_try_import_scenario_manager", _fake_import)
        adapter.start(_config())
        assert adapter.is_running is True
        assert stub.ran is True
        assert stub.loaded["name"] == "s1"

    def test_start_when_already_running_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = ScenarioRunnerAdapter()
        calls: list[int] = []

        def _fake_import() -> None:
            calls.append(1)

        monkeypatch.setattr(adapter, "_try_import_scenario_manager", _fake_import)
        adapter._running = True
        adapter.start(_config())
        assert calls == []  # 提前返回，不触发导入

    def test_stop_noop_when_not_running(self) -> None:
        ScenarioRunnerAdapter().stop()  # 不应抛出

    def test_stop_cleans_up(self) -> None:
        adapter = ScenarioRunnerAdapter()
        stub = _StubManager()
        adapter._manager = stub
        adapter._running = True
        adapter.stop()
        assert stub.stopped is True
        assert stub.cleaned is True
        assert adapter.is_running is False


class TestAvailability:
    def test_is_available_is_bool(self) -> None:
        assert isinstance(ScenarioRunnerAdapter().is_available, bool)
