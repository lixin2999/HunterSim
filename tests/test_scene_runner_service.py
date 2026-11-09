"""SceneRunnerService 平台发布链路集成测试（V2.10.0）。

验证 sim_data_producer 注入后：
- _publish_sim_status() 正确构建消息并发布；
- _publish_sim_telemetry() 正确构建消息并发布；
- publish_event_to_platform() 调用 SimDataProducer.publish_event；
- 未注入 producer 时全部安全 no-op，不抛异常。

使用 Mock world/scenario 避免 CARLA 依赖。
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from hunter_sim.common.models import SceneStatus
from hunter_sim.scene_runner.event_detector import DetectedEvent
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
    SceneConfig,
    SpawnPoint,
)
from hunter_sim.scene_runner.scene_runner_service import SceneRunnerService


# ── Mock 基础设施 ───────────────────────────────────────────────────────────────


class _FakeProducer:
    """桩 SimDataProducer：记录调用，返回 True。"""

    def __init__(self) -> None:
        self.status_calls: list[dict[str, Any]] = []
        self.telemetry_calls: list[dict[str, Any]] = []
        self.event_calls: list[dict[str, Any]] = []
        self._seq = 0

    def publish_status(self, msg: dict[str, Any]) -> bool:
        self.status_calls.append(msg)
        return True

    def publish_telemetry(self, msg: dict[str, Any]) -> bool:
        self.telemetry_calls.append(msg)
        return True

    def publish_event(self, **kwargs: Any) -> bool:
        self.event_calls.append(kwargs)
        return True

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq


class _FakeWorldSettings:
    def __init__(self) -> None:
        self.synchronous_mode = True
        self.fixed_delta_seconds = 0.02


class _FakeWorld:
    """Mock CARLA World。"""

    def __init__(self) -> None:
        self.settings = _FakeWorldSettings()
        self.apply_calls = 0

    def tick(self) -> int:
        return 0

    def get_settings(self) -> _FakeWorldSettings:
        return self.settings

    def apply_settings(self, settings: _FakeWorldSettings) -> None:
        self.settings = settings
        self.apply_calls += 1

    def get_actors(self) -> list[Any]:
        return []

    def get_weather(self) -> Any:
        w = MagicMock()
        w.cloudiness = 0.0
        w.precipitation = 0.0
        w.precipitation_deposits = 0.0
        w.wind_intensity = 0.0
        w.sun_azimuth_angle = 0.0
        w.sun_altitude_angle = 45.0
        w.fog_density = 0.0
        w.fog_distance = 0.0
        return w

    def set_weather(self, weather: Any) -> None:
        pass


def _minimal_config() -> SceneConfig:
    return SceneConfig(
        scene_id="scene-test-001",
        ego_vehicle=EgoVehicleConfig(spawn_point=SpawnPoint(x=0.0, y=0.0)),
        duration_seconds=30.0,
        timeout_seconds=60.0,
    )


def _make_runner(
    producer: Optional[Any] = None,
    *,
    world: Optional[_FakeWorld] = None,
    traffic_manager: Optional[Any] = None,
) -> SceneRunnerService:
    """构造带/不带 sim_data_producer 的 SceneRunnerService。"""
    world = world or _FakeWorld()
    return SceneRunnerService(
        world=world,
        sim_data_producer=producer,
        sim_id="sim-runner-test",
        vehicle_id="veh-test",
        scene_id="scene-test-001",
        traffic_manager=traffic_manager,
    )


# ── 测试：无 producer 时安全降级 ────────────────────────────────────────────────


class TestNoProducerSafeNoop:
    def test_publish_sim_status_noop(self) -> None:
        runner = _make_runner(producer=None)
        # 直接调用发布方法不应抛异常
        runner._publish_sim_status()

    def test_publish_sim_telemetry_noop(self) -> None:
        runner = _make_runner(producer=None)
        runner._publish_sim_telemetry(sim_time=1.0)

    def test_publish_event_to_platform_noop(self) -> None:
        runner = _make_runner(producer=None)
        event = DetectedEvent(
            event_id="e1", event_type="collision", time_stamp=1.0,
            description="test", event_level="warning", sim_time=1.0,
        )
        runner.publish_event_to_platform(event)


# ── 测试：_publish_sim_status 正确调用 ─────────────────────────────────────────


class TestPublishSimStatus:
    def test_publishes_when_producer_present(self) -> None:
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        # 需加载场景后 state_mgr 才有 snapshot
        config = _minimal_config()
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(config)

        # V2.12.0：状态跃迁（created→loading→ready）已即时发布，手动发布再增一条
        baseline = len(producer.status_calls)
        assert baseline >= 1
        runner._publish_sim_status()
        assert len(producer.status_calls) == baseline + 1
        msg = producer.status_calls[-1]
        assert msg["type"] == "sim_status"
        assert msg["sim_id"] == "sim-runner-test"
        assert "payload" in msg

    def test_state_transition_publishes_immediately(self) -> None:
        """状态跃迁必须即时上报（不等 1Hz 周期）。"""
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        config = _minimal_config()
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(config)
        statuses = [m["payload"]["status"] for m in producer.status_calls]
        assert "LOADING" in statuses
        assert "READY" in statuses

    def test_status_seqs_are_monotonic(self) -> None:
        """seq 必须来自 SimDataProducer 且单调递增（否则平台去重会丢掉全部状态）。"""
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        config = _minimal_config()
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(config)
        runner._publish_sim_status()
        seqs = [m["seq"] for m in producer.status_calls]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        assert seqs[0] >= 1

    def test_status_carries_vehicle_id_and_events_triggered(self) -> None:
        """方案 §5.4：VIL 模式 sim_status 必须携带 vehicle_id 与 events_triggered。"""
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        config = _minimal_config()
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(config)
        msg = producer.status_calls[-1]
        assert msg["vehicle_id"] == "veh-test"
        assert msg["payload"]["events_triggered"] == 0

    def test_terminal_status_published_once(self) -> None:
        """终态必须补发且只补发一次（stop 与循环退出都可能到达终态）。"""
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        config = _minimal_config()
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(config)

        runner._state_mgr.transition_to(SceneStatus.RUNNING)
        runner._state_mgr.transition_to(SceneStatus.COMPLETED)
        baseline = len(producer.status_calls)
        assert producer.status_calls[-1]["payload"]["status"] == "COMPLETED"
        runner._publish_terminal_status()
        assert len(producer.status_calls) == baseline

    def test_event_published_to_platform(self) -> None:
        """事件回调必须并联发到平台 simple_event（V2.12.0 接线）。"""
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        event = DetectedEvent(
            event_id="e2", event_type="collision", time_stamp=2.0,
            description="bump", event_level="critical", sim_time=2.0,
        )
        runner._handle_event(event)
        assert len(producer.event_calls) == 1
        assert producer.event_calls[0]["event_type"] == "collision"

    def test_status_payload_has_correct_fields(self) -> None:
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        config = _minimal_config()
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(config)

        runner._publish_sim_status()
        payload = producer.status_calls[-1]["payload"]
        # SimStatusPayload 字段应序列化到 dict
        assert "status" in payload
        assert "progress_percent" in payload
        assert "elapsed_time" in payload
        assert "total_time" in payload
        assert "actors_count" in payload
        assert "updated_at" in payload


# ── 测试：_publish_sim_telemetry 正确调用 ──────────────────────────────────────


class TestPublishSimTelemetry:
    def test_publishes_telemetry(self) -> None:
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        config = _minimal_config()
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(config)

        runner._publish_sim_telemetry(sim_time=2.0)
        assert len(producer.telemetry_calls) == 1
        msg = producer.telemetry_calls[0]
        assert msg["type"] == "sim_telemetry"
        assert msg["sim_id"] == "sim-runner-test"
        assert msg["vehicle_id"] == "veh-test"

    def test_seq_monotonic(self) -> None:
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        config = _minimal_config()
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(config)

        runner._publish_sim_telemetry(sim_time=1.0)
        runner._publish_sim_telemetry(sim_time=1.1)
        seqs = [c["seq"] for c in producer.telemetry_calls]
        assert seqs[1] > seqs[0]


# ── 测试：publish_event_to_platform ────────────────────────────────────────────


class TestPublishEventToPlatform:
    def test_calls_producer_publish_event(self) -> None:
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        event = DetectedEvent(
            event_id="e-collision", event_type="collision", time_stamp=3.5,
            description="hit pedestrian", data={"actor_id": 42},
            event_level="critical", sim_time=3.5,
        )
        runner.publish_event_to_platform(event)
        assert len(producer.event_calls) == 1
        call = producer.event_calls[0]
        assert call["event_type"] == "collision"
        assert call["event_level"] == "critical"
        assert call["sim_time"] == 3.5
        assert call["data"] == {"actor_id": 42}

    def test_event_publish_exception_swallowed(self) -> None:
        producer = _FakeProducer()
        producer.publish_event = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[assignment]
        runner = _make_runner(producer=producer)
        event = DetectedEvent(
            event_id="e1", event_type="lane_invasion", time_stamp=1.0,
        )
        # 不应抛出
        runner.publish_event_to_platform(event)


# ── 测试：_run_loop 中节流发布逻辑 ─────────────────────────────────────────────


class TestRunLoopThrottling:
    def test_no_publish_without_producer(self) -> None:
        """未注入 producer 时 _run_loop 中的发布路径被跳过，不影响执行。"""
        runner = _make_runner(producer=None)
        # 确认属性正确设置
        assert runner._sim_data_producer is None
        assert runner._sim_id == "sim-runner-test"
        assert runner._vehicle_id == "veh-test"
        assert runner._scene_id == "scene-test-001"

    def test_producer_injected_with_correct_ids(self) -> None:
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        assert runner._sim_data_producer is producer
        assert runner._sim_id == "sim-runner-test"
        assert runner._vehicle_id == "veh-test"
        assert runner._scene_id == "scene-test-001"


# ── 测试：自车状态取数（V3.3.4 现场回归） ──────────────────────────────────────


def _vehicle() -> Any:
    return SimpleNamespace(
        get_location=lambda: SimpleNamespace(x=12.5, y=-3.0, z=0.5),
        get_rotation=lambda: SimpleNamespace(pitch=0.0, yaw=90.0, roll=0.0),
        get_velocity=lambda: SimpleNamespace(x=10.0, y=0.0, z=0.0),
        get_acceleration=lambda: SimpleNamespace(x=0.0, y=0.0, z=9.81),
        get_control=lambda: SimpleNamespace(steer=-0.25, throttle=0.6, brake=0.0),
    )


class _BuiltinLikeScenario:
    """``BuiltinScenario`` 形态：只暴露 ``ego_actor`` property，无 ``_ego_vehicle``。"""

    def __init__(self, actor: Any) -> None:
        self.ego_actor = actor


class _LegacyScenario:
    """旧形态：只提供 ``get_ego_vehicle()``。"""

    def __init__(self, actor: Any) -> None:
        self._actor = actor

    def get_ego_vehicle(self) -> Any:
        return self._actor


class TestEgoState:
    def test_ego_state_reads_ego_actor_attribute(self) -> None:
        """真机自车状态必须非零：viz 面板、sim_status/sim_telemetry 上行都依赖它。

        回归现场：``_get_ego_state`` 只找 ``get_ego_vehicle()`` / ``_ego_vehicle``，
        而 ``BuiltinScenario`` 暴露的是 ``ego_actor`` property，导致可视化页面
        「自车状态」全 0、上行位姿恒零、评估采样拿到静止假象。
        """
        runner = _make_runner()
        runner._scenario = _BuiltinLikeScenario(_vehicle())  # noqa: SLF001

        state = runner.get_ego_state()

        assert state["x"] == pytest.approx(12.5)
        assert state["y"] == pytest.approx(-3.0)
        assert state["z"] == pytest.approx(0.5)
        assert state["velocity"] == pytest.approx(10.0)
        assert state["heading"] == pytest.approx(math.radians(90.0), abs=1e-6)
        assert state["yaw"] == pytest.approx(math.radians(90.0), abs=1e-6)
        assert state["steering"] == pytest.approx(-0.25)
        assert state["acceleration"] == pytest.approx(9.81, abs=1e-3)

    def test_ego_state_matches_ego_vehicle_property(self) -> None:
        """状态取数必须与 ``ego_vehicle`` property 同源，避免两处解析规则漂移。"""
        actor = _vehicle()
        runner = _make_runner()
        runner._scenario = _BuiltinLikeScenario(actor)  # noqa: SLF001

        assert runner.ego_vehicle is actor
        assert runner.get_ego_state()["x"] == pytest.approx(12.5)

    def test_ego_state_supports_legacy_getter(self) -> None:
        """向后兼容：旧 scenario 仍可通过 ``get_ego_vehicle()`` 取到状态。"""
        runner = _make_runner()
        runner._scenario = _LegacyScenario(_vehicle())  # noqa: SLF001

        assert runner.get_ego_state()["x"] == pytest.approx(12.5)

    def test_ego_state_without_scenario_returns_empty(self) -> None:
        """无场景时返回空字典（而不是零值），避免把「无自车」写成真实位姿。"""
        runner = _make_runner()

        state = runner.get_ego_state()

        assert state == {}

    def test_ego_state_falls_back_to_world_hero(self) -> None:
        """场景对象拿不到自车时，按 ``role_name=hero`` 从世界兜底。"""
        hero = _vehicle()
        hero.attributes = {"role_name": "hero"}
        other = _vehicle()
        other.attributes = {"role_name": ""}
        world = SimpleNamespace(
            get_actors=lambda: SimpleNamespace(filter=lambda _pattern: [other, hero])
        )
        runner = _make_runner(world=world)

        assert runner.get_ego_state()["x"] == pytest.approx(12.5)

    def test_ego_state_falls_back_to_single_vehicle(self) -> None:
        """世界里没有 hero 标签但只有一辆车时，仍应取到该车状态。"""
        vehicle = _vehicle()
        vehicle.attributes = {"role_name": ""}
        world = SimpleNamespace(
            get_actors=lambda: SimpleNamespace(filter=lambda _pattern: [vehicle])
        )
        runner = _make_runner(world=world)

        assert runner.get_ego_state()["velocity"] == pytest.approx(10.0)

    def test_status_payload_carries_nonzero_ego_state(self) -> None:
        """上行 sim_status 的 ego_state 不能恒零（平台侧据此判断车辆在动）。"""
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(_minimal_config())
        runner._scenario = _BuiltinLikeScenario(_vehicle())  # noqa: SLF001

        runner._publish_sim_status()  # noqa: SLF001

        msg = producer.status_calls[-1]
        assert msg["payload"]["ego_state"]["x"] == pytest.approx(12.5)
        assert msg["payload"]["ego_state"]["velocity"] == pytest.approx(10.0)


class TestRestoreAsync:
    """场景结束/停止后必须恢复世界与 Traffic Manager 到异步模式。"""

    def test_restore_async_resets_world_and_tm(self) -> None:
        world = _FakeWorld()
        traffic_manager = MagicMock()
        runner = _make_runner(world=world, traffic_manager=traffic_manager)

        runner._restore_async()  # noqa: SLF001

        assert world.settings.synchronous_mode is False
        assert world.settings.fixed_delta_seconds == pytest.approx(0.0)
        assert world.apply_calls == 1
        traffic_manager.set_synchronous_mode.assert_called_once_with(False)

    def test_stop_restores_async_after_thread_joined(self) -> None:
        world = _FakeWorld()
        traffic_manager = MagicMock()
        runner = _make_runner(world=world, traffic_manager=traffic_manager)
        runner._state_mgr = MagicMock()  # noqa: SLF001
        runner._state_mgr.status = SceneStatus.RUNNING  # noqa: SLF001

        runner.stop(timeout_s=0.1)

        assert world.settings.synchronous_mode is False
        traffic_manager.set_synchronous_mode.assert_called_once_with(False)

    def test_restore_async_swallows_missing_world_api(self) -> None:
        runner = _make_runner(world=object())  # type: ignore[arg-type]
        runner._restore_async()  # noqa: SLF001  不抛异常

    def test_load_scene_reapplies_sync_after_previous_run(self) -> None:
        """运行循环退出会恢复异步；重下发场景必须重新切回同步再启动。"""
        world = _FakeWorld()
        world.settings.synchronous_mode = False
        world.settings.fixed_delta_seconds = 0.0
        traffic_manager = MagicMock()
        runner = _make_runner(world=world, traffic_manager=traffic_manager)

        with patch.object(runner, "_apply_scene_weather"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(_minimal_config())

        assert world.settings.synchronous_mode is True
        assert world.settings.fixed_delta_seconds == pytest.approx(0.02)
        traffic_manager.set_synchronous_mode.assert_called_with(True)
