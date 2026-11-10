"""场景事件检测器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import pytest

from hunter_sim.common.models import Transform, VehicleState
from hunter_sim.scene_runner.event_detector import DetectedEvent, EventDetector, TriggerMetrics
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
            "event_level": "info",
            "sim_time": 0.0,
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


class TestSceneTriggers:
    """平台事件每 tick 评估（V1.26.0 / J+K+L）。"""

    @staticmethod
    def _trigger(
        trigger_type: TriggerType, value: float, comparator: str = ">"
    ) -> SceneEventTrigger:
        return SceneEventTrigger(
            trigger_type=trigger_type,
            value=value,
            comparator=comparator,  # type: ignore[arg-type]
            source_condition_type=trigger_type.value,
        )

    def _definition(
        self,
        event_id: str,
        trigger_type: TriggerType,
        value: float,
        comparator: str = ">",
        *,
        evaluable: bool = True,
    ) -> SceneEventDefinition:
        return SceneEventDefinition(
            event_id=event_id,
            event_type=SceneEventType.CUSTOM,
            trigger=self._trigger(trigger_type, value, comparator),
            is_runtime_evaluable=evaluable,
        )

    def test_scene_trigger_emits_trigger_reached(self) -> None:
        det = EventDetector([self._definition("e1", TriggerType.TIME, 2.0)])
        det.update_scene_triggers(1.0, TriggerMetrics(elapsed_s=1.0))
        assert det.detected_events == []
        # 严格大于：t=2.0 与阈值相等不触发，t=2.5 才越过阈值
        det.update_scene_triggers(2.0, TriggerMetrics(elapsed_s=2.0))
        assert det.detected_events == []
        det.update_scene_triggers(2.5, TriggerMetrics(elapsed_s=2.5))
        assert len(det.detected_events) == 1
        event = det.detected_events[0]
        assert event.event_id == "e1"
        assert event.event_type == SceneEventType.TRIGGER_REACHED.value
        assert event.data["actual"] == pytest.approx(2.5)
        assert event.data["threshold"] == pytest.approx(2.0)

    def test_comparator_lt_and_gt(self) -> None:
        det = EventDetector([self._definition("e1", TriggerType.DISTANCE, 10.0, "<")])
        metrics = TriggerMetrics(relative_distance_m={"euclidean": 12.0})
        det.update_scene_triggers(1.0, metrics)
        assert det.detected_events == []
        det.update_scene_triggers(2.0, TriggerMetrics(relative_distance_m={"euclidean": 5.0}))
        assert len(det.detected_events) == 1

    def test_relation_selects_distance_metric(self) -> None:
        trigger = SceneEventTrigger(
            trigger_type=TriggerType.DISTANCE,
            value=10.0,
            comparator="<",
            relation="longitudinal",
        )
        det = EventDetector(
            [
                SceneEventDefinition(
                    event_id="e1", event_type=SceneEventType.CUSTOM, trigger=trigger
                )
            ]
        )
        det.update_scene_triggers(
            1.0,
            TriggerMetrics(
                relative_distance_m={"euclidean": 2.0, "longitudinal": 30.0}
            ),
        )
        assert det.detected_events == []

    def test_disabled_never_emits(self) -> None:
        definition = SceneEventDefinition(
            event_id="e1",
            event_type=SceneEventType.CUSTOM,
            trigger=SceneEventTrigger(trigger_type=TriggerType.DISABLED),
            is_runtime_evaluable=False,
        )
        det = EventDetector([definition])
        det.update_scene_triggers(
            10.0,
            TriggerMetrics(
                elapsed_s=10.0,
                traveled_distance_m=100.0,
                speed_ms=10.0,
                acceleration_ms2=5.0,
            ),
        )
        assert det.detected_events == []

    def test_edge_latch_once(self) -> None:
        det = EventDetector([self._definition("e1", TriggerType.TIME, 1.0)])
        for index in range(5):
            det.update_scene_triggers(
                float(index), TriggerMetrics(elapsed_s=2.0 + index)
            )
        assert len(det.detected_events) == 1

    def test_reset_rearms_scene_trigger(self) -> None:
        det = EventDetector([self._definition("e1", TriggerType.TIME, 1.0)])
        det.update_scene_triggers(1.0, TriggerMetrics(elapsed_s=2.0))
        det.reset()
        det.update_scene_triggers(2.0, TriggerMetrics(elapsed_s=3.0))
        assert len(det.detected_events) == 1

    def test_trigger_not_counted_as_collision(self) -> None:
        definition = self._definition("e1", TriggerType.TIME, 1.0)
        # 平台事件标记本身不是失败条件；失败计数只由 collision/emergency_brake 定义决定
        definition = definition.model_copy(update={"is_failure_condition": False})
        det = EventDetector([definition])
        det.update_scene_triggers(1.0, TriggerMetrics(elapsed_s=2.0))
        assert all(
            event.event_type != SceneEventType.COLLISION.value
            for event in det.detected_events
        )
        assert det.failure_events == []

    def test_optional_metrics_skip_when_missing(self) -> None:
        det = EventDetector(
            [self._definition("thw", TriggerType.TIME_HEADWAY, 1.0, "<")]
        )
        det.update_scene_triggers(1.0, TriggerMetrics(time_headway_s=None))
        assert det.detected_events == []
        det.update_scene_triggers(2.0, TriggerMetrics(time_headway_s=0.5))
        assert len(det.detected_events) == 1

    def test_offroad_metric(self) -> None:
        det = EventDetector([self._definition("off", TriggerType.OFFROAD, 0.5, ">")])
        det.update_scene_triggers(1.0, TriggerMetrics(offroad=False))
        assert det.detected_events == []
        det.update_scene_triggers(2.0, TriggerMetrics(offroad=True))
        assert len(det.detected_events) == 1


class TestEdgeLatchAndBoundedBuffer:
    """审查项 H：边沿触发闩锁 + 有界缓冲，防 50Hz 每 tick 检查事件洪泛。"""

    def test_persistent_condition_reports_once(self) -> None:
        det = EventDetector([])
        for i in range(50):  # 超速条件持续成立 50 tick
            det.check_speed_violation(1.0 + i * 0.02, speed_ms=12.0, limit_ms=10.0)
        assert len(det.detected_events) == 1

    def test_rearm_after_condition_clears(self) -> None:
        det = EventDetector([])
        det.check_speed_violation(1.0, speed_ms=12.0, limit_ms=10.0)  # 上升沿 → 上报
        det.check_speed_violation(2.0, speed_ms=8.0, limit_ms=10.0)   # 条件消失 → 释闩
        det.check_speed_violation(3.0, speed_ms=12.0, limit_ms=10.0)  # 再次上升沿 → 上报
        assert len(det.detected_events) == 2

    def test_reset_clears_latch(self) -> None:
        det = EventDetector([])
        det.check_emergency_brake(1.0, acceleration_ms2=-5.0)  # 上报并闩锁
        det.reset()
        det.check_emergency_brake(2.0, acceleration_ms2=-5.0)  # reset 后重新上报
        assert len(det.detected_events) == 1

    def test_buffer_is_bounded(self) -> None:
        det = EventDetector([], max_events=5)
        for i in range(10):  # 碰撞回调事件非闩锁，逐条入缓冲
            det.on_collision(float(i), other_actor_id=i, impulse=10.0)
        assert len(det.detected_events) == 5
        # 保留最新事件（FIFO 淘汰旧条目）
        assert det.detected_events[-1].data["other_actor_id"] == 9
