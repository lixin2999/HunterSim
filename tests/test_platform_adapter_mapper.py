"""平台 scene_config → 内部 SceneConfig 映射测试（CARLA 适配器）。"""

from __future__ import annotations

import math
from typing import Any

import pytest

from hunter_sim.common.models import SimMode
from hunter_sim.platform_adapter.scene_mapper import (
    SceneMappingError,
    map_calibration,
    map_scene_config,
    resolve_mode,
)


def scene_config(**overrides: Any) -> dict[str, Any]:
    """构造一份平台已合并的最小场景配置。"""
    payload: dict[str, Any] = {
        "map": {
            "map_id": "Town03",
            "spawn_point": {"x": 1.0, "y": 2.0, "z": 0.0, "yaw": math.pi / 2},
        },
        "duration": 30.0,
        "ego_vehicle": {"model": "hunter_se", "initial_speed": 3.0},
        "weather": {"rain": 20.0, "fog": 5.0},
        "actors": [
            {
                "actor_id": "actor-1",
                "type": "vehicle",
                "spawn_point": {"x": 10.0, "y": 20.0},
                "behavior": {
                    "behavior_type": "cut_in",
                    "parameters": {"speed": 6.0},
                },
            }
        ],
        "events": [
            {
                "event_id": "event-1",
                "type": "collision",
                "trigger": {"condition_type": "time", "parameters": {"value": 5.0}},
            }
        ],
        "success_criteria": {"no_collision": True},
    }
    payload.update(overrides)
    return payload


class TestResolveMode:
    def test_supported_modes(self) -> None:
        assert resolve_mode("sil") is SimMode.SIL
        assert resolve_mode("VIL") is SimMode.VIL

    def test_replay_is_unsupported(self) -> None:
        with pytest.raises(SceneMappingError) as exc:
            resolve_mode("replay")
        assert exc.value.code == "unsupported_mode:replay"

    def test_unknown_mode_is_unsupported(self) -> None:
        with pytest.raises(SceneMappingError) as exc:
            resolve_mode("hardware")
        assert exc.value.code == "unsupported_mode:hardware"


class TestMapSceneConfig:
    def test_maps_core_fields(self) -> None:
        config = map_scene_config(
            scene_config(),
            scene_id="scene-1",
            scene_name="测试场景",
            mode=SimMode.SIL,
            quality="high",
        )
        assert config.scene_id == "scene-1"
        assert config.map_id == "Town03"
        assert config.duration_seconds == pytest.approx(30.0)
        assert config.ego_vehicle.autopilot is True
        assert config.ego_vehicle.initial_speed_ms == pytest.approx(3.0)
        assert config.ego_vehicle.spawn_point.yaw_deg == pytest.approx(90.0)
        assert len(config.traffic_participants) == 1
        assert config.traffic_participants[0].behavior.value == "cut_in"
        assert config.weather.precipitation == pytest.approx(20.0)
        assert config.weather.fog_density == pytest.approx(5.0)

    def test_vil_disables_autopilot(self) -> None:
        config = map_scene_config(
            scene_config(),
            scene_id="scene-vil",
            mode=SimMode.VIL,
        )
        assert config.ego_vehicle.autopilot is False

    def test_param_overrides_do_not_change_map(self) -> None:
        payload = scene_config()
        payload["param_overrides"] = {"map": {"map_id": "Town05"}}
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.map_id == "Town03"

    def test_missing_map_is_rejected(self) -> None:
        with pytest.raises(SceneMappingError) as exc:
            map_scene_config({"duration": 10}, scene_id="scene-1")
        assert exc.value.code == "invalid_scene_config:map"

    def test_unsupported_behavior_is_loud(self) -> None:
        payload = scene_config()
        payload["actors"][0]["behavior"]["behavior_type"] = "teleport"
        with pytest.raises(SceneMappingError) as exc:
            map_scene_config(payload, scene_id="scene-1")
        assert exc.value.code == "unsupported_behavior:teleport"

    def test_duration_limit(self) -> None:
        with pytest.raises(SceneMappingError) as exc:
            map_scene_config(
                scene_config(duration=9999),
                scene_id="scene-1",
                max_duration_seconds=60.0,
            )
        assert exc.value.code == "invalid_scene_config:duration"

    def test_default_duration_when_missing(self) -> None:
        payload = scene_config()
        payload.pop("duration")
        config = map_scene_config(
            payload, scene_id="scene-1", default_duration_seconds=45.0
        )
        assert config.duration_seconds == pytest.approx(45.0)


class TestMapCalibration:
    def test_calibration_formula(self) -> None:
        calibration = map_calibration(
            {
                "calibration": {
                    "map_x": 100.0,
                    "map_y": 50.0,
                    "map_heading": 0.5,
                    "odom_x": 2.0,
                    "odom_y": 1.0,
                    "odom_heading": 0.1,
                }
            }
        )
        assert calibration is not None
        yaw0 = -0.5 - 0.1
        assert calibration.yaw0 == pytest.approx(yaw0)
        assert calibration.x0 == pytest.approx(
            100.0 - (2.0 * math.cos(yaw0) - 1.0 * math.sin(yaw0))
        )
        assert calibration.y0 == pytest.approx(
            50.0 + (2.0 * math.sin(yaw0) + 1.0 * math.cos(yaw0))
        )

    def test_missing_calibration_returns_none(self) -> None:
        assert map_calibration(None) is None
        assert map_calibration({"calibration": {"map_x": 1.0}}) is None
