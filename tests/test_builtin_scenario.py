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
from hunter_sim.scene_runner.spawn_resolver import is_placeholder_origin
from tests.mocks.carla_mocks import (
    MockActor,
    MockBlueprint,
    MockLocation,
    MockTrafficManager,
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


def _make_scenario(
    overrides: dict[str, Any] | None = None,
    *,
    traffic_manager: Any = None,
    traffic_manager_port: int = 8000,
) -> BuiltinScenario:
    """构造内置场景（注入 Transform 工厂，脱离 carla SDK）。"""
    config = SceneConfig(**{**_BASE_CONFIG, **(overrides or {})})
    return BuiltinScenario(
        config,
        SceneStateManager(config.scene_id),
        EventDetector(config.scene_events),
        transform_factory=lambda _world, sp: MockTransform(MockLocation(sp.x, sp.y, sp.z or 0.5)),
        traffic_manager=traffic_manager,
        traffic_manager_port=traffic_manager_port,
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

    def test_unknown_participant_blueprint_falls_back_to_generic(self) -> None:
        """未安装的参与者蓝图必须回退到同类通用蓝图，而不是静默丢弃。

        现场回归：平台下发的 walker/vehicle 蓝图在标准 CARLA 里不存在时，旧实现
        只记一条 WARNING 就跳过，可视化里就只剩自车在跑（看不到儿童追球）。
        """
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
        actors = world.get_actors()
        assert len(actors) == 2  # 自车 + 回退蓝图的参与者
        assert any(actor.type_id == "vehicle.tesla.model3" for actor in actors)

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


class TestMotionAndBehaviors:
    """同步模式下自车/参与者的运动与行为执行。"""

    def test_ego_initial_speed_passed_to_traffic_manager(self) -> None:
        tm = MockTrafficManager()
        scenario = _make_scenario(
            {"ego_vehicle": {**_BASE_CONFIG["ego_vehicle"], "initial_speed_ms": 8.0}},
            traffic_manager=tm,
        )
        world = MockWorld()

        scenario.setup(world)

        ego = scenario.ego_actor
        assert ego is not None
        assert tm.desired_speeds[ego.id] == pytest.approx(8.0)

    def test_ego_autopilot_uses_configured_tm_port(self) -> None:
        scenario = _make_scenario(traffic_manager=MockTrafficManager(), traffic_manager_port=9123)
        world = MockWorld()

        scenario.setup(world)

        # 自车 autopilot 使用的端口来自实例 TM 端口（而不是硬编码 8000）
        assert scenario._traffic_manager_port == 9123  # noqa: SLF001

    def test_non_static_vehicle_participant_gets_desired_speed(self) -> None:
        tm = MockTrafficManager()
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "car-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 10.0, "y": 0.0},
                        "behavior": "constant_speed",
                        "speed_ms": 7.0,
                    }
                ]
            },
            traffic_manager=tm,
        )
        world = MockWorld()

        scenario.setup(world)

        vehicle = next(
            a for a in world.get_actors() if a.type_id == "vehicle.tesla.model3"
        )
        assert vehicle._autopilot is True
        assert tm.desired_speeds[vehicle.id] == pytest.approx(7.0)

    def test_decelerate_behavior_reduces_desired_speed_over_ticks(self) -> None:
        tm = MockTrafficManager()
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "car-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 10.0, "y": 0.0},
                        "behavior": "decelerate",
                        "speed_ms": 10.0,
                    }
                ]
            },
            traffic_manager=tm,
        )
        world = MockWorld()
        scenario.setup(world)
        vehicle = next(
            a for a in world.get_actors() if a.type_id == "vehicle.tesla.model3"
        )
        assert tm.desired_speeds[vehicle.id] == pytest.approx(10.0)

        scenario.tick(world, 1.0, 1.0)
        assert tm.desired_speeds[vehicle.id] == pytest.approx(8.0)

        scenario.tick(world, 2.0, 1.0)
        assert tm.desired_speeds[vehicle.id] == pytest.approx(6.0)

    def test_cut_in_forces_lane_change_only_once(self) -> None:
        tm = MockTrafficManager()
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "car-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 10.0, "y": 0.0},
                        "behavior": "cut_in",
                        "speed_ms": 6.0,
                    }
                ]
            },
            traffic_manager=tm,
        )
        world = MockWorld()
        scenario.setup(world)
        vehicle = next(
            a for a in world.get_actors() if a.type_id == "vehicle.tesla.model3"
        )

        scenario.tick(world, 1.0, 0.02)
        scenario.tick(world, 2.0, 0.02)

        # 自车在参与者右侧（ego y=2 > participant y=0）→ direction=True（向右）
        assert tm.lane_changes == [(vehicle.id, True)]

    def test_cut_in_changes_left_when_ego_is_on_left(self) -> None:
        tm = MockTrafficManager()
        scenario = _make_scenario(
            {
                "ego_vehicle": {
                    **_BASE_CONFIG["ego_vehicle"],
                    "spawn_point": {"x": 1.0, "y": -2.0},
                },
                "traffic_participants": [
                    {
                        "participant_id": "car-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 10.0, "y": 0.0},
                        "behavior": "cut_in",
                        "speed_ms": 6.0,
                    }
                ],
            },
            traffic_manager=tm,
        )
        world = MockWorld()
        scenario.setup(world)
        vehicle = next(
            a for a in world.get_actors() if a.type_id == "vehicle.tesla.model3"
        )

        scenario.tick(world, 1.0, 0.02)

        # 自车在左侧（ego y=-2）→ direction=False（向左）
        assert tm.lane_changes == [(vehicle.id, False)]

    def test_cut_out_changes_to_opposite_side_of_ego(self) -> None:
        tm = MockTrafficManager()
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "car-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 10.0, "y": 0.0},
                        "behavior": "cut_out",
                        "speed_ms": 6.0,
                    }
                ]
            },
            traffic_manager=tm,
        )
        world = MockWorld()
        scenario.setup(world)
        vehicle = next(
            a for a in world.get_actors() if a.type_id == "vehicle.tesla.model3"
        )

        scenario.tick(world, 1.0, 0.02)

        # 自车在右侧 → 切出应向左（direction=False）
        assert tm.lane_changes == [(vehicle.id, False)]

    def test_static_vehicle_is_braked(self) -> None:
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "car-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 10.0, "y": 0.0},
                        "behavior": "static",
                    }
                ]
            },
            traffic_manager=MockTrafficManager(),
        )
        world = MockWorld()

        scenario.setup(world)

        vehicle = next(
            a for a in world.get_actors() if a.type_id == "vehicle.tesla.model3"
        )
        assert vehicle._autopilot is False
        assert vehicle._control is not None
        assert vehicle._control.brake == pytest.approx(1.0)

    def test_walker_crossing_uses_ai_controller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_carla = types.SimpleNamespace(
            Transform=lambda *args, **kwargs: MockTransform(),
            Location=lambda x=0.0, y=0.0, z=0.0: MockLocation(x, y, z),
        )
        monkeypatch.setitem(sys.modules, "carla", fake_carla)
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "walker-1",
                        "actor_type": "walker",
                        "blueprint": "walker.pedestrian.0001",
                        "spawn_point": {"x": 20.0, "y": 0.0},
                        "behavior": "pedestrian_cross",
                        "speed_ms": 1.2,
                    }
                ]
            },
            traffic_manager=MockTrafficManager(),
        )
        world = MockWorld()

        scenario.setup(world)

        assert len(scenario._walker_controllers) == 1  # noqa: SLF001
        controller = scenario._walker_ai_controllers[0]  # noqa: SLF001
        assert controller._started is True
        assert controller.destination is not None
        assert controller.max_speed == pytest.approx(1.2)
        # 控制器必须以相对 transform（单位变换）附着，否则会摆到「出生点×2」
        assert controller.attached_transform == MockTransform()

    def test_missing_ai_controller_falls_back_to_scripted_crossing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``controller.ai.walker`` 缺失时行人仍应横穿（脚本化兜底）。"""

        class _Rotation:
            def __init__(self, pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0) -> None:
                self.pitch = pitch
                self.yaw = yaw
                self.roll = roll

        class _Transform:
            def __init__(self, location: Any = None, rotation: Any = None) -> None:
                self.location = location or MockLocation(0.0, 0.0, 0.0)
                self.rotation = rotation or _Rotation()

        fake_carla = types.SimpleNamespace(
            Transform=_Transform,
            Location=lambda x=0.0, y=0.0, z=0.0: MockLocation(x, y, z),
            Rotation=_Rotation,
        )
        monkeypatch.setitem(sys.modules, "carla", fake_carla)
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "walker-1",
                        "actor_type": "walker",
                        "blueprint": "walker.pedestrian.0001",
                        "spawn_point": {"x": 20.0, "y": 0.0},
                        "behavior": "pedestrian_cross",
                        "speed_ms": 1.2,
                    }
                ]
            },
            traffic_manager=MockTrafficManager(),
        )
        world = MockWorld()
        library = world.get_blueprint_library()
        library._blueprints = [  # noqa: SLF001  模拟服务端未安装行人 AI 蓝图
            bp for bp in library._blueprints if bp.id != "controller.ai.walker"  # noqa: SLF001
        ]

        scenario.setup(world)

        assert scenario._walker_ai_controllers == []  # noqa: SLF001
        assert len(scenario._scripted_walkers) == 1  # noqa: SLF001
        walker = next(
            actor for actor in world.get_actors() if actor.type_id.startswith("walker.")
        )
        before = walker.get_transform().location
        for _ in range(25):
            scenario.tick(world, time.time(), 0.2)
        after = walker.get_transform().location

        assert ((after.x - before.x) ** 2 + (after.y - before.y) ** 2) ** 0.5 > 1.0


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


class _FlakyEgoWorld(MockWorld):
    """首次 spawn 返回 None（位置被占用），用于验证候选出生点重试。"""

    def __init__(self) -> None:
        super().__init__()
        self.spawn_attempts = 0

    def try_spawn_actor(self, blueprint: Any, transform: Any, attach_to: Any = None) -> Any:
        self.spawn_attempts += 1
        if self.spawn_attempts == 1:
            return None
        return super().spawn_actor(blueprint, transform, attach_to=attach_to)


class TestSpawnReplacement:
    """原点替换 + 参与者相对偏移（HunterCore V1.25.0 契约）。"""

    def test_origin_ego_replaced_and_participants_follow(self) -> None:
        scenario = _make_scenario(
            {
                "map_type": "carla_town",
                "spawn_semantics": "relative_to_ego",
                "ego_vehicle": {
                    "vehicle_blueprint": "hunter_se",
                    "spawn_point": {"x": 0.0, "y": 0.0},
                    "autopilot": False,
                },
                "traffic_participants": [
                    {
                        "participant_id": "car-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 5.0, "y": -2.0},
                        "behavior": "static",
                    }
                ],
            }
        )
        world = MockWorld()

        scenario.setup(world)

        ego = scenario.ego_actor
        assert ego is not None
        ego_spawn = scenario._ego_spawn
        assert ego_spawn is not None
        # 原点被替换为地图合法出生点
        assert is_placeholder_origin(ego_spawn) is False

        participant = next(a for a in world.get_actors() if a.id != ego.id)
        transform = participant.get_transform()
        assert transform.location.x == pytest.approx(ego_spawn.x + 5.0)
        assert transform.location.y == pytest.approx(ego_spawn.y - 2.0)

    def test_absolute_semantics_keeps_participant_coordinates(self) -> None:
        scenario = _make_scenario(
            {
                "map_type": "carla_town",
                "spawn_semantics": "absolute",
                "ego_vehicle": {
                    "vehicle_blueprint": "hunter_se",
                    "spawn_point": {"x": 0.0, "y": 0.0},
                },
                "traffic_participants": [
                    {
                        "participant_id": "car-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 7.0, "y": 3.0},
                        "behavior": "static",
                    }
                ],
            }
        )
        world = MockWorld()

        scenario.setup(world)

        ego = scenario.ego_actor
        participant = next(a for a in world.get_actors() if a.id != ego.id)
        assert participant.get_transform().location.x == pytest.approx(7.0)
        assert participant.get_transform().location.y == pytest.approx(3.0)

    def test_ego_spawn_retries_next_candidate(self) -> None:
        scenario = _make_scenario(
            {
                "map_type": "carla_town",
                "ego_vehicle": {
                    "vehicle_blueprint": "hunter_se",
                    "spawn_point": {"x": 0.0, "y": 0.0},
                },
                "traffic_participants": [],
            }
        )
        world = _FlakyEgoWorld()

        scenario.setup(world)

        assert scenario.ego_actor is not None
        assert world.spawn_attempts >= 2
        assert scenario._ego_spawn is not None
        assert is_placeholder_origin(scenario._ego_spawn) is False
