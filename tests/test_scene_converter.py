"""场景配置转换器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from hunter_sim.common.models import QualityLevel, SimMode
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
    ScenarioBehaviorType,
    SceneConfig,
    SpawnPoint,
    TrafficParticipantConfig,
    WeatherConfig,
)
from hunter_sim.scene_runner.scene_converter import SceneConfigConverter


def _spawn(x: float = 1.0, y: float = 2.0, z: float = 0.5, yaw: float = 90.0) -> SpawnPoint:
    return SpawnPoint(x=x, y=y, z=z, yaw_deg=yaw)


def _config() -> SceneConfig:
    return SceneConfig(
        scene_id="s1",
        scene_name="演示",
        map_id="Town03",
        quality=QualityLevel.EPIC,
        mode=SimMode.VIL,
        ego_vehicle=EgoVehicleConfig(spawn_point=_spawn(), autopilot=True),
        traffic_participants=[
            TrafficParticipantConfig(participant_id="v1", spawn_point=_spawn(), behavior=ScenarioBehaviorType.CONSTANT_SPEED),
            TrafficParticipantConfig(
                participant_id="p1", actor_type="walker", spawn_point=_spawn(),
                behavior=ScenarioBehaviorType.CUT_IN,
            ),
        ],
        weather=WeatherConfig(cloudiness=50.0, precipitation=70.0, fog_density=20.0),
        duration_seconds=45.0,
        timeout_seconds=90.0,
    )


class TestConvert:
    def test_ego_spawn(self) -> None:
        params = SceneConfigConverter().convert(_config())
        assert params.ego_spawn.actor_type == "vehicle"
        assert params.ego_spawn.blueprint_id == "hunter_se"
        assert params.ego_spawn.x == 1.0
        assert params.ego_spawn.yaw_deg == 90.0
        assert params.ego_spawn.autopilot is True
        assert params.map_id == "Town03"
        assert params.quality == "epic"
        assert params.mode == SimMode.VIL
        assert params.duration_seconds == 45.0
        assert params.timeout_seconds == 90.0

    def test_participant_autopilot_only_constant_speed(self) -> None:
        params = SceneConfigConverter().convert(_config())
        assert params.participant_spawns[0].autopilot is True   # constant_speed
        assert params.participant_spawns[1].autopilot is False  # cut_in
        assert params.participant_spawns[1].actor_type == "walker"

    def test_weather_normalized(self) -> None:
        wd = SceneConfigConverter().convert(_config()).weather_dict
        assert wd["cloudiness"] == 0.5
        assert wd["precipitation"] == 0.7
        assert wd["fog_density"] == 0.2
        assert wd["sun_altitude_angle"] == 45.0  # 不除 100


class TestScenarioRunnerDict:
    def test_ego_actor_and_weather_preset(self) -> None:
        d = SceneConfigConverter().to_scenario_runner_dict(_config())
        assert d["name"] == "s1"
        assert d["description"] == "演示"
        ego = d["actors"][0]
        assert ego["type"] == "ego_vehicle"
        assert ego["name"] == "hero"
        assert ego["weather"] == "HardRain"  # precipitation 70 > 60

    def test_clear_weather_when_dry(self) -> None:
        cfg = _config()
        cfg = cfg.model_copy(update={"weather": WeatherConfig(precipitation=10.0)})
        d = SceneConfigConverter().to_scenario_runner_dict(cfg)
        assert d["actors"][0]["weather"] == "ClearNoon"

    def test_participant_types(self) -> None:
        d = SceneConfigConverter().to_scenario_runner_dict(_config())
        types = {a["name"]: a["type"] for a in d["actors"]}
        assert types["v1"] == "other_vehicle"
        assert types["p1"] == "pedestrian"
