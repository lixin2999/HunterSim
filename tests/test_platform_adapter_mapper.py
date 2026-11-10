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
from hunter_sim.scene_runner.scene_config import MappingWarning


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


class TestActorContainers:
    """参与者容器兼容（现场「鬼探头看不到行人」的定位/兼容路径）。"""

    @staticmethod
    def _pedestrian() -> dict[str, Any]:
        return {
            "actor_id": "ped-1",
            "type": "pedestrian",
            "spawn_point": {"x": 12.0, "y": 3.0},
            "behavior": {
                "behavior_type": "pedestrian_crossing",
                "parameters": {"speed": 1.2},
            },
        }

    def test_alias_key_is_read_when_actors_missing(self) -> None:
        payload = scene_config()
        payload.pop("actors")
        payload["pedestrians"] = [self._pedestrian()]
        config = map_scene_config(payload, scene_id="scene-1")
        assert len(config.traffic_participants) == 1
        participant = config.traffic_participants[0]
        assert participant.participant_id == "ped-1"
        assert participant.actor_type == "walker"
        assert participant.behavior.value == "pedestrian_cross"

    def test_alias_keys_are_merged(self) -> None:
        payload = scene_config()
        payload["traffic_participants"] = [self._pedestrian()]
        config = map_scene_config(payload, scene_id="scene-1")
        assert len(config.traffic_participants) == 2
        assert [p.actor_type for p in config.traffic_participants] == ["vehicle", "walker"]

    def test_duplicate_entries_across_keys_are_deduped(self) -> None:
        payload = scene_config()
        payload["traffic_participants"] = payload["actors"]
        config = map_scene_config(payload, scene_id="scene-1")
        assert len(config.traffic_participants) == 1

    def test_nested_container_is_unwrapped(self) -> None:
        payload = scene_config(actors={"items": [self._pedestrian()]})
        config = map_scene_config(payload, scene_id="scene-1")
        assert len(config.traffic_participants) == 1
        assert config.traffic_participants[0].actor_type == "walker"

    def test_pedestrian_behavior_without_type_maps_to_walker(self) -> None:
        payload = scene_config()
        payload["actors"] = [
            {
                "actor_id": "ghost-1",
                "spawn_point": {"x": 12.0, "y": 3.0},
                "behavior": {"behavior_type": "pedestrian_crossing"},
            }
        ]
        config = map_scene_config(payload, scene_id="scene-1")
        participant = config.traffic_participants[0]
        assert participant.actor_type == "walker"
        assert participant.blueprint.startswith("walker.")


class TestSemanticsV126:
    """HunterCore V1.26.0 场景语义对齐（B/D/C/E/F/G/H）。"""

    @staticmethod
    def _actor(actor_id: str, raw_type: str, behavior: str, **parameters: Any) -> dict[str, Any]:
        return {
            "actor_id": actor_id,
            "type": raw_type,
            "spawn_point": {"x": 10.0, "y": 20.0},
            "behavior": {"behavior_type": behavior, "parameters": parameters},
        }

    def test_other_object_kind_maps_to_prop(self) -> None:
        payload = scene_config(actors=[self._actor("p", "other", "static", object_kind="cone")])
        config = map_scene_config(payload, scene_id="scene-1")
        participant = config.traffic_participants[0]
        assert participant.actor_type == "prop"
        assert participant.object_kind == "cone"
        assert participant.blueprint.startswith("static.prop.")
        assert participant.speed_ms == pytest.approx(0.0)

    def test_other_vehicle_kind_bicycle_maps_to_cyclist(self) -> None:
        payload = scene_config(
            actors=[self._actor("c", "other", "lane_follow", vehicle_kind="bicycle", target_speed=4.0)]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        participant = config.traffic_participants[0]
        assert participant.actor_type == "cyclist"
        assert participant.blueprint == "vehicle.bh.crossbike"
        assert participant.vehicle_kind == "bicycle"

    def test_other_without_kind_legacy_walker(self) -> None:
        payload = scene_config(actors=[self._actor("o", "other", "static")])
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.traffic_participants[0].actor_type == "walker"
        assert any(w.code == "actor_type_legacy_other" for w in config.mapping_warnings)

    def test_parked_alias_normalizes_to_vehicle_prop(self) -> None:
        payload = scene_config(actors=[self._actor("v", "other", "static", object_kind="parked")])
        config = map_scene_config(payload, scene_id="scene-1")
        participant = config.traffic_participants[0]
        assert participant.actor_type == "prop"
        assert participant.object_kind == "parked_vehicle"
        assert participant.blueprint.startswith("vehicle.")
        assert any("object_kind_normalized" in w.code for w in config.mapping_warnings)

    def test_animal_kind_degrades_to_walker(self) -> None:
        payload = scene_config(actors=[self._actor("d", "other", "cross_road", animal_kind="dog")])
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.traffic_participants[0].actor_type == "walker"
        assert any(w.code == "animal_asset_missing" for w in config.mapping_warnings)

    def test_ball_asset_degraded_warns(self) -> None:
        payload = scene_config(actors=[self._actor("b", "other", "static", object_kind="ball")])
        config = map_scene_config(payload, scene_id="scene-1")
        assert any(w.code == "prop_asset_degraded:ball" for w in config.mapping_warnings)

    def test_unknown_object_kind_warns(self) -> None:
        payload = scene_config(actors=[self._actor("t", "other", "static", object_kind="tunnel")])
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.traffic_participants[0].actor_type == "prop"
        assert any("object_kind_unknown:tunnel" == w.code for w in config.mapping_warnings)

    def test_crossing_speed_alias(self) -> None:
        payload = scene_config(
            actors=[self._actor("w", "pedestrian", "cross_road", crossing_speed=2.6)]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        participant = config.traffic_participants[0]
        assert participant.behavior.value == "pedestrian_cross"
        assert participant.speed_ms == pytest.approx(2.6)

    def test_walker_speed_out_of_range_is_clamped_and_reported(self) -> None:
        payload = scene_config(
            actors=[self._actor("w", "pedestrian", "occluded_dash", crossing_speed=8.0)]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.traffic_participants[0].speed_ms == pytest.approx(4.0)
        assert any(w.code.startswith("speed_out_of_range:") for w in config.mapping_warnings)

    def test_unresolved_param_warns_and_uses_default(self) -> None:
        payload = scene_config(
            actors=[self._actor("w", "pedestrian", "occluded_dash", crossing_speed="ped_speed")]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.traffic_participants[0].speed_ms == pytest.approx(1.5)
        assert any(w.code.startswith("unresolved_param:") for w in config.mapping_warnings)

    def test_approximate_behaviors_are_reported(self) -> None:
        payload = scene_config(
            actors=[
                self._actor("b1", "vehicle", "borrow_oncoming", target_speed=8.0),
                self._actor("b2", "vehicle", "door_open", vehicle_kind="car"),
                self._actor("b3", "pedestrian", "boarding", crossing_speed=1.2),
            ]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        codes = {w.code for w in config.mapping_warnings}
        assert {"behavior_approx:borrow_oncoming", "behavior_approx:door_open", "behavior_approx:boarding"} <= codes
        assert config.traffic_participants[0].behavior.value == "constant_speed"
        assert config.traffic_participants[1].behavior.value == "static"
        assert config.traffic_participants[2].behavior.value == "pedestrian_cross"

    def test_unknown_condition_disabled_not_time(self) -> None:
        payload = scene_config(
            events=[
                {
                    "event_id": "e1",
                    "type": "custom",
                    "trigger": {"condition_type": "quantum_entangle", "parameters": {"value": 1.0}},
                }
            ]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        event = config.scene_events[0]
        assert event.trigger.trigger_type.value == "disabled"
        assert event.is_runtime_evaluable is False
        assert any(w.code == "condition_unknown:quantum_entangle" for w in config.mapping_warnings)

    def test_missing_condition_disabled(self) -> None:
        payload = scene_config(
            events=[{"event_id": "e1", "type": "custom", "trigger": {"parameters": {"source": "batch"}}}]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.scene_events[0].trigger.trigger_type.value == "disabled"
        assert any(w.code == "condition_missing" for w in config.mapping_warnings)

    def test_missing_condition_value_disabled(self) -> None:
        payload = scene_config(
            events=[
                {
                    "event_id": "e1",
                    "type": "collision",
                    "trigger": {"condition_type": "relative_distance", "parameters": {"rule": "<"}},
                }
            ]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.scene_events[0].trigger.trigger_type.value == "disabled"
        assert any(
            w.code == "condition_value_unresolved:relative_distance"
            for w in config.mapping_warnings
        )

    def test_collision_event_keeps_event_trigger_without_value(self) -> None:
        payload = scene_config(
            events=[
                {"event_id": "c1", "type": "collision", "trigger": {"condition_type": "collision"}}
            ]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        event = config.scene_events[0]
        assert event.trigger.trigger_type.value == "event"
        assert event.is_runtime_evaluable is True

    def test_trigger_comparator_and_relation_preserved(self) -> None:
        payload = scene_config(
            events=[
                {
                    "event_id": "e1",
                    "type": "cut_in",
                    "trigger": {
                        "condition_type": "relative_distance",
                        "parameters": {
                            "value": 10.0,
                            "rule": "<",
                            "relative_distance_type": "longitudinal",
                        },
                    },
                    "action": {
                        "action_type": "emergency_stop",
                        "parameters": {"deceleration": 6.5},
                    },
                }
            ]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        event = config.scene_events[0]
        assert event.trigger.comparator == "<"
        assert event.trigger.relation == "longitudinal"
        assert event.action_type == "emergency_stop"
        assert event.action_parameters == {"deceleration": 6.5}

    def test_reach_position_approximated_as_traveled_distance(self) -> None:
        payload = scene_config(
            events=[
                {
                    "event_id": "e1",
                    "type": "custom",
                    "trigger": {"condition_type": "reach_position", "parameters": {"value": 30.0}},
                }
            ]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        trigger = config.scene_events[0].trigger
        assert trigger.trigger_type.value == "traveled_distance"
        assert trigger.is_approximate is True
        assert any(w.code == "condition_approx:reach_position" for w in config.mapping_warnings)

    def test_offroad_condition_mapped_and_approximate(self) -> None:
        payload = scene_config(
            events=[
                {
                    "event_id": "e1",
                    "type": "custom",
                    "trigger": {"condition_type": "offroad", "parameters": {"value": 0.5}},
                }
            ]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        trigger = config.scene_events[0].trigger
        assert trigger.trigger_type.value == "offroad"
        assert trigger.is_approximate is True

    def test_standstill_defaults_to_lt_threshold(self) -> None:
        payload = scene_config(
            events=[
                {"event_id": "e1", "type": "custom", "trigger": {"condition_type": "standstill"}}
            ]
        )
        config = map_scene_config(payload, scene_id="scene-1")
        trigger = config.scene_events[0].trigger
        assert trigger.trigger_type.value == "velocity"
        assert trigger.comparator == "<"
        assert trigger.value == pytest.approx(0.1)

    def test_ego_model_normalized_and_initial_steer(self) -> None:
        payload = scene_config()
        payload["ego_vehicle"] = {"model": "HUNTER-SE", "initial_speed": 3.0, "initial_steer": 0.2}
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.ego_vehicle.vehicle_blueprint == "hunter_se"
        assert config.ego_vehicle.initial_steer == pytest.approx(0.2)
        assert any(w.code.startswith("ego_blueprint_normalized") for w in config.mapping_warnings)

    def test_lane_id_string_accepted(self) -> None:
        payload = scene_config()
        payload["map"]["spawn_point"]["lane_id"] = "lane-1"
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.ego_vehicle.spawn_point.lane_id == "lane-1"
        assert config.ego_vehicle.spawn_point.lane_index is None

    def test_lane_id_numeric_becomes_index(self) -> None:
        payload = scene_config()
        payload["map"]["spawn_point"]["lane_id"] = "2"
        config = map_scene_config(payload, scene_id="scene-1")
        assert config.ego_vehicle.spawn_point.lane_id == "2"
        assert config.ego_vehicle.spawn_point.lane_index == 2

    def test_mapping_report_counts(self) -> None:
        payload = scene_config(
            actors=[
                self._actor("v", "vehicle", "cut_in", speed=6.0),
                self._actor("p", "other", "static", object_kind="cone"),
                self._actor("w", "pedestrian", "occluded_dash", crossing_speed=1.8),
            ],
            events=[
                {"event_id": "e1", "type": "collision", "trigger": {"condition_type": "collision"}},
                {"event_id": "e2", "type": "custom", "trigger": {}},
            ],
        )
        config = map_scene_config(payload, scene_id="scene-1")
        report = config.extra_params["mapping_report"]
        assert report["actors"]["declared"] == 3
        assert report["actors"]["mapped"] == len(config.traffic_participants) == 3
        assert report["actors"]["by_actor_type"] == {"vehicle": 1, "prop": 1, "walker": 1}
        assert report["events"]["total"] == 2
        assert report["events"]["disabled"] == 1
        assert report["events"]["runtime_evaluable"] == 1
        assert report["warning_counts"]["condition_missing"] == 1
        assert all(isinstance(w, MappingWarning) for w in config.mapping_warnings)


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
