"""交通触发条件单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Optional

import pytest

from hunter_sim.traffic_sim.trigger_conditions import (
    AccelerationTrigger,
    DistanceTrigger,
    EventTrigger,
    OffroadTrigger,
    PositionTrigger,
    TimeHeadwayTrigger,
    TimeToCollisionTrigger,
    TimeTrigger,
    TraveledDistanceTrigger,
    VelocityTrigger,
    create_trigger_from_config,
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


class TestTriggerFactory:
    """create_trigger_from_config（文档 §7.4.2 五种触发条件）。"""

    def test_time(self) -> None:
        assert isinstance(create_trigger_from_config({"type": "time", "value": 3.0}), TimeTrigger)

    def test_distance(self) -> None:
        assert isinstance(
            create_trigger_from_config({"type": "distance", "value": 10.0}), DistanceTrigger
        )

    def test_position(self) -> None:
        assert isinstance(
            create_trigger_from_config({"type": "position", "x": 1.0, "y": 2.0}), PositionTrigger
        )

    def test_velocity(self) -> None:
        assert isinstance(
            create_trigger_from_config({"type": "velocity", "value": 5.0}), VelocityTrigger
        )

    def test_event(self) -> None:
        trig = create_trigger_from_config({"type": "event", "event_name": "prev_done"})
        assert isinstance(trig, EventTrigger)
        assert trig.check(0.0, None, None) is False

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError):
            create_trigger_from_config({"type": "magic"})


class _MetricActor:
    """携带加速度/里程/越界/THW/TTC 属性的测试 Actor。"""

    def __init__(self, **metrics: object) -> None:
        self._metrics = metrics

    def __getattr__(self, name: str) -> object:
        if name in self._metrics:
            return self._metrics[name]
        raise AttributeError(name)


class _AccelActor(_MetricActor):
    def __init__(self, magnitude: float) -> None:
        super().__init__()
        self._magnitude = magnitude

    def get_acceleration(self) -> _Vec:
        return _Vec(self._magnitude, 0.0)


class TestExtendedTriggers:
    def test_acceleration_threshold(self) -> None:
        trig = AccelerationTrigger(3.0, comparator=">")
        assert trig.check(0.0, None, _AccelActor(2.0)) is False
        assert trig.check(0.0, None, _AccelActor(4.0)) is True

    def test_traveled_distance(self) -> None:
        trig = TraveledDistanceTrigger(100.0, comparator=">=")
        assert trig.check(0.0, None, _MetricActor(traveled_distance_m=99.0)) is False
        assert trig.check(0.0, None, _MetricActor(traveled_distance_m=100.0)) is True

    def test_traveled_distance_without_metric_is_false(self) -> None:
        assert TraveledDistanceTrigger(1.0).check(0.0, None, _MetricActor()) is False

    def test_offroad(self) -> None:
        trig = OffroadTrigger(0.5, ">")
        assert trig.check(0.0, None, _MetricActor(offroad=False)) is False
        assert trig.check(0.0, None, _MetricActor(offroad=True)) is True

    def test_time_headway(self) -> None:
        trig = TimeHeadwayTrigger(1.0, "<")
        assert trig.check(0.0, None, _MetricActor(time_headway_s=1.5)) is False
        assert trig.check(0.0, None, _MetricActor(time_headway_s=0.5)) is True

    def test_time_to_collision(self) -> None:
        trig = TimeToCollisionTrigger(2.0, "<")
        assert trig.check(0.0, None, _MetricActor(time_to_collision_s=3.0)) is False
        assert trig.check(0.0, None, _MetricActor(time_to_collision_s=1.0)) is True

    def test_velocity_lt_for_standstill(self) -> None:
        trig = VelocityTrigger(0.1, comparator="<")
        assert trig.check(0.0, None, _Actor(_Vec(0.0, 0.0), vel=_Vec(0.05, 0.0))) is True
        assert trig.check(0.0, None, _Actor(_Vec(0.0, 0.0), vel=_Vec(1.0, 0.0))) is False

    def test_factory_extended_types(self) -> None:
        assert isinstance(
            create_trigger_from_config({"type": "acceleration", "value": 3.0}),
            AccelerationTrigger,
        )
        assert isinstance(
            create_trigger_from_config({"type": "traveled_distance", "value": 50.0}),
            TraveledDistanceTrigger,
        )
        assert isinstance(
            create_trigger_from_config({"type": "offroad", "value": 0.5}), OffroadTrigger
        )
        assert isinstance(
            create_trigger_from_config({"type": "time_headway", "value": 1.0}),
            TimeHeadwayTrigger,
        )
        assert isinstance(
            create_trigger_from_config({"type": "time_to_collision", "value": 2.0}),
            TimeToCollisionTrigger,
        )
        assert isinstance(
            create_trigger_from_config({"type": "standstill"}), VelocityTrigger
        )
