"""SceneRunnerService 平台发布链路集成测试（V2.10.0）。

验证 sim_data_producer 注入后：
- _publish_sim_status() 正确构建消息并发布；
- _publish_sim_telemetry() 正确构建消息并发布；
- publish_event_to_platform() 调用 SimDataProducer.publish_event；
- 未注入 producer 时全部安全 no-op，不抛异常。

使用 Mock world/scenario 避免 CARLA 依赖。
"""

from __future__ import annotations

import time
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
from hunter_sim.scene_runner.scene_state_manager import SceneStateSnapshot


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


class _FakeWorld:
    """Mock CARLA World。"""

    def tick(self) -> int:
        return 0

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


def _make_runner(producer: Optional[Any] = None) -> SceneRunnerService:
    """构造带/不带 sim_data_producer 的 SceneRunnerService。"""
    world = _FakeWorld()
    return SceneRunnerService(
        world=world,
        sim_data_producer=producer,
        sim_id="sim-runner-test",
        vehicle_id="veh-test",
        scene_id="scene-test-001",
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

        # 手动触发发布
        runner._publish_sim_status()
        assert len(producer.status_calls) == 1
        msg = producer.status_calls[0]
        assert msg["type"] == "sim_status"
        assert msg["sim_id"] == "sim-runner-test"
        assert "payload" in msg

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
        payload = producer.status_calls[0]["payload"]
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
