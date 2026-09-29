"""模块 5.3 CLI 核心协程 :func:`collect` 的单元测试（不依赖 ``typer``）。"""

from __future__ import annotations

from datetime import UTC, datetime

from hunter_sim.app import cli
from hunter_sim.app.models import RunResult
from hunter_sim.core.config import ConnectionConfig, ScenarioConfig
from hunter_sim.core.contracts import RunMetadata


def make_config() -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "scenario": {
                "name": "cli-demo",
                "map": "Town01",
                "duration_seconds": 1.0,
                "tick_rate": 10.0,
            },
            "vehicle": {"blueprint": "vehicle.tesla.model3"},
            "sensors": [
                {
                    "id": "cam0",
                    "type": "camera.rgb",
                    "position": [0.0, 0.0, 1.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "tick_rate": 10.0,
                    "width": 8,
                    "height": 8,
                    "fov": 90.0,
                }
            ],
            "output": {"base_dir": "./data/runs"},
        }
    )


class FakeOrchestrator:
    def __init__(self) -> None:
        self.max_ticks: int | None = -1

    async def run(self, *, max_ticks: int | None = None) -> RunResult:
        self.max_ticks = max_ticks
        metadata = RunMetadata(
            run_id="r",
            scenario_name="cli-demo",
            map_name="Town01",
            start_time=datetime.now(UTC),
        )
        metadata.finalize(status="completed", end_time=datetime.now(UTC))
        return RunResult(
            run_id="r",
            scenario_name="cli-demo",
            ticks=3,
            written_frames=3,
            sensor_counts={"cam0": 3},
            metadata=metadata,
        )


async def test_collect_builds_container_and_runs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    orch = FakeOrchestrator()

    class FakeContainer:
        def resolve(self, interface: object) -> object:
            return orch

    seen: dict[str, object] = {}

    def _fake_build(config: ScenarioConfig, *, run_id: str, connection_config: object) -> object:
        seen["run_id"] = run_id
        seen["connection_config"] = connection_config
        return FakeContainer()

    monkeypatch.setattr(cli, "build_container", _fake_build)

    result = await cli.collect(make_config(), run_id="abc", max_ticks=5)

    assert seen["run_id"] == "abc"
    assert isinstance(seen["connection_config"], (ConnectionConfig, type(None)))
    assert orch.max_ticks == 5
    assert result.ticks == 3


async def test_collect_generates_run_id_when_absent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    class FakeContainer:
        def resolve(self, interface: object) -> object:
            return FakeOrchestrator()

    def _fake_build(config: ScenarioConfig, *, run_id: str, connection_config: object) -> object:
        captured["run_id"] = run_id
        return FakeContainer()

    monkeypatch.setattr(cli, "build_container", _fake_build)

    await cli.collect(make_config())

    assert isinstance(captured["run_id"], str)
    assert len(str(captured["run_id"])) == 32  # uuid4().hex
