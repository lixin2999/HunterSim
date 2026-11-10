"""场景 fixture 回归：V1.26 新载荷 / V1.25 旧载荷 / 未知兜底 / prop 全表。

fixture 与 :mod:`hunter_sim.platform_adapter.scene_mapper` 的实际语义绑定：
任何字段改名或兜底策略变化都会在这里先失败，避免「平台升级后仿真端静默变脸」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from hunter_sim.platform_adapter.scene_mapper import (
    SceneMappingError,
    map_scene_config,
)
from hunter_sim.scene_runner.scene_config import SceneConfig, TriggerType

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "scenes"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _scenes(payload: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """展开单场景/批处理产物为 ``(scene_id, scene_config)``。"""
    items = payload.get("scenes")
    if isinstance(items, list):
        for item in items:
            yield str(item["scene_id"]), item["scene_config"]
        return
    yield str(payload.get("scene_id") or "scene"), payload


def _map_all(name: str) -> list[SceneConfig]:
    return [
        map_scene_config(config, scene_id=scene_id)
        for scene_id, config in _scenes(_load(name))
    ]


class TestSemanticsSample:
    """V1.26 新载荷（裁剪样本）。"""

    def test_all_scenes_map(self) -> None:
        configs = _map_all("semantics_sample_v1_26.json")
        assert len(configs) == 6

    def test_actor_types_cover_walker_cyclist_prop_vehicle(self) -> None:
        participants = [
            participant
            for config in _map_all("semantics_sample_v1_26.json")
            for participant in config.traffic_participants
        ]
        assert {p.actor_type for p in participants} == {
            "walker",
            "cyclist",
            "prop",
            "vehicle",
        }

    def test_walker_speed_uses_platform_value(self) -> None:
        participants = [
            participant
            for config in _map_all("semantics_sample_v1_26.json")
            for participant in config.traffic_participants
            if participant.actor_type == "walker"
        ]
        speeds = {p.participant_id: p.speed_ms for p in participants}
        assert speeds["pedestrian-1"] == pytest.approx(2.6)
        assert speeds["child-1"] == pytest.approx(3.2)
        assert all(0.3 <= speed <= 4.0 for speed in speeds.values())

    def test_cyclist_uses_bicycle_blueprint(self) -> None:
        participants = [
            participant
            for config in _map_all("semantics_sample_v1_26.json")
            for participant in config.traffic_participants
            if participant.actor_type == "cyclist"
        ]
        assert participants[0].vehicle_kind == "bicycle"
        assert participants[0].blueprint == "vehicle.bh.crossbike"
        assert participants[0].speed_ms == pytest.approx(4.5)

    def test_no_zero_time_trigger(self) -> None:
        for config in _map_all("semantics_sample_v1_26.json"):
            assert not any(
                event.trigger.trigger_type is TriggerType.TIME
                and event.trigger.value == 0.0
                for event in config.scene_events
            )

    def test_events_are_runtime_evaluable(self) -> None:
        for config in _map_all("semantics_sample_v1_26.json"):
            for event in config.scene_events:
                assert event.is_runtime_evaluable is True
                assert event.trigger.trigger_type is not TriggerType.DISABLED

    def test_approximations_are_reported(self) -> None:
        codes = {
            warning.code
            for config in _map_all("semantics_sample_v1_26.json")
            for warning in config.mapping_warnings
        }
        assert {
            "behavior_approx:borrow_oncoming",
            "behavior_approx:door_open",
            "behavior_approx:boarding",
            "condition_approx:reach_position",
            "prop_asset_degraded:ball",
        } <= codes

    def test_lane_id_and_initial_steer(self) -> None:
        config = _map_all("semantics_sample_v1_26.json")[0]
        spawn = config.ego_vehicle.spawn_point
        assert spawn.lane_id == "1"
        assert spawn.lane_index == 1
        assert config.ego_vehicle.vehicle_blueprint == "hunter_se"
        assert config.ego_vehicle.initial_steer == pytest.approx(0.0)

    def test_mapping_report_counts(self) -> None:
        for config in _map_all("semantics_sample_v1_26.json"):
            report = config.extra_params["mapping_report"]
            assert report["actors"]["mapped"] == len(config.traffic_participants)
            assert report["events"]["total"] == len(config.scene_events)
            assert report["events"]["disabled"] == 0
            expected_prefixes = {
                warning.code.split(":", 1)[0]
                for warning in config.mapping_warnings
            }
            assert set(report["warning_counts"]) == expected_prefixes


class TestLegacySample:
    """V1.25 旧载荷向后兼容。"""

    def test_all_scenes_map(self) -> None:
        assert len(_map_all("legacy_v1_25_sample.json")) == 3

    def test_legacy_other_becomes_walker(self) -> None:
        config = _map_all("legacy_v1_25_sample.json")[0]
        participant = config.traffic_participants[0]
        assert participant.actor_type == "walker"
        assert participant.behavior.value == "pedestrian_cross"
        # 未解析变量名不能静默当 0，按行人默认步速并留告警
        assert participant.speed_ms == pytest.approx(1.5)
        assert any(
            warning.code.startswith("unresolved_param:") for warning in config.mapping_warnings
        )

    def test_parked_alias_normalized(self) -> None:
        config = _map_all("legacy_v1_25_sample.json")[1]
        participant = config.traffic_participants[0]
        assert participant.actor_type == "prop"
        assert participant.object_kind == "parked_vehicle"
        assert participant.blueprint.startswith("vehicle.")
        assert any(
            warning.code.startswith("object_kind_normalized:")
            for warning in config.mapping_warnings
        )

    def test_missing_condition_disabled_not_time(self) -> None:
        for config in _map_all("legacy_v1_25_sample.json"):
            for event in config.scene_events:
                assert event.trigger.trigger_type is TriggerType.DISABLED
                assert event.is_runtime_evaluable is False
            assert any(
                warning.code == "condition_missing" for warning in config.mapping_warnings
            )

    def test_collision_event_keeps_event_type(self) -> None:
        config = _map_all("legacy_v1_25_sample.json")[1]
        event = config.scene_events[0]
        assert event.event_type.value == "collision"
        assert event.is_failure_condition is True

    def test_lane_id_placeholder_preserved(self) -> None:
        spawn = _map_all("legacy_v1_25_sample.json")[0].ego_vehicle.spawn_point
        assert spawn.lane_id == "lane-1"
        assert spawn.lane_index is None


class TestFailLoud:
    def test_unknown_behavior_raises(self) -> None:
        payload = _load("unknown_behavior.json")
        with pytest.raises(SceneMappingError) as excinfo:
            map_scene_config(payload, scene_id="unknown-behavior")
        assert excinfo.value.code == "unsupported_behavior:warp_drive"


class TestUnknownCondition:
    def test_unknown_condition_is_disabled(self) -> None:
        config = map_scene_config(_load("unknown_condition.json"), scene_id="unknown-condition")
        event = config.scene_events[0]
        assert event.trigger.trigger_type is TriggerType.DISABLED
        assert event.is_runtime_evaluable is False
        assert any(
            warning.code == "condition_unknown:quantum_entangle"
            for warning in config.mapping_warnings
        )


class TestMissingCondition:
    def test_missing_condition_is_disabled(self) -> None:
        config = map_scene_config(_load("missing_condition.json"), scene_id="missing-condition")
        assert config.scene_events[0].trigger.trigger_type is TriggerType.DISABLED
        assert any(
            warning.code == "condition_missing" for warning in config.mapping_warnings
        )

    def test_actor_behavior_trigger_disabled(self) -> None:
        config = map_scene_config(_load("missing_condition.json"), scene_id="missing-condition")
        trigger = config.traffic_participants[0].behavior_trigger
        assert trigger is not None
        assert trigger.trigger_type is TriggerType.DISABLED

    def test_collision_event_still_evaluable(self) -> None:
        config = map_scene_config(_load("missing_condition.json"), scene_id="missing-condition")
        collision = next(e for e in config.scene_events if e.event_id == "ev-collision")
        assert collision.trigger.trigger_type is TriggerType.EVENT
        assert collision.is_runtime_evaluable is True


class TestPropKinds:
    def test_prop_kinds_map_without_spawn_failure(self) -> None:
        config = map_scene_config(_load("prop_kinds.json"), scene_id="prop-kinds")
        by_id = {p.participant_id: p for p in config.traffic_participants}
        assert len(by_id) == 20
        assert by_id["animal-dog"].actor_type == "walker"
        assert all(
            participant.actor_type == "prop"
            for key, participant in by_id.items()
            if key != "animal-dog"
        )

    def test_blueprint_prefix_matches_kind(self) -> None:
        config = map_scene_config(_load("prop_kinds.json"), scene_id="prop-kinds")
        by_id = {p.participant_id: p for p in config.traffic_participants}
        for key in (
            "prop-cone",
            "prop-construction",
            "prop-road-barrier",
            "prop-container",
            "prop-tyre",
            "prop-fallen-load",
            "prop-debris",
            "prop-sign",
            "prop-bin",
            "prop-ball",
            "prop-bus-stop-shelter",
            "prop-building-corner",
            "prop-gravel",
            "prop-liquid",
        ):
            assert by_id[key].blueprint.startswith("static.prop."), key
        for key in (
            "prop-parked-vehicle",
            "prop-accident-vehicle",
            "prop-broken-vehicle",
            "prop-legacy-parked",
        ):
            assert by_id[key].blueprint.startswith("vehicle."), key

    def test_degraded_and_unknown_assets_are_reported(self) -> None:
        config = map_scene_config(_load("prop_kinds.json"), scene_id="prop-kinds")
        codes = {warning.code for warning in config.mapping_warnings}
        for kind in ("ball", "bus_stop_shelter", "building_corner", "gravel", "liquid"):
            assert f"prop_asset_degraded:{kind}" in codes
        assert "object_kind_unknown:tunnel" in codes
        assert "animal_asset_missing" in codes
        assert any(code.startswith("object_kind_normalized:") for code in codes)

    def test_mapping_report_by_actor_type(self) -> None:
        config = map_scene_config(_load("prop_kinds.json"), scene_id="prop-kinds")
        report = config.extra_params["mapping_report"]
        assert report["actors"]["declared"] == 20
        assert report["actors"]["by_actor_type"] == {"prop": 19, "walker": 1}
