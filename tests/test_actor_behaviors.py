"""交通参与者行为单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Optional

from hunter_sim.traffic_sim.actor_behaviors import (
    ActorAction,
    ConstantSpeedBehavior,
    CutInBehavior,
    DecelerateBehavior,
    PedestrianCrossBehavior,
    StaticBehavior,
)


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
