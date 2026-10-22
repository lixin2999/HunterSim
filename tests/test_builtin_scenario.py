"""内置场景执行器测试（V2.3.0）。

用 CARLA Mock 验证「场景配置 → 自车/交通参与者/事件传感器」的最小闭环，
不依赖真实 CARLA server 与 Python SDK。
"""

from __future__ import annotations

import sys
import time
import types
from typing import Any

import pytest

from hunter_sim.common.exceptions import CarlaSimulationError
from hunter_sim.scene_runner.builtin_scenario import BuiltinScenario
from hunter_sim.scene_runner.event_detector import EventDetector
from hunter_sim.scene_runner.scene_config import SceneConfig
from hunter_sim.scene_runner.scene_state_manager import SceneStateManager
from tests.mocks.carla_mocks import (
    MockActor,
    MockBlueprint,
    MockLocation,
    MockTransform,
    MockVector3D,
    MockWorld,
)

_BASE_CONFIG: dict[str, Any] = {
    "scene_id": "scene-builtin-1",
    "map_id": "Town03",
    "duration_seconds": 30.0,
    "ego_vehicle": {
        "vehicle_blueprint": "hunter_se",
        "spawn_point": {"x": 1.0, "y": 2.0},
        "autopilot": True,
    },
    "traffic_participants": [
        {
            "participant_id": "car-1",
            "actor_type": "vehicle",
            "blueprint": "vehicle.tesla.model3",
            "spawn_point": {"x": 10.0, "y": 0.0},
            "behavior": "constant_speed",
        },
        {
            "participant_id": "walker-1",
            "actor_type": "walker",
            "blueprint": "walker.pedestrian.0001",
            "spawn_point": {"x": 20.0, "y": 0.0},
            "behavior": "static",
        },
    ],
}


def _make_scenario(overrides: dict[str, Any] | None = None) -> BuiltinScenario:
    """构造内置场景（注入 Transform 工厂，脱离 carla SDK）。"""
    config = SceneConfig(**{**_BASE_CONFIG, **(overrides or {})})
    return BuiltinScenario(
        config,
        SceneStateManager(config.scene_id),
        EventDetector(config.scene_events),
        transform_factory=lambda _world, sp: MockTransform(MockLocation(sp.x, sp.y, sp.z or 0.5)),
    )


class TestSetup:
    def test_spawns_ego_and_participants(self) -> None:
        scenario = _make_scenario()
        world = MockWorld()

        scenario.setup(world)

        assert scenario.ego_actor is not None
        assert scenario.ego_actor.type_id == "vehicle.hunter_se"
        # hunter_se 候选名补全为 vehicle.hunter_se，且自车启用 autopilot
        assert scenario.ego_actor._autopilot is True
        assert len(world.get_actors()) == 3
        assert len(scenario.spawned_actor_ids) == 3

        participants = [a for a in world.get_actors() if a.id != scenario.ego_actor.id]
        vehicle = next(a for a in participants if a.type_id.startswith("vehicle."))
        walker = next(a for a in participants if a.type_id.startswith("walker."))
        assert vehicle._autopilot is True  # 非 static 车辆参与者交由 TM 驾驶
        assert walker.type_id.startswith("walker.")

    def test_unknown_participant_blueprint_is_skipped(self) -> None:
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "ghost",
                        "blueprint": "vehicle.not_installed",
                        "spawn_point": {"x": 5.0, "y": 5.0},
                    }
                ]
            }
        )
        world = MockWorld()

        scenario.setup(world)

        assert scenario.ego_actor is not None
        assert len(world.get_actors()) == 1  # 仅自车，参与者失败不阻断场景

    def test_setup_twice_recycles_previous_actors(self) -> None:
        scenario = _make_scenario()
        world = MockWorld()
        scenario.setup(world)
        first_ego = scenario.ego_actor

        scenario.setup(world)

        assert first_ego is not None
        assert first_ego.is_alive() is False
        assert scenario.ego_actor is not first_ego
        # 旧场景 3 个 Actor 全部销毁，仅保留新场景 3 个
        assert len([a for a in world.get_actors() if a.is_alive()]) == 3

    def test_world_without_blueprint_library_raises(self) -> None:
        scenario = _make_scenario()
        with pytest.raises(CarlaSimulationError):
            scenario.setup(object())


class TestEgoFallback:
    """标准 CARLA 不带 vehicle.hunter_se 资产时的自车回退。"""

    class _StrictLibrary:
        """模拟真实 CARLA：find 不存在的 ID 抛 IndexError。"""

        def __init__(self) -> None:
            self._blueprints = [
                MockBlueprint("vehicle.tesla.model3"),
                MockBlueprint("walker.pedestrian.0001"),
            ]

        def find(self, type_id: str) -> MockBlueprint:
            for bp in self._blueprints:
                if bp.id == type_id:
                    return bp
            raise IndexError(f"Blueprint '{type_id}' not found")

        def filter(self, pattern: str) -> list[MockBlueprint]:
            prefix = pattern[:-1] if pattern.endswith("*") else pattern
            return [bp for bp in self._blueprints if bp.id.startswith(prefix)]

    def test_ego_falls_back_when_hunter_se_missing(self) -> None:
        scenario = _make_scenario()
        world = MockWorld()
        world._bp_lib = self._StrictLibrary()

        scenario.setup(world)

        assert scenario.ego_actor is not None
        assert scenario.ego_actor.type_id == "vehicle.tesla.model3"


class TestEventSensors:
    def test_attaches_collision_and_lane_invasion_sensors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_carla = types.SimpleNamespace(
            Transform=lambda *args, **kwargs: MockTransform(),
            Vector3D=lambda *args, **kwargs: MockVector3D(*(args or (0.0, 0.0, 0.0))),
        )
        monkeypatch.setitem(sys.modules, "carla", fake_carla)
        monkeypatch.setattr(
            MockActor,
            "listen",
            lambda self, callback: setattr(self, "_listener", callback),
            raising=False,
        )

        scenario = _make_scenario(
            {
                "scene_events": [
                    {
                        "event_id": "e-collision",
                        "event_type": "collision",
                        "trigger": {"trigger_type": "event", "event_name": "collision"},
                    },
                    {
                        "event_id": "e-lane",
                        "event_type": "lane_invasion",
                        "trigger": {"trigger_type": "event", "event_name": "lane_invasion"},
                    },
                ]
            }
        )
        world = MockWorld()

        scenario.setup(world)

        sensor_types = {a.type_id for a in world.get_actors() if a.type_id.startswith("sensor.")}
        assert sensor_types == {"sensor.other.collision", "sensor.other.lane_invasion"}
        assert all(hasattr(a, "_listener") for a in world.get_actors() if a.type_id.startswith("sensor."))


class TestLifecycle:
    def test_cleanup_is_idempotent(self) -> None:
        scenario = _make_scenario()
        world = MockWorld()
        scenario.setup(world)
        ego = scenario.ego_actor

        scenario.cleanup(world)
        assert scenario.ego_actor is None
        assert ego is not None and ego.is_alive() is False

        scenario.cleanup(world)  # 第二次调用不应抛异常

    def test_check_finish_after_duration(self) -> None:
        scenario = _make_scenario()
        world = MockWorld()
        scenario.setup(world)

        assert scenario.check_finish(world, 0.0) is False
        scenario._start_time = time.perf_counter() - 31.0
        scenario._update_elapsed()
        assert scenario.check_finish(world, 31.0) is True
