"""场景事件检测器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import pytest

from hunter_sim.common.models import Transform, VehicleState
from hunter_sim.scene_runner.event_detector import DetectedEvent, EventDetector
from hunter_sim.scene_runner.scene_config import (
    SceneEventDefinition,
    SceneEventTrigger,
    SceneEventType,
    TriggerType,
)


def _def(event_id: str, etype: SceneEventType, failure: bool = True) -> SceneEventDefinition:
    return SceneEventDefinition(
        event_id=event_id,
        event_type=etype,
        trigger=SceneEventTrigger(trigger_type=TriggerType.TIME, value=1.0),
        is_failure_condition=failure,
    )


def _state(speed: float = 0.0, accel=(0.0, 0.0, 0.0), brake: float = 0.0) -> VehicleState:
    return VehicleState(
        time_stamp=0.0,
        transform=Transform(),
        vehicle_speed=speed,
        acceleration=accel,
        brake=brake,
    )


class TestDetectedEvent:
    def test_to_dict_roundtrip(self) -> None:
        ev = DetectedEvent("e1", "collision", 2.5, "desc", {"a": 1})
        d = ev.to_dict()
        assert d == {
            "event_id": "e1",
            "event_type": "collision",
            "time_stamp": 2.5,
            "description": "desc",
            "data": {"a": 1},
        }


class TestEventDetectorCallbacks:
    def test_collision_emits_event(self) -> None:
        received: list[DetectedEvent] = []
        det = EventDetector([], on_event=received.append)
        det.on_collision(1.0, other_actor_id=42, impulse=120.0)
        assert len(received) == 1
        ev = received[0]
        assert ev.event_type == SceneEventType.COLLISION.value
        assert ev.data["other_actor_id"] == 42
        assert det.detected_events[0] is ev

    def test_lane_invasion_emits(self) -> None:
        det = EventDetector([])
        det.on_lane_invasion(0.5, "solid")
        assert det.detected_events[0].data == {"lane_type": "solid"}

    def test_speed_violation_threshold(self) -> None:
        det = EventDetector([])
        det.check_speed_violation(1.0, speed_ms=10.5, limit_ms=10.0)  # 10.5 <= 11 不触发
        assert det.detected_events == []
        det.check_speed_violation(2.0, speed_ms=12.0, limit_ms=10.0)  # 12 > 11 触发
        assert len(det.detected_events) == 1
        assert det.detected_events[0].event_type == SceneEventType.SPEED_VIOLATION.value

    def test_emergency_brake_threshold(self) -> None:
        det = EventDetector([])
        det.check_emergency_brake(1.0, acceleration_ms2=-2.0)  # 未达阈值
        assert det.detected_events == []
        det.check_emergency_brake(2.0, acceleration_ms2=-5.0)  # 触发
        assert det.detected_events[0].data["deceleration"] == pytest.approx(5.0)

    def test_check_tick_speed_and_brake(self) -> None:
        det = EventDetector([])
        det.check_tick(1.0, _state(speed=15.0, accel=(-6.0, 0.0, 0.0), brake=0.9), speed_limit_ms=10.0)
        types = {e.event_type for e in det.detected_events}
        assert SceneEventType.SPEED_VIOLATION.value in types
        assert SceneEventType.EMERGENCY_BRAKE.value in types

    def test_check_tick_no_brake_when_low(self) -> None:
        det = EventDetector([])
        det.check_tick(1.0, _state(speed=5.0, accel=(-6.0, 0.0, 0.0), brake=0.2), speed_limit_ms=10.0)
        types = {e.event_type for e in det.detected_events}
        assert SceneEventType.EMERGENCY_BRAKE.value not in types


class TestFailureAndReset:
    def test_failure_events_filtered(self) -> None:
        defs = [
            _def("collision_detected", SceneEventType.COLLISION, failure=True),
            _def("lane_invasion_detected", SceneEventType.LANE_INVASION, failure=False),
        ]
        det = EventDetector(defs)
        det.on_collision(1.0, 1, 50.0)
        det.on_lane_invasion(2.0, "broken")
        failures = det.failure_events
        assert [e.event_id for e in failures] == ["collision_detected"]

    def test_reset_clears(self) -> None:
        det = EventDetector([])
        det.on_collision(1.0, 1, 50.0)
        det.reset()
        assert det.detected_events == []

    def test_callback_exception_swallowed(self) -> None:
        def _boom(ev: DetectedEvent) -> None:
            raise RuntimeError("cb fail")

        det = EventDetector([], on_event=_boom)
        det.on_collision(1.0, 1, 50.0)  # 不应抛出
        assert len(det.detected_events) == 1
