"""交通触发条件单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Optional

from hunter_sim.traffic_sim.trigger_conditions import (
    DistanceTrigger,
    EventTrigger,
    PositionTrigger,
    TimeTrigger,
    VelocityTrigger,
)


class _Vec:
    def __init__(self, x: float, y: float, z: float = 0.0) -> None:
        self.x = x
        self.y = y
        self.z = z


class _Actor:
    def __init__(self, loc: _Vec, vel: Optional[_Vec] = None) -> None:
        self._loc = loc
        self._vel = vel or _Vec(0.0, 0.0)

    def get_location(self) -> _Vec:
        return self._loc

    def get_velocity(self) -> _Vec:
        return self._vel


class TestTimeTrigger:
    def test_fires_once_at_threshold(self) -> None:
        t = TimeTrigger(5.0)
        assert t.check(4.0, None, None) is False
        assert t.check(5.0, None, None) is True
        assert t.check(4.0, None, None) is True  # 一旦触发保持

    def test_reset(self) -> None:
        t = TimeTrigger(1.0)
        t.check(2.0, None, None)
        t.reset()
        assert t.check(0.5, None, None) is False


class TestDistanceTrigger:
    def test_within_range(self) -> None:
        trig = DistanceTrigger(10.0)
        actor = _Actor(_Vec(0.0, 0.0))
        ego = _Actor(_Vec(5.0, 0.0))
        assert trig.check(0.0, actor, ego) is True

    def test_out_of_range(self) -> None:
        trig = DistanceTrigger(3.0)
        actor = _Actor(_Vec(0.0, 0.0))
        ego = _Actor(_Vec(10.0, 0.0))
        assert trig.check(0.0, actor, ego) is False

    def test_no_ego_returns_false(self) -> None:
        trig = DistanceTrigger(5.0)
        assert trig.check(0.0, _Actor(_Vec(0.0, 0.0)), None) is False

    def test_bad_actor_returns_false(self) -> None:
        trig = DistanceTrigger(5.0)

        class _Bad:
            def get_location(self) -> None:
                raise RuntimeError("boom")

        assert trig.check(0.0, _Bad(), _Bad()) is False


class TestPositionTrigger:
    def test_in_radius(self) -> None:
        trig = PositionTrigger(100.0, 100.0, radius_m=5.0)
        actor = _Actor(_Vec(102.0, 102.0))
        assert trig.check(0.0, actor, None) is True

    def test_out_of_radius(self) -> None:
        trig = PositionTrigger(100.0, 100.0, radius_m=5.0)
        actor = _Actor(_Vec(200.0, 200.0))
        assert trig.check(0.0, actor, None) is False


class TestVelocityTrigger:
    def test_ego_speed_above(self) -> None:
        trig = VelocityTrigger(2.0, use_ego=True)
        ego = _Actor(_Vec(0.0, 0.0), vel=_Vec(3.0, 0.0))
        assert trig.check(0.0, None, ego) is True

    def test_actor_speed_below(self) -> None:
        trig = VelocityTrigger(5.0, use_ego=False)
        actor = _Actor(_Vec(0.0, 0.0), vel=_Vec(1.0, 1.0))
        assert trig.check(0.0, actor, None) is False

    def test_missing_target_returns_false(self) -> None:
        trig = VelocityTrigger(1.0, use_ego=True)
        assert trig.check(0.0, None, None) is False


class TestEventTrigger:
    def test_not_fired(self) -> None:
        trig = EventTrigger("brake")
        assert trig.check(0.0, None, None) is False

    def test_fire_and_reset(self) -> None:
        trig = EventTrigger("brake")
        trig.fire()
        assert trig.check(0.0, None, None) is True
        trig.reset()
        assert trig.check(0.0, None, None) is False
