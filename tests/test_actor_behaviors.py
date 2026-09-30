"""交通参与者行为单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Optional

import pytest

from hunter_sim.traffic_sim.actor_behaviors import (
    ActorAction,
    ConstantSpeedBehavior,
    CutInBehavior,
    DecelerateBehavior,
    PedestrianCrossBehavior,
    ScriptedActorController,
    StaticBehavior,
    create_behavior_from_config,
)
from hunter_sim.traffic_sim.trigger_conditions import EventTrigger, TimeTrigger


class _Vec3:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class _Actor:
    def __init__(self, vel: Optional[_Vec3] = None) -> None:
        self._vel = vel or _Vec3(0.0, 0.0, 0.0)

    def get_velocity(self) -> _Vec3:
        return self._vel


class TestActorAction:
    def test_defaults(self) -> None:
        a = ActorAction()
        assert a.target_speed_ms == 0.0
        assert a.is_stop is False


class TestConstantSpeedBehavior:
    def test_throttle_when_slow(self) -> None:
        b = ConstantSpeedBehavior(target_speed_ms=10.0)
        b.start()
        action = b.update(0.0, _Actor(_Vec3(0.0, 0.0, 0.0)), None, 0.02)
        assert action.target_speed_ms == 10.0
        assert action.throttle == 1.0  # (10-0)*0.5=5 clamped to <=1.0

    def test_no_throttle_at_speed(self) -> None:
        b = ConstantSpeedBehavior(target_speed_ms=5.0)
        action = b.update(0.0, _Actor(_Vec3(5.0, 0.0, 0.0)), None, 0.02)
        assert action.throttle == 0.0

    def test_bad_actor_speed_zero(self) -> None:
        class _Bad:
            def get_velocity(self) -> None:
                raise RuntimeError("x")

        assert ConstantSpeedBehavior._get_current_speed(_Bad()) == 0.0

    def test_start_stop_active(self) -> None:
        b = ConstantSpeedBehavior()
        assert b.is_active is False
        b.start()
        assert b.is_active is True
        b.stop()
        assert b.is_active is False


class TestDecelerateBehavior:
    def test_initial_brake(self) -> None:
        b = DecelerateBehavior(initial_speed_ms=8.0, deceleration_ms2=2.0)
        action = b.update(0.0, None, None, 0.02)
        assert action.target_speed_ms == 8.0
        assert action.brake > 0.0
        assert action.is_stop is False

    def test_reaches_stop(self) -> None:
        b = DecelerateBehavior(initial_speed_ms=4.0, deceleration_ms2=2.0, target_speed_ms=0.0)
        action = b.update(5.0, None, None, 0.02)  # 4 - 2*5 = -6 -> 0
        assert action.target_speed_ms == 0.0
        assert action.is_stop is True
        assert action.brake == 0.0

    def test_target_floor(self) -> None:
        b = DecelerateBehavior(initial_speed_ms=8.0, deceleration_ms2=2.0, target_speed_ms=3.0)
        action = b.update(10.0, None, None, 0.02)
        assert action.target_speed_ms == 3.0


class TestCutInBehavior:
    def test_steering_scurve(self) -> None:
        b = CutInBehavior(speed_ms=6.0, duration_s=4.0)
        mid = b.update(2.0, None, None, 0.02)  # progress=0.5 -> sin(pi/2)=1
        assert mid.steering == 0.6
        assert mid.target_speed_ms == 6.0

    def test_end_no_steer(self) -> None:
        b = CutInBehavior(duration_s=4.0)
        end = b.update(4.0, None, None, 0.02)  # progress=1 -> sin(pi)=~0
        assert abs(end.steering) < 1e-6


class TestStaticBehavior:
    def test_always_stop(self) -> None:
        action = StaticBehavior().update(1.0, None, None, 0.02)
        assert action.is_stop is True
        assert action.brake == 1.0


class TestPedestrianCrossBehavior:
    def test_speed_capped(self) -> None:
        b = PedestrianCrossBehavior(speed_ms=3.0)  # max 1.4
        action = b.update(0.0, None, None, 0.02)
        assert action.target_speed_ms <= 1.4

    def test_done_after_duration(self) -> None:
        b = PedestrianCrossBehavior(crossing_duration_s=5.0)
        action = b.update(6.0, None, None, 0.02)
        assert action.is_stop is True
        assert action.target_speed_ms == 0.0


class TestDecelerateTriggerTime:
    """文档 §7.4.1：decelerate 行为在 trigger_time 后才开始减速。"""

    def test_holds_initial_speed_before_trigger(self) -> None:
        b = DecelerateBehavior(initial_speed_ms=8.0, deceleration_ms2=2.0, trigger_time_s=3.0)
        action = b.update(1.0, None, None, 0.02)
        assert action.target_speed_ms == 8.0
        assert action.brake == 0.0

    def test_decelerates_after_trigger(self) -> None:
        b = DecelerateBehavior(initial_speed_ms=8.0, deceleration_ms2=2.0, trigger_time_s=3.0)
        action = b.update(4.0, None, None, 0.02)  # 触发后 1s: 8 - 2*1 = 6
        assert action.target_speed_ms == pytest.approx(6.0)


class TestBehaviorFactory:
    """create_behavior_from_config（文档 §7.4.1 type 字段）。"""

    def test_constant_speed(self) -> None:
        b = create_behavior_from_config({"type": "constant_speed", "speed": 6.0})
        assert isinstance(b, ConstantSpeedBehavior)

    def test_decelerate(self) -> None:
        b = create_behavior_from_config(
            {"type": "decelerate", "trigger_time": 2.0, "deceleration": 3.0}
        )
        assert isinstance(b, DecelerateBehavior)

    def test_cut_in(self) -> None:
        b = create_behavior_from_config({"type": "cut_in", "speed": 5.0})
        assert isinstance(b, CutInBehavior)

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError):
            create_behavior_from_config({"type": "fly_away"})


class TestScriptedActorController:
    """触发 + 行为组合执行（文档 §7.4）。"""

    def test_no_action_before_trigger(self) -> None:
        ctrl = ScriptedActorController(
            object(), ConstantSpeedBehavior(5.0), trigger=TimeTrigger(5.0)
        )
        assert ctrl.update(None, 1.0) is None  # t=1s 未触发
        action = ctrl.update(None, 5.0)  # t=6s 已触发
        assert action is not None
        assert action.target_speed_ms == 5.0

    def test_no_trigger_acts_immediately(self) -> None:
        ctrl = ScriptedActorController(object(), StaticBehavior())
        action = ctrl.update(None, 0.02)
        assert action is not None and action.is_stop is True

    def test_reset_clears_trigger_state(self) -> None:
        trig = EventTrigger("prev_event")
        trig.fire()
        ctrl = ScriptedActorController(object(), ConstantSpeedBehavior(5.0), trigger=trig)
        ctrl.update(None, 0.02)
        assert ctrl.is_triggered is True
        ctrl.reset()
        assert ctrl.is_triggered is False
