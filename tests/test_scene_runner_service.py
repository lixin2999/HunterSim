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
import threading
import time
from types import SimpleNamespace
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

from hunter_sim.common.models import SceneStatus
from hunter_sim.engine.carla_mailbox import reset_mailboxes
from hunter_sim.scene_runner.event_detector import DetectedEvent
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
    SceneConfig,
    SpawnPoint,
)
from hunter_sim.scene_runner.scene_runner_service import SceneRunnerService
from tests.mocks.carla_thread_mocks import held_endpoint_owner


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
    engine_key: str = "",
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
        engine_key=engine_key,
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
    """真机形态的车辆替身：位姿只由 ``get_transform()`` 提供。

    CARLA ``Actor`` 没有 ``get_rotation()``（V3.4.3 现场回归）；桩里刻意不提供，
    避免再次出现「单测通过、真机每帧 AttributeError」的盲区。
    """
    return SimpleNamespace(
        get_transform=lambda: SimpleNamespace(
            location=SimpleNamespace(x=12.5, y=-3.0, z=0.5),
            rotation=SimpleNamespace(pitch=0.0, yaw=90.0, roll=0.0),
        ),
        get_velocity=lambda: SimpleNamespace(x=10.0, y=0.0, z=0.0),
        get_acceleration=lambda: SimpleNamespace(x=0.0, y=-2.0, z=9.81),
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

        state = runner._get_ego_state()  # noqa: SLF001  tick 线程专用入口

        assert state["x"] == pytest.approx(12.5)
        assert state["y"] == pytest.approx(-3.0)
        assert state["z"] == pytest.approx(0.5)
        assert state["velocity"] == pytest.approx(10.0)
        assert state["heading"] == pytest.approx(math.radians(90.0), abs=1e-6)
        assert state["yaw"] == pytest.approx(math.radians(90.0), abs=1e-6)
        assert state["steering"] == pytest.approx(-0.25)
        # V3.7.1：加速度改为「前向投影速度的时间导数 + 一阶低通」，不再用会抖动的
        # get_acceleration() 瞬时值。首帧无历史 → 0（避免近静止时的 ±数 m/s² 尖峰）。
        assert state["acceleration"] == pytest.approx(0.0, abs=1e-6)

    def test_acceleration_is_smoothed_velocity_derivative(self) -> None:
        """面板/遥测加速度取前向速度的时间导数（低通），制动为负。

        回归现场：V3.7.0 用 ``get_acceleration()`` 投影，自车近乎静止时逐帧
        抖动除以 dt 会得到“速度 0.03、加速度 6.2”的无意义大值。
        """
        holder = {"vy": 0.0}
        veh = SimpleNamespace(
            get_transform=lambda: SimpleNamespace(
                location=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                rotation=SimpleNamespace(pitch=0.0, yaw=90.0, roll=0.0),
            ),
            get_velocity=lambda: SimpleNamespace(x=0.0, y=holder["vy"], z=0.0),
            get_control=lambda: SimpleNamespace(steer=0.0, throttle=0.0, brake=0.0),
        )
        runner = _make_runner()
        runner._scenario = _BuiltinLikeScenario(veh)  # noqa: SLF001

        # yaw=90° 前向单位向量 (0,1) → 前向速度 = vel.y；首帧无历史→ 0
        s0 = runner._get_ego_state(delta_seconds=0.02)  # noqa: SLF001
        assert s0["acceleration"] == pytest.approx(0.0, abs=1e-6)

        # vel.y 从 0→-0.08，dt=0.02 → raw=-4.0；EMA(α=0.5)=0.5*-4+0.5*0=-2.0
        holder["vy"] = -0.08
        s1 = runner._get_ego_state(delta_seconds=0.02)  # noqa: SLF001
        assert s1["acceleration"] == pytest.approx(-2.0, abs=1e-6)

    def test_ego_state_matches_ego_vehicle_property(self) -> None:
        """状态取数必须与 ``ego_vehicle`` property 同源，避免两处解析规则漂移。"""
        actor = _vehicle()
        runner = _make_runner()
        runner._scenario = _BuiltinLikeScenario(actor)  # noqa: SLF001

        assert runner.ego_vehicle is actor
        assert runner._get_ego_state()["x"] == pytest.approx(12.5)  # noqa: SLF001

    def test_ego_state_supports_legacy_getter(self) -> None:
        """向后兼容：旧 scenario 仍可通过 ``get_ego_vehicle()`` 取到状态。"""
        runner = _make_runner()
        runner._scenario = _LegacyScenario(_vehicle())  # noqa: SLF001

        assert runner._get_ego_state()["x"] == pytest.approx(12.5)  # noqa: SLF001

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

        assert runner._get_ego_state()["x"] == pytest.approx(12.5)  # noqa: SLF001

    def test_ego_state_falls_back_to_single_vehicle(self) -> None:
        """世界里没有 hero 标签但只有一辆车时，仍应取到该车状态。"""
        vehicle = _vehicle()
        vehicle.attributes = {"role_name": ""}
        world = SimpleNamespace(
            get_actors=lambda: SimpleNamespace(filter=lambda _pattern: [vehicle])
        )
        runner = _make_runner(world=world)

        assert runner._get_ego_state()["velocity"] == pytest.approx(10.0)  # noqa: SLF001

    def test_status_payload_carries_nonzero_ego_state(self) -> None:
        """上行 sim_status 的 ego_state 不能恒零（平台侧据此判断车辆在动）。

        V3.6.0：``_publish_sim_status`` 只读 tick 线程写的缓存，不再自己发 RPC；
        用例先用 ``_get_ego_state()`` 模拟一帧 tick 的取数，再验证上报内容。
        """
        producer = _FakeProducer()
        runner = _make_runner(producer=producer)
        with patch.object(runner, "_apply_scene_weather"), \
             patch.object(runner, "_cleanup"), \
             patch("hunter_sim.scene_runner.scene_runner_service.BuiltinScenario") as MockScenario:
            MockScenario.return_value.setup = MagicMock()
            runner.load_scene(_minimal_config())
        runner._scenario = _BuiltinLikeScenario(_vehicle())  # noqa: SLF001

        runner._get_ego_state()  # noqa: SLF001  等价于 tick 线程每帧取数写缓存
        runner._publish_sim_status()  # noqa: SLF001

        msg = producer.status_calls[-1]
        assert msg["payload"]["ego_state"]["x"] == pytest.approx(12.5)
        assert msg["payload"]["ego_state"]["velocity"] == pytest.approx(10.0)

    def test_public_get_ego_state_never_touches_world(self) -> None:
        """V3.5.6 ②：公开入口只读缓存，缓存未写入时不得退回直读 CARLA。

        回归现场（大量 502 的另一条根因链）：首个成功 tick 之前缓存为空（日志
        ``Ego state unavailable (stage=actor_missing)`` 即该窗口），executor 线程的
        0.5s 进度轮询会落到 ``world.get_actors()`` / ``get_transform()``，与 tick
        线程的 ``spawn_actor`` / ``actor.destroy()`` 并发操作同一个 Client 连接
        → 进程无 traceback 原生退出（0xC00000FD）。
        """
        world = _FakeWorld()
        world.get_actors = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("非 tick 线程不得直读 CARLA")
        )
        runner = _make_runner(world=world)

        assert runner.get_ego_state() == {}
        world.get_actors.assert_not_called()  # type: ignore[attr-defined]

    def test_public_get_ego_state_returns_cache_copy(self) -> None:
        """有缓存时返回副本：调用方改写不得污染 tick 线程写入的快照。"""
        runner = _make_runner()
        runner._ego_state_cache = {"x": 1.0}  # noqa: SLF001

        snapshot = runner.get_ego_state()
        snapshot["x"] = 99.0

        assert runner._ego_state_cache == {"x": 1.0}  # noqa: SLF001


class _AliveThread:
    """永不退出的 tick 线程替身：join 超时后 ``is_alive()`` 仍为 True。"""

    def __init__(self) -> None:
        self.join_timeouts: list[float] = []

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(float(timeout or 0.0))


class TestStopDestroyDeferral:
    """V3.5.6 ①：join 超时必须把「tick 线程仍持有 world」告知调用方。"""

    def _running_runner(self, world: _FakeWorld) -> SceneRunnerService:
        runner = _make_runner(world=world)
        runner._state_mgr = MagicMock()  # noqa: SLF001
        runner._state_mgr.status = SceneStatus.RUNNING  # noqa: SLF001
        runner._thread = _AliveThread()  # noqa: SLF001
        return runner

    def test_stop_returns_deferred_and_skips_world_writes(self) -> None:
        world = _FakeWorld()
        runner = self._running_runner(world)

        assert runner.stop(timeout_s=0.01) is True

        assert world.apply_calls == 0, "tick 线程仍在跑，本线程不得修改世界设置"

    def test_destroy_returns_deferred_and_keeps_scenario(self) -> None:
        """被接走的清理不得提前丢场景引用（否则 tick 线程收尾时无 Actor 可销）。"""
        world = _FakeWorld()
        runner = self._running_runner(world)
        runner._scenario = _BuiltinLikeScenario(_vehicle())  # noqa: SLF001

        assert runner.destroy(timeout_s=0.01) is True

        assert runner._scenario is not None  # noqa: SLF001
        assert world.apply_calls == 0

    def test_join_timeout_allows_real_cleanup_duration(self) -> None:
        """join 上限必须覆盖真实销毁耗时（现场 4 传感器+自车+3 参与者约 7s）。

        旧值 5s 会在清理未完时提前返回，直接制造了释放线程与 tick 线程并发。
        """
        from hunter_sim.scene_runner import scene_runner_service as module

        assert module._RUNNER_JOIN_TIMEOUT_S >= 15.0  # noqa: SLF001

    def test_load_scene_rejects_reload_while_tick_thread_alive(self) -> None:
        """上一场景还在销毁时重下发必须被拒（宁 FAILED 不可崩溃）。"""
        from hunter_sim.common.exceptions import CarlaSimulationError

        world = _FakeWorld()
        runner = self._running_runner(world)

        with pytest.raises(CarlaSimulationError, match="scene_reload"):
            runner.load_scene(_minimal_config())

        assert world.apply_calls == 0


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


@pytest.fixture
def _clean_mailboxes() -> Iterator[None]:
    reset_mailboxes()
    yield
    reset_mailboxes()


class TestOffOwnerReadsDegradeToCache:
    """V3.6.0：端点被 tick 线程占住时，其他线程一律不得碰 CARLA。

    原生崩溃的唯一成因就是「两个线程同时操作同一个 Client 连接」；这里的守护
    不是日志告警，而是**直接拒接 RPC**并退回缓存/空值：读数允许缺席，
    不允许用崩溃换读数。
    """

    def test_ego_state_read_returns_cache_without_rpc(self, _clean_mailboxes: None) -> None:
        world = _FakeWorld()
        world.get_actors = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("owner 占用期间不得直读 CARLA")
        )
        runner = _make_runner(world=world, engine_key="127.0.0.1:2071")
        runner._ego_state_cache = {"x": 3.5, "velocity": 8.0}  # noqa: SLF001

        with held_endpoint_owner(runner.mailbox):
            state = runner._get_ego_state()  # noqa: SLF001

        assert state == {"x": 3.5, "velocity": 8.0}
        world.get_actors.assert_not_called()  # type: ignore[attr-defined]

    def test_actor_count_read_returns_cache_without_rpc(self, _clean_mailboxes: None) -> None:
        world = _FakeWorld()
        world.get_actors = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("owner 占用期间不得直读 CARLA")
        )
        runner = _make_runner(world=world, engine_key="127.0.0.1:2072")
        runner._actors_count_cache = 7  # noqa: SLF001

        with held_endpoint_owner(runner.mailbox):
            assert runner._get_actor_count() == 7  # noqa: SLF001

        world.get_actors.assert_not_called()  # type: ignore[attr-defined]

    def test_ego_vehicle_world_fallback_is_skipped(self, _clean_mailboxes: None) -> None:
        """场景对象里没有自车时的「世界兜底查询」在异线程直接放弃。"""
        world = _FakeWorld()
        world.get_actors = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("owner 占用期间不得直读 CARLA")
        )
        runner = _make_runner(world=world, engine_key="127.0.0.1:2073")

        with held_endpoint_owner(runner.mailbox):
            assert runner.ego_vehicle is None

        world.get_actors.assert_not_called()  # type: ignore[attr-defined]

    def test_sim_status_publish_carries_no_rpc_when_endpoint_busy(
        self, _clean_mailboxes: None
    ) -> None:
        """周期性状态上报由异线程触发时，只能读缓存（现场 503/崩溃同源路径）。"""
        producer = _FakeProducer()
        world = _FakeWorld()
        world.get_actors = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("owner 占用期间不得直读 CARLA")
        )
        runner = _make_runner(producer=producer, world=world, engine_key="127.0.0.1:2074")
        runner._state_mgr = MagicMock()  # noqa: SLF001
        runner._state_mgr.get_snapshot.return_value.status.value = "RUNNING"  # noqa: SLF001
        runner._state_mgr.get_snapshot.return_value.elapsed_seconds = 1.0  # noqa: SLF001
        runner._state_mgr.get_snapshot.return_value.progress = 0.5  # noqa: SLF001
        runner._state_mgr.get_snapshot.return_value.error_message = None  # noqa: SLF001
        runner._ego_state_cache = {"x": 1.0}  # noqa: SLF001
        runner._actors_count_cache = 3  # noqa: SLF001

        with held_endpoint_owner(runner.mailbox):
            runner._publish_sim_status()  # noqa: SLF001

        world.get_actors.assert_not_called()  # type: ignore[attr-defined]
        payload = producer.status_calls[-1]["payload"]
        assert payload["actors_count"] == 3
        assert payload["ego_state"]["x"] == 1.0

    def test_teardown_is_deferred_while_owner_holds_endpoint(
        self, _clean_mailboxes: None
    ) -> None:
        """无 tick 线程但端点被他人占用：收尾走信箱排队，本线程不发 RPC。"""
        world = _FakeWorld()
        traffic_manager = MagicMock()
        runner = _make_runner(
            world=world, traffic_manager=traffic_manager, engine_key="127.0.0.1:2075"
        )
        runner._state_mgr = MagicMock()  # noqa: SLF001
        runner._state_mgr.status = SceneStatus.RUNNING  # noqa: SLF001

        with held_endpoint_owner(runner.mailbox) as owner:
            deferred = runner.stop(timeout_s=0.1)
            assert deferred is True
            assert world.apply_calls == 0, "收尾不得由调用线程执行"

        # owner 线程退出前代执行了排队的收尾：世界回到异步
        assert owner.executed >= 1
        assert world.apply_calls == 1
        traffic_manager.set_synchronous_mode.assert_called_once_with(False)

    def test_owner_thread_still_reads_world_directly(self, _clean_mailboxes: None) -> None:
        """守护不能把正常 tick 路径也堵死：owner 线程照旧读数。"""
        vehicle = _vehicle()
        world = SimpleNamespace(
            get_actors=lambda: SimpleNamespace(filter=lambda _pattern: [vehicle])
        )
        runner = _make_runner(world=world, engine_key="127.0.0.1:2076")  # type: ignore[arg-type]

        assert runner.mailbox.claim("MainThread", timeout=0.0) is True
        try:
            assert runner._get_ego_state()["x"] == pytest.approx(12.5)  # noqa: SLF001
            assert runner._actor_from_world() is vehicle  # noqa: SLF001
        finally:
            runner.mailbox.relinquish()


class TestTickLoopDrainsMailbox:
    """V3.6.0：tick 循环必须**每帧**消费信箱，而不是等整个场景跑完。

    只在循环退出时 ``drain()`` 会让场景运行期间的排队调用全部超时降级（场景
    可跑几十分钟），信箱退化为「只能降级」，收尾/读数拿不到任何结果。
    """

    def test_off_owner_call_is_executed_while_loop_runs(
        self, _clean_mailboxes: None
    ) -> None:
        runner = _make_runner(engine_key="127.0.0.1:2090")
        runner._config = _minimal_config()  # noqa: SLF001
        runner._state_mgr = None  # noqa: SLF001  不走 PAUSED 分支
        runner._get_ego_state = lambda *_a, **_k: {}  # type: ignore[method-assign]  # noqa: SLF001
        runner._refresh_actor_states = lambda: None  # type: ignore[assignment]  # noqa: SLF001
        runner._refresh_actor_count = lambda: None  # type: ignore[assignment]  # noqa: SLF001

        def _owner() -> None:
            if not runner.mailbox.claim("Tick-Drain", timeout=1.0):
                return
            try:
                runner._run_loop_body()  # noqa: SLF001
            finally:
                runner.mailbox.relinquish()

        thread = threading.Thread(target=_owner, name="Tick-Drain", daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 2.0
            while runner.mailbox.owner_name != "Tick-Drain":
                assert time.monotonic() < deadline, "owner 未被 tick 线程拿到"
                time.sleep(0.01)

            result = runner.mailbox.run(
                lambda: threading.current_thread().name,
                name="drained_op",
                timeout=2.0,
                fallback="not-executed",
            )

            assert result == "Tick-Drain", "排队调用必须由 owner 线程代执行"
            assert runner.mailbox.stats["executed"] >= 1
        finally:
            runner._stop_event.set()  # noqa: SLF001
            thread.join(timeout=5.0)
            assert not thread.is_alive()

    def test_paused_loop_still_drains(self, _clean_mailboxes: None) -> None:
        """暂停不等于停止：收尾任务不能被暂停分支卡到超时。"""
        runner = _make_runner(engine_key="127.0.0.1:2091")
        runner._config = _minimal_config()  # noqa: SLF001
        runner._state_mgr = MagicMock()  # noqa: SLF001
        runner._state_mgr.status = SceneStatus.PAUSED

        def _owner() -> None:
            if not runner.mailbox.claim("Tick-Paused", timeout=1.0):
                return
            try:
                runner._run_loop_body()  # noqa: SLF001
            finally:
                runner.mailbox.relinquish()

        thread = threading.Thread(target=_owner, name="Tick-Paused", daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 2.0
            while runner.mailbox.owner_name != "Tick-Paused":
                assert time.monotonic() < deadline, "owner 未被 tick 线程拿到"
                time.sleep(0.01)

            assert (
                runner.mailbox.run(
                    lambda: 1, name="paused_drained", timeout=2.0, fallback=0
                )
                == 1
            )
        finally:
            runner._stop_event.set()  # noqa: SLF001
            thread.join(timeout=5.0)
            assert not thread.is_alive()
