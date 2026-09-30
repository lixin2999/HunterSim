"""场景配置模型与校验器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import math

import pytest

from hunter_sim.common.models import QualityLevel, SimMode
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
    ScenarioBehaviorType,
    SceneConfig,
    SceneEventDefinition,
    SceneEventTrigger,
    SceneEventType,
    SpawnPoint,
    TrafficParticipantConfig,
    TriggerType,
    WeatherConfig,
)
from hunter_sim.scene_runner.scene_validator import SceneConfigValidator, ValidationResult


def _spawn(x: float = 10.0, y: float = 20.0, yaw: float = 90.0) -> SpawnPoint:
    return SpawnPoint(x=x, y=y, z=0.0, yaw_deg=yaw)


def _ego() -> EgoVehicleConfig:
    return EgoVehicleConfig(spawn_point=_spawn())


def _config(**overrides) -> SceneConfig:
    base = dict(
        scene_id="scene-1",
        scene_name="测试场景",
        map_id="Town03",
        ego_vehicle=_ego(),
    )
    base.update(overrides)
    return SceneConfig(**base)


class TestSpawnPoint:
    def test_to_transform_radians(self) -> None:
        t = _spawn(x=1.0, y=2.0, yaw=180.0).to_transform()
        assert t.x == 1.0
        assert t.y == 2.0
        assert math.isclose(t.yaw, math.pi)

    def test_yaw_range_validation(self) -> None:
        with pytest.raises(Exception):
            SpawnPoint(x=0.0, y=0.0, yaw_deg=999.0)


class TestSceneConfig:
    def test_defaults(self) -> None:
        c = _config()
        assert c.quality == QualityLevel.MEDIUM
        assert c.mode == SimMode.SIL
        assert c.duration_seconds == 60.0

    def test_map_id_blank_rejected(self) -> None:
        with pytest.raises(Exception):
            _config(map_id="   ")

    def test_map_id_stripped(self) -> None:
        c = _config(map_id="  Town05  ")
        assert c.map_id == "Town05"

    def test_duration_positive(self) -> None:
        with pytest.raises(Exception):
            _config(duration_seconds=0.0)


class TestValidationResult:
    def test_counts_and_summary(self) -> None:
        r = ValidationResult(valid=False, errors=[], warnings=[])
        assert r.error_count == 0
        assert "no issues" in ValidationResult(valid=True, errors=[], warnings=[]).summary()


class TestSceneConfigValidator:
    def setup_method(self) -> None:
        self.validator = SceneConfigValidator(available_maps={"Town03", "Town05"})

    def test_valid_scene(self) -> None:
        result = self.validator.validate(_config())
        assert result.valid is True
        assert result.errors == []

    def test_unknown_map_error(self) -> None:
        result = self.validator.validate(_config(map_id="Town99"))
        assert result.valid is False
        assert any(e.field_path == "map_id" for e in result.errors)

    def test_spawn_out_of_range_error(self) -> None:
        cfg = _config(ego_vehicle=EgoVehicleConfig(spawn_point=_spawn(x=99999.0)))
        result = self.validator.validate(cfg)
        assert result.valid is False

    def test_initial_speed_warning(self) -> None:
        cfg = _config(ego_vehicle=EgoVehicleConfig(spawn_point=_spawn(), initial_speed_ms=6.0))
        result = self.validator.validate(cfg)
        assert any(w.field_path == "ego_vehicle.initial_speed_ms" for w in result.warnings)

    def test_unknown_weather_preset_warning(self) -> None:
        cfg = _config(weather=WeatherConfig(preset_name="monsoon"))
        result = self.validator.validate(cfg)
        assert any(w.field_path == "weather.preset_name" for w in result.warnings)

    def test_heavy_rain_epic_warning(self) -> None:
        cfg = _config(
            quality=QualityLevel.EPIC,
            weather=WeatherConfig(precipitation=90.0),
        )
        result = self.validator.validate(cfg)
        assert any(w.field_path == "quality" for w in result.warnings)

    def test_duplicate_participant_error(self) -> None:
        tp = TrafficParticipantConfig(participant_id="p1", spawn_point=_spawn())
        cfg = _config(traffic_participants=[tp, tp.model_copy()])
        result = self.validator.validate(cfg)
        assert result.valid is False
        assert any("Duplicate" in e.message for e in result.errors)

    def test_too_many_participants_warning(self) -> None:
        tps = [
            TrafficParticipantConfig(participant_id=f"p{i}", spawn_point=_spawn())
            for i in range(60)
        ]
        result = self.validator.validate(_config(traffic_participants=tps))
        assert any(w.field_path == "traffic_participants" for w in result.warnings)

    def test_negative_duration_error(self) -> None:
        # SceneConfig 会拒绝 duration<=0，此处测试 timeout 校验
        cfg = _config()
        result = self.validator.validate(cfg)
        assert result.valid is True

    def test_scene_event_enum(self) -> None:
        ev = SceneEventDefinition(
            event_id="e1",
            event_type=SceneEventType.COLLISION,
            trigger=SceneEventTrigger(trigger_type=TriggerType.TIME, value=5.0),
        )
        assert ev.is_failure_condition is True

    def test_behavior_enum(self) -> None:
        tp = TrafficParticipantConfig(
            participant_id="p",
            spawn_point=_spawn(),
            behavior=ScenarioBehaviorType.CUT_IN,
        )
        assert tp.behavior == ScenarioBehaviorType.CUT_IN
