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
from hunter_sim.scene_runner.scene_config import SceneConfig, SpawnPoint
from hunter_sim.scene_runner.scene_state_manager import SceneStateManager
from hunter_sim.scene_runner.spawn_resolver import is_placeholder_origin
from tests.mocks.carla_mocks import (
    MockActor,
    MockBlueprint,
    MockLocation,
    MockRotation,
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
        # 默认走脚本化横穿；仅在显式开启时启用原生 AI 导航控制器。
        monkeypatch.setenv("SCENE_WALKER_AI_CONTROLLER", "1")
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

    def test_walker_defaults_to_scripted_to_avoid_offnavmesh_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认不走原生 AI 导航（避免 off-mesh go_to_location 原生栈溢出）。

        回归 V3.6.2：场景行人坐标为平台下发的任意值，不保证在导航网格上；
        对 off-mesh 点调用 controller.ai.walker.go_to_location 会触发 CARLA 原生
        RecastNavigation 深层递归（0xC00000FD）。默认必须走脚本化横穿。
        """
        monkeypatch.delenv("SCENE_WALKER_AI_CONTROLLER", raising=False)
        fake_carla = types.SimpleNamespace(
            Transform=lambda *args, **kwargs: MockTransform(),
            Location=lambda x=0.0, y=0.0, z=0.0: MockLocation(x, y, z),
        )
        monkeypatch.setitem(sys.modules, "carla", fake_carla)
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "animal-1",
                        "actor_type": "walker",
                        "blueprint": "walker.pedestrian.0001",
                        "spawn_point": {"x": 548.6, "y": 37.2},
                        "behavior": "pedestrian_cross",
                        "speed_ms": 1.2,
                    }
                ]
            },
            traffic_manager=MockTrafficManager(),
        )
        world = MockWorld()

        scenario.setup(world)

        # 不生成任何原生 AI 控制器（否则会在真实 CARLA 上 go_to_location 崩溃）
        assert scenario._walker_ai_controllers == []  # noqa: SLF001
        assert scenario._walker_controllers == []  # noqa: SLF001
        # 但行人仍通过脚本化横穿保留行为
        assert len(scenario._scripted_walkers) == 1  # noqa: SLF001

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

    def test_scripted_walker_not_teleported_by_placeholder_origin(self) -> None:
        """首 tick 前占位原点读数不得把脚本化行人瞬移到世界原点。

        回归 V3.6.3：scenario.tick() 先于 world.tick() 执行，同步模式下首 tick 前
        walker.get_transform() 返回 (0,0,0)。若不判据就会把行人从真实出生点
        (548.6,37.2) 推向目标 (548.6,47.7) 时以原点为起点，等价于瞬移到世界原点再
        横穿整张地图（画面里动物消失、视区外数百米）。
        """
        from hunter_sim.scene_runner.builtin_scenario import _ScriptedWalker

        class _Rotation:
            def __init__(self, pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0) -> None:
                self.pitch, self.yaw, self.roll = pitch, yaw, roll

        class _Transform:
            def __init__(self, location: Any, rotation: Any) -> None:
                self.location, self.rotation = location, rotation

        fake_carla = types.SimpleNamespace(
            Transform=lambda loc, rot: _Transform(loc, rot),
            Location=lambda x=0.0, y=0.0, z=0.0: MockLocation(x, y, z),
            Rotation=_Rotation,
        )

        class _WalkerActor:
            def __init__(self, real: tuple[float, float, float]) -> None:
                self.real = MockLocation(*real)
                self.report_placeholder = True
                self.set_calls: list[Any] = []

            def get_transform(self) -> Any:
                loc = (
                    MockLocation(0.0, 0.0, 0.0)
                    if self.report_placeholder
                    else self.real
                )
                return _Transform(loc, _Rotation())

            def set_transform(self, transform: Any) -> None:
                self.set_calls.append(transform)
                self.real = transform.location

        scenario = _make_scenario(traffic_manager=MockTrafficManager())
        walker = _WalkerActor((548.6, 37.2, 0.3))
        scenario._scripted_walkers.append(  # noqa: SLF001
            _ScriptedWalker(
                actor=walker,
                destination=(548.6, 47.7, 0.3),
                speed_ms=1.5,
                carla_module=fake_carla,
                spawn=SpawnPoint(x=548.6, y=37.2, z=0.3),
            )
        )

        # 未同步（读数落在原点）：本 tick 必须跳过推进，绝不 set_transform
        scenario._advance_scripted_walkers(0.02)  # noqa: SLF001
        assert walker.set_calls == []
        assert scenario._scripted_walkers[0].done is False  # noqa: SLF001

        # 位姿同步后：从真实出生点沿 +y 推向目标，x 基本不变（不会往原点跑）
        walker.report_placeholder = False
        scenario._advance_scripted_walkers(0.02)  # noqa: SLF001
        assert len(walker.set_calls) == 1
        new_loc = walker.set_calls[0].location
        assert abs(new_loc.x - 548.6) < 1.0
        assert new_loc.y > 37.2


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

    def test_participant_states_reports_relative_bearing(self) -> None:
        """可视化参与者面板：身份 / 期望行为 / 车体系方位 / 是否在主视区。"""
        scenario = _make_scenario(
            {
                "spawn_semantics": "relative_to_ego",
                "ego_vehicle": {
                    "vehicle_blueprint": "hunter_se",
                    "spawn_point": {"x": 0.0, "y": 0.0},
                    "autopilot": False,
                },
                "traffic_participants": [
                    {
                        "participant_id": "lead-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 18.0, "y": 0.0},
                        "behavior": "constant_speed",
                    }
                ],
            }
        )
        world = MockWorld()

        scenario.setup(world)

        states = scenario.participant_states()
        assert len(states) == 1
        state = states[0]
        assert state["id"] == "lead-1"
        assert state["behavior"] == "constant_speed"
        assert state["spawned"] is True
        # 车体系分量：x 前向/y 左向，偏移 (18, 0) → 正前方 18m
        assert state["ahead_m"] == pytest.approx(18.0, abs=0.5)
        assert state["left_m"] == pytest.approx(0.0, abs=0.5)
        assert state["distance_to_ego_m"] == pytest.approx(18.0, abs=0.5)
        assert state["visible"] is True

    def test_participant_states_flags_actor_behind_ego(self) -> None:
        """车后的参与者必须标记为不在主视区（否则现场只能凭画面猜）。"""
        scenario = _make_scenario(
            {
                "spawn_semantics": "relative_to_ego",
                "ego_vehicle": {
                    "vehicle_blueprint": "hunter_se",
                    "spawn_point": {"x": 0.0, "y": 0.0},
                    "autopilot": False,
                },
                "traffic_participants": [
                    {
                        "participant_id": "rear-1",
                        "actor_type": "vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": -9.0, "y": 0.0},
                        "behavior": "constant_speed",
                    }
                ],
            }
        )
        world = MockWorld()

        scenario.setup(world)

        state = scenario.participant_states()[0]
        assert state["ahead_m"] == pytest.approx(-9.0, abs=0.5)
        assert state["visible"] is False


class TestSemanticsV126Execution:
    """V1.26.0 执行端语义：prop/cyclist/行人速度/initial_steer/boarding。"""

    def test_prop_spawn_no_control(self) -> None:
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "cone-1",
                        "actor_type": "prop",
                        "blueprint": "static.prop.constructioncone",
                        "spawn_point": {"x": 6.0, "y": 2.0},
                        "behavior": "static",
                    }
                ]
            }
        )
        world = MockWorld()

        scenario.setup(world)

        prop = next(
            actor for actor in world.get_actors() if actor.type_id.startswith("static.prop.")
        )
        # static.prop 没有车辆控制接口，绝不能进入车辆控制分支
        assert not hasattr(prop, "get_control")
        assert not hasattr(prop, "set_autopilot")

    def test_parked_vehicle_prop_braked(self) -> None:
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "parked-1",
                        "actor_type": "prop",
                        "object_kind": "parked_vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 12.0, "y": 0.0},
                        "behavior": "static",
                    }
                ]
            }
        )
        world = MockWorld()

        scenario.setup(world)

        prop = next(
            actor
            for actor in world.get_actors()
            if actor.type_id == "vehicle.tesla.model3"
        )
        control = prop.get_control()
        assert control is not None
        assert control.brake == pytest.approx(1.0)
        assert prop._autopilot is False

    def test_cyclist_uses_vehicle_blueprint(self) -> None:
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "cyc-1",
                        "actor_type": "cyclist",
                        "blueprint": "vehicle.bh.crossbike",
                        "spawn_point": {"x": 8.0, "y": 1.0},
                        "behavior": "constant_speed",
                        "speed_ms": 4.0,
                    }
                ]
            },
            traffic_manager=MockTrafficManager(),
        )
        world = MockWorld()

        scenario.setup(world)

        cyclist = next(
            actor for actor in world.get_actors() if actor.type_id == "vehicle.bh.crossbike"
        )
        assert cyclist._autopilot is True

    def test_walker_speed_not_capped_at_1_4(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCENE_WALKER_AI_CONTROLLER", "1")
        fake_carla = types.SimpleNamespace(
            Transform=lambda *args, **kwargs: MockTransform(),
            Location=lambda x=0.0, y=0.0, z=0.0: MockLocation(x, y, z),
        )
        monkeypatch.setitem(sys.modules, "carla", fake_carla)
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "walker-fast",
                        "actor_type": "walker",
                        "blueprint": "walker.pedestrian.0001",
                        "spawn_point": {"x": 20.0, "y": 0.0},
                        "behavior": "pedestrian_cross",
                        "speed_ms": 3.2,
                    }
                ]
            },
            traffic_manager=MockTrafficManager(),
        )
        world = MockWorld()

        scenario.setup(world)

        controller = scenario._walker_ai_controllers[0]  # noqa: SLF001
        assert controller.max_speed == pytest.approx(3.2)

    def test_initial_steer_applied_in_manual_mode(self) -> None:
        scenario = _make_scenario(
            {
                "ego_vehicle": {
                    "vehicle_blueprint": "hunter_se",
                    "spawn_point": {"x": 1.0, "y": 2.0},
                    "autopilot": False,
                    "initial_steer": 0.35,
                },
                "traffic_participants": [],
            }
        )
        world = MockWorld()

        scenario.setup(world)

        ego = scenario.ego_actor
        assert ego is not None
        assert ego.get_control().steer == pytest.approx(0.35)

    def test_boarding_targets_parked_vehicle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCENE_WALKER_AI_CONTROLLER", "1")
        fake_carla = types.SimpleNamespace(
            Transform=lambda *args, **kwargs: MockTransform(),
            Location=lambda x=0.0, y=0.0, z=0.0: MockLocation(x, y, z),
        )
        monkeypatch.setitem(sys.modules, "carla", fake_carla)
        scenario = _make_scenario(
            {
                "traffic_participants": [
                    {
                        "participant_id": "parked-1",
                        "actor_type": "prop",
                        "object_kind": "parked_vehicle",
                        "blueprint": "vehicle.tesla.model3",
                        "spawn_point": {"x": 12.0, "y": 0.0},
                        "behavior": "static",
                    },
                    {
                        "participant_id": "person-1",
                        "actor_type": "walker",
                        "source_behavior_type": "boarding",
                        "blueprint": "walker.pedestrian.0001",
                        "spawn_point": {"x": 10.0, "y": 5.0},
                        "behavior": "pedestrian_cross",
                        "speed_ms": 1.2,
                    },
                ]
            },
            traffic_manager=MockTrafficManager(),
        )
        world = MockWorld()

        scenario.setup(world)

        controller = scenario._walker_ai_controllers[0]  # noqa: SLF001
        destination = controller.destination
        assert destination is not None
        distance = ((destination.x - 12.0) ** 2 + (destination.y - 0.0) ** 2) ** 0.5
        assert distance == pytest.approx(1.0, abs=0.05)

    def test_tick_evaluates_scene_triggers(self) -> None:
        scenario = _make_scenario(
            {
                "traffic_participants": [],
                "scene_events": [
                    {
                        "event_id": "ev-time",
                        "event_type": "custom",
                        "trigger": {"trigger_type": "time", "value": 0.5},
                    }
                ],
            }
        )
        world = MockWorld()
        scenario.setup(world)

        scenario.tick(world, 0.0, 0.4)
        assert scenario._event_detector.detected_events == []  # noqa: SLF001
        scenario.tick(world, 0.6, 0.2)
        events = scenario._event_detector.detected_events  # noqa: SLF001
        assert len(events) == 1
        assert events[0].event_type == "trigger_reached"
        assert events[0].event_id == "ev-time"


# ── V3.5.3：同步模式下的跟车场景出生点基准 ──────────────────────────────────────


class _SyncMockMap:
    """只含一个合法出生点的地图 Mock（使自车替换结果可预期）。"""

    def __init__(self, spawn_points: list[MockTransform]) -> None:
        self._spawn_points = spawn_points

    def get_spawn_points(self) -> list[MockTransform]:
        return list(self._spawn_points)


class _SyncMockWorld(MockWorld):
    """CARLA 同步模式 Mock：首 ``tick()`` 之前 Actor 位姿停在世界原点。

    线上跟车场景的失败条件正是这个：``setup()`` 在「World switched to
    synchronous mode」之后、首个 tick 之前执行，此时新 spawn 的 Actor
    ``get_transform()`` 仍返回 ``(0,0,0)``，旧实现把它当自车实时位姿用作
    参与者偏移基准，旋转叠加因此作用在占位原点上。
    """

    def __init__(self, spawn_points: list[MockTransform] | None = None) -> None:
        super().__init__()
        if spawn_points is not None:
            self._map = _SyncMockMap(spawn_points)  # type: ignore[assignment]
        self._pending: list[tuple[MockActor, MockTransform]] = []

    def spawn_actor(
        self,
        blueprint: MockBlueprint,
        transform: MockTransform,
        attach_to: MockActor | None = None,
    ) -> MockActor:
        actor = super().spawn_actor(blueprint, transform, attach_to)
        actor._transform = MockTransform()  # noqa: SLF001  未同步：世界原点
        self._pending.append((actor, transform))
        return actor

    def tick(self) -> int:
        count = super().tick()
        for actor, transform in self._pending:
            actor._transform = transform  # noqa: SLF001
        self._pending.clear()
        return count


_TOWN_SPAWN = MockTransform(MockLocation(392.47, 212.90, 0.30), MockRotation(yaw=90.0))

_LEAD_FOLLOW_CONFIG: dict[str, Any] = {
    "scene_id": "SCN-0176-FOL-LVV-L1-LVV-1a1e-lead",
    "map_id": "Town01",
    "map_type": "carla_town",
    "spawn_semantics": "relative_to_ego",
    "duration_seconds": 60.0,
    "ego_vehicle": {
        "vehicle_blueprint": "hunter_se",
        # 平台下发的占位原点：由仿真端替换为地图出生点（yaw=90）
        "spawn_point": {"x": 0.0, "y": 0.0, "z": 0.0, "yaw_deg": 0.0},
        "autopilot": True,
        "initial_speed_ms": 8.2,
    },
    "traffic_participants": [
        {
            "participant_id": "lead-1",
            "actor_type": "vehicle",
            "blueprint": "vehicle.tesla.model3",
            # 车体系偏移：自车正前方 18.5m
            "spawn_point": {"x": 18.5, "y": 0.0, "z": 0.0, "yaw_deg": 0.0},
            "behavior": "constant_speed",
            "speed_ms": 8.0,
        }
    ],
    "scene_events": [
        {
            "event_id": "ev-headway",
            "event_type": "custom",
            "trigger": {"trigger_type": "time_headway", "value": 2.0, "comparator": "<"},
        }
    ],
}


def _make_follow_scenario(
    *, participant_x: float = 18.5, traffic_manager: Any = None
) -> BuiltinScenario:
    """构造跟车场景（与线上失败场景同构：占位原点 + relative_to_ego）。"""
    config = SceneConfig(**_LEAD_FOLLOW_CONFIG)
    config.traffic_participants[0].spawn_point.x = participant_x
    return BuiltinScenario(
        config,
        SceneStateManager(config.scene_id),
        EventDetector(config.scene_events),
        transform_factory=lambda _world, sp: MockTransform(
            MockLocation(sp.x, sp.y, sp.z or 0.5), MockRotation(yaw=sp.yaw_deg)
        ),
        traffic_manager=traffic_manager,
    )


class TestSyncModeEgoBase:
    """回归：同步模式首 tick 前位姿未同步时，参与者基准必须是自车出生点。"""

    def test_ego_spawn_kept_when_transform_unsynced(self) -> None:
        """自车实时位姿为占位原点时不得覆盖出生点（旧版取到 (0,0,0)）。"""
        scenario = _make_follow_scenario()
        world = _SyncMockWorld([_TOWN_SPAWN])

        scenario.setup(world)

        ego = scenario.ego_actor
        assert ego is not None
        location = ego.get_transform().location
        assert (location.x, location.y) == (0.0, 0.0)  # 首 tick 前读数仍在原点
        base = scenario._participant_base()  # noqa: SLF001
        assert base.x == pytest.approx(392.47, abs=0.01)
        assert base.y == pytest.approx(212.90, abs=0.01)
        assert base.yaw_deg == pytest.approx(90.0, abs=0.01)

    def test_lead_vehicle_lands_ahead_of_ego_not_at_world_origin(self) -> None:
        """前车必须落在自车前方 18.5m（沿自车航向旋转），而非世界 (18.5, 0)。"""
        scenario = _make_follow_scenario()
        world = _SyncMockWorld([_TOWN_SPAWN])

        scenario.setup(world)

        spawn = scenario._participant_spawns["lead-1"]  # noqa: SLF001
        assert spawn.x == pytest.approx(392.47, abs=0.6)  # yaw=90 → 前向沿 +y
        assert spawn.y == pytest.approx(212.90 + 18.5, abs=0.6)
        assert spawn.yaw_deg == pytest.approx(90.0, abs=0.1)

        # 首 tick 之前（位姿未同步）与之后都必须给出同样的车体系偏移
        unsynced = scenario.participant_states()[0]
        assert unsynced["ahead_m"] == pytest.approx(18.5, abs=0.6)
        assert unsynced["left_m"] == pytest.approx(0.0, abs=0.6)

        world.tick()  # 位姿同步到真实出生点
        synced = scenario.participant_states()[0]
        assert synced["spawned"] is True
        assert synced["ahead_m"] == pytest.approx(18.5, abs=0.6)
        assert synced["left_m"] == pytest.approx(0.0, abs=0.6)
        assert synced["visible"] is True

    def test_tm_controls_reapplied_on_first_tick_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """首 tick 后补发 TM 指令；后续 tick 不重复补发。"""
        tm = MockTrafficManager()
        scenario = _make_follow_scenario(traffic_manager=tm)
        world = _SyncMockWorld([_TOWN_SPAWN])
        scenario.setup(world)
        lead_id = scenario._actor_by_participant["lead-1"].id  # noqa: SLF001
        ego_id = scenario.ego_actor.id
        assert tm.desired_speeds[lead_id] == pytest.approx(8.0)
        tm.desired_speeds.clear()

        with caplog.at_level("INFO", logger="hunter_sim.scene_runner.builtin_scenario"):
            scenario.tick(world, 0.02, 0.02)
            assert "post_tick_controls_reapplied" in caplog.text
            assert tm.desired_speeds[lead_id] == pytest.approx(8.0)
            assert tm.desired_speeds[ego_id] == pytest.approx(8.2)

            tm.desired_speeds.clear()
            caplog.clear()
            scenario.tick(world, 0.04, 0.02)
            assert "post_tick_controls_reapplied" not in caplog.text

    def test_follow_condition_warns_when_lead_is_behind(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """声明了跟车事件但前方无车：必须限频告警，不再只留「0 条事件」。"""
        scenario = _make_follow_scenario(participant_x=-18.5)
        world = _SyncMockWorld([_TOWN_SPAWN])
        scenario.setup(world)
        world.tick()

        with caplog.at_level("WARNING", logger="hunter_sim.scene_runner.builtin_scenario"):
            scenario.tick(world, 0.02, 0.02)
            warnings = [
                r.getMessage()
                for r in caplog.records
                if "follow_condition_unmet" in r.getMessage()
            ]
            assert len(warnings) == 1, warnings
            assert "lead-1" in warnings[0]
            scenario.tick(world, 0.04, 0.02)

    def test_scene_expectations_log_offsets_and_world_coords(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """期望日志同时给 offset 与 world，避免把车体系偏移读成世界坐标。"""
        scenario = _make_follow_scenario()
        world = _SyncMockWorld([_TOWN_SPAWN])

        with caplog.at_level("INFO", logger="hunter_sim.scene_runner.builtin_scenario"):
            scenario.setup(world)

        line = next(
            record.getMessage()
            for record in caplog.records
            if "Scene expectations" in record.getMessage()
        )
        assert "offset=(18.5,0.0)" in line
        assert "speed=8.0m/s" in line
        assert "world=(392.5,231.4,yaw=90)" in line


class TestDrivingLaneSnap:
    """车辆类参与者落在非行车道时投影到车道中心（否则 TM 无法接管）。"""

    @staticmethod
    def _fake_carla(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "carla",
            types.SimpleNamespace(
                Location=lambda x=0.0, y=0.0, z=0.0: MockLocation(x, y, z),
                LaneType=types.SimpleNamespace(Driving="driving"),
            ),
        )

    def test_off_lane_point_snaps_to_lane_center(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._fake_carla(monkeypatch)
        lane = MockTransform(MockLocation(100.0, 200.0, 0.3), MockRotation(yaw=90.0))
        calls: list[bool] = []

        class _Map:
            @staticmethod
            def get_waypoint(_location, project_to_road=True, lane_type=None):  # noqa: ANN001
                calls.append(project_to_road)
                if not project_to_road:
                    return None  # 原点不在行车道上
                return types.SimpleNamespace(transform=lane)

        world = types.SimpleNamespace(get_map=lambda: _Map())
        scenario = _make_scenario()

        snapped = scenario._snap_to_driving_lane(  # noqa: SLF001
            world, SpawnPoint(x=100.0, y=201.6, z=0.0, yaw_deg=0.0), "lead-1"
        )

        assert (snapped.x, snapped.y) == (100.0, 200.0)
        assert snapped.yaw_deg == pytest.approx(90.0)
        assert calls == [False, True]

    def test_point_already_on_driving_lane_is_left_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._fake_carla(monkeypatch)

        class _Map:
            @staticmethod
            def get_waypoint(location, project_to_road=True, lane_type=None):  # noqa: ANN001
                return types.SimpleNamespace(
                    transform=MockTransform(MockLocation(location.x, location.y, 0.3))
                )

        world = types.SimpleNamespace(get_map=lambda: _Map())
        scenario = _make_scenario()
        requested = SpawnPoint(x=30.0, y=5.0, z=0.0, yaw_deg=45.0)

        assert scenario._snap_to_driving_lane(world, requested, "car-1") is requested  # noqa: SLF001
