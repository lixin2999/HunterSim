"""simulation.scenario 单元测试（mock world / vehicle / 真实事件总线）。"""

from __future__ import annotations

import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter_sim.core.config import ScenarioConfig
from hunter_sim.core.event_bus import InMemoryEventBus
from hunter_sim.core.events import (
    ScenarioEndedEvent,
    ScenarioStartedEvent,
    SimulationTickEvent,
)
from hunter_sim.core.exceptions import ConfigurationError, SimulationError
from hunter_sim.simulation import scenario as scenario_module
from hunter_sim.simulation.protocols import ScenarioManager, ScenarioState
from hunter_sim.simulation.scenario import ScenarioManagerImpl


def _config() -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "scenario": {
                "name": "s",
                "map": "Town04",
                "duration_seconds": 10,
                "tick_rate": 20,
                "simulation_mode": {
                    "mode": "synchronous",
                    "fixed_delta_seconds": 0.02,
                    "substepping": True,
                    "max_substep_delta_time": 0.01,
                    "max_substeps": 4,
                },
            },
            "vehicle": {"blueprint": "vehicle.tesla.model3", "spawn_point_index": 0},
            "sensors": [
                {
                    "id": "cam",
                    "type": "camera.rgb",
                    "position": [0.0, 0.0, 1.6],
                    "rotation": [0.0, 0.0, 0.0],
                    "width": 8,
                    "height": 8,
                    "fov": 90,
                }
            ],
            "traffic": {"npc_vehicles": 2, "npc_walkers": 1, "traffic_manager_port": 8000},
        }
    )


def _transform() -> SimpleNamespace:
    return SimpleNamespace(
        location=SimpleNamespace(x=1.0, y=2.0, z=0.5),
        rotation=SimpleNamespace(pitch=0.0, yaw=90.0, roll=0.0),
    )


def _world() -> MagicMock:
    world = MagicMock(name="world")
    world.get_map.return_value.get_spawn_points.return_value = [_transform() for _ in range(5)]
    world.get_blueprint_library.return_value.filter.return_value = [MagicMock(name="bp")]
    world.try_spawn_actor.return_value = MagicMock(name="npc_actor")
    world.get_snapshot.return_value = SimpleNamespace(timestamp=0.05, frame=1)
    world.get_settings.return_value = MagicMock(name="settings")
    return world


def _build(
    sample_vehicle_state: object,
) -> tuple[ScenarioManagerImpl, MagicMock, MagicMock, InMemoryEventBus]:
    world = _world()
    conn = MagicMock(name="connection")
    conn.get_world.return_value = world
    conn.load_world = AsyncMock(return_value=world)

    vehicle = MagicMock(name="vehicle")
    vehicle.spawn = AsyncMock()
    vehicle.destroy = AsyncMock()
    vehicle.get_state.return_value = sample_vehicle_state

    bus = InMemoryEventBus()
    scenario = ScenarioManagerImpl(_config(), conn, vehicle, bus, rng=random.Random(0))
    return scenario, conn, vehicle, bus


def test_satisfies_protocol() -> None:
    scenario, _, _, _ = _build(MagicMock())
    assert isinstance(scenario, ScenarioManager)


async def test_configure_transitions_and_spawns(sample_vehicle_state: object) -> None:
    scenario, conn, vehicle, _ = _build(sample_vehicle_state)
    await scenario.configure()
    assert scenario.state is ScenarioState.INITIALIZING
    conn.load_world.assert_awaited_once_with("Town04")
    vehicle.spawn.assert_awaited_once()


async def test_configure_insufficient_spawn_points_raises() -> None:
    world = _world()
    world.get_map.return_value.get_spawn_points.return_value = []
    conn = MagicMock()
    conn.get_world.return_value = world
    conn.load_world = AsyncMock(return_value=world)
    scenario = ScenarioManagerImpl(_config(), conn, MagicMock(), InMemoryEventBus())
    with pytest.raises(ConfigurationError):
        await scenario.configure()


async def test_full_lifecycle_and_events(sample_vehicle_state: object) -> None:
    scenario, _, vehicle, bus = _build(sample_vehicle_state)
    events: list[object] = []
    bus.subscribe(SimulationTickEvent, events.append)
    bus.subscribe(ScenarioStartedEvent, events.append)
    bus.subscribe(ScenarioEndedEvent, events.append)

    hook_calls = {"start": 0, "tick": 0, "end": 0}
    scenario.register_hook(
        "on_start", lambda: hook_calls.__setitem__("start", hook_calls["start"] + 1)
    )
    scenario.register_hook(
        "on_tick", lambda: hook_calls.__setitem__("tick", hook_calls["tick"] + 1)
    )
    scenario.register_hook("on_end", lambda: hook_calls.__setitem__("end", hook_calls["end"] + 1))

    await scenario.configure()
    await scenario.start()
    assert scenario.state is ScenarioState.RUNNING

    await scenario.step()
    await scenario.step()
    assert scenario.tick_count == 2
    assert hook_calls["tick"] == 2

    await scenario.pause()
    assert scenario.state is ScenarioState.PAUSED
    await scenario.resume()
    assert scenario.state is ScenarioState.RUNNING

    await scenario.stop(status="completed")
    assert scenario.state is ScenarioState.COMPLETED
    vehicle.destroy.assert_awaited_once()
    assert hook_calls["start"] == 1
    assert hook_calls["end"] == 1

    started = [e for e in events if isinstance(e, ScenarioStartedEvent)]
    ticks = [e for e in events if isinstance(e, SimulationTickEvent)]
    ended = [e for e in events if isinstance(e, ScenarioEndedEvent)]
    assert len(started) == 1
    assert len(ticks) == 2
    assert len(ended) == 1
    assert ended[0].total_frames == 2  # type: ignore[attr-defined]


async def test_step_requires_running_state(sample_vehicle_state: object) -> None:
    scenario, _, _, _ = _build(sample_vehicle_state)
    await scenario.configure()
    with pytest.raises(SimulationError):
        await scenario.step()


async def test_start_from_idle_invalid_transition(sample_vehicle_state: object) -> None:
    scenario, _, _, _ = _build(sample_vehicle_state)
    with pytest.raises(SimulationError):
        await scenario.start()


async def test_configure_applies_synchronous_mode_settings(sample_vehicle_state: object) -> None:
    """同步（VIL）模式：固定步长与子步参数应写入并应用 WorldSettings。"""
    scenario, conn, _, _ = _build(sample_vehicle_state)
    world = conn.get_world()
    settings = world.get_settings()
    await scenario.configure()
    assert settings.synchronous_mode is True
    assert settings.fixed_delta_seconds == 0.02
    assert settings.substepping is True
    assert settings.max_substep_delta_time == 0.01
    assert settings.max_substeps == 4
    world.apply_settings.assert_called_with(settings)


async def test_configure_asynchronous_mode_clears_delta(sample_vehicle_state: object) -> None:
    """异步（回放/SIL）模式：synchronous_mode=False 且 fixed_delta_seconds=None。"""
    cfg = _config()
    cfg.scenario.simulation_mode.mode = "asynchronous"  # type: ignore[misc]
    world = _world()
    conn = MagicMock()
    conn.get_world.return_value = world
    conn.load_world = AsyncMock(return_value=world)
    vehicle = MagicMock()
    vehicle.spawn = AsyncMock()
    scenario = ScenarioManagerImpl(cfg, conn, vehicle, InMemoryEventBus())
    settings = world.get_settings()
    await scenario.configure()
    assert settings.synchronous_mode is False
    assert settings.fixed_delta_seconds is None


async def test_register_hook_invalid_name(sample_vehicle_state: object) -> None:
    scenario, _, _, _ = _build(sample_vehicle_state)
    with pytest.raises(ConfigurationError):
        scenario.register_hook("on_bogus", lambda: None)


async def test_reset_returns_to_idle(sample_vehicle_state: object) -> None:
    scenario, _, vehicle, _ = _build(sample_vehicle_state)
    await scenario.configure()
    await scenario.start()
    await scenario.reset()
    assert scenario.state is ScenarioState.IDLE
    assert scenario.tick_count == 0
    vehicle.destroy.assert_awaited()


def _config_with_weather(weather: dict[str, float | str]) -> ScenarioConfig:
    raw = {
        "scenario": {"name": "s", "map": "Town04", "duration_seconds": 10, "tick_rate": 20},
        "vehicle": {"blueprint": "vehicle.tesla.model3", "spawn_point_index": 0},
        "sensors": [
            {
                "id": "cam",
                "type": "camera.rgb",
                "position": [0.0, 0.0, 1.6],
                "rotation": [0.0, 0.0, 0.0],
                "width": 8,
                "height": 8,
                "fov": 90,
            }
        ],
        "weather": weather,
    }
    return ScenarioConfig.model_validate(raw)


def _capture_weather_params(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """拦截 ``carla.WeatherParameters`` 构造，记录传入的车参数字。"""
    captured: dict[str, float] = {}

    def _fake(**kwargs: float) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(scenario_module.carla, "WeatherParameters", _fake)
    return captured


async def test_configure_applies_full_explicit_weather(
    monkeypatch: pytest.MonkeyPatch, sample_vehicle_state: object
) -> None:
    """§3.4.1：无 preset 时，六项显式天气参数均应下发到 world.set_weather。"""
    cfg = _config_with_weather(
        {
            "cloudiness": 30.0,
            "precipitation": 40.0,
            "precipitation_deposits": 50.0,
            "wind_intensity": 60.0,
            "sun_azimuth_angle": 90.0,
            "sun_altitude_angle": 20.0,
        }
    )
    world = _world()
    conn = MagicMock()
    conn.get_world.return_value = world
    conn.load_world = AsyncMock(return_value=world)
    vehicle = MagicMock()
    vehicle.spawn = AsyncMock()
    scenario = ScenarioManagerImpl(cfg, conn, vehicle, InMemoryEventBus())

    captured = _capture_weather_params(monkeypatch)
    await scenario.configure()

    assert captured == {
        "cloudiness": 30.0,
        "precipitation": 40.0,
        "precipitation_deposits": 50.0,
        "wind_intensity": 60.0,
        "sun_azimuth_angle": 90.0,
        "sun_altitude_angle": 20.0,
    }
    world.set_weather.assert_called_once()


async def test_configure_applies_preset_overrides_explicit(
    monkeypatch: pytest.MonkeyPatch, sample_vehicle_state: object
) -> None:
    """§3.4.2：指定 preset 时以预设参数为准（night：云量 100 / 高度角 -15）。"""
    cfg = _config_with_weather({"preset": "night", "cloudiness": 5.0, "sun_altitude_angle": 80.0})
    world = _world()
    conn = MagicMock()
    conn.get_world.return_value = world
    conn.load_world = AsyncMock(return_value=world)
    vehicle = MagicMock()
    vehicle.spawn = AsyncMock()
    scenario = ScenarioManagerImpl(cfg, conn, vehicle, InMemoryEventBus())

    captured = _capture_weather_params(monkeypatch)
    await scenario.configure()

    assert captured["cloudiness"] == 100.0
    assert captured["sun_altitude_angle"] == -15.0


async def test_configure_unknown_preset_raises(sample_vehicle_state: object) -> None:
    cfg = _config_with_weather({"preset": "blizzard"})
    world = _world()
    conn = MagicMock()
    conn.get_world.return_value = world
    conn.load_world = AsyncMock(return_value=world)
    vehicle = MagicMock()
    vehicle.spawn = AsyncMock()
    scenario = ScenarioManagerImpl(cfg, conn, vehicle, InMemoryEventBus())
    with pytest.raises(ConfigurationError):
        await scenario.configure()
