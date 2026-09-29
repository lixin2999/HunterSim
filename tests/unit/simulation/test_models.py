"""simulation.models 数据对象单元测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from hunter_sim.simulation.models import Location, Rotation, Transform, VehicleCommand


def test_location_distance() -> None:
    a = Location(0.0, 0.0, 0.0)
    b = Location(3.0, 4.0, 0.0)
    assert a.distance_to(b) == pytest.approx(5.0)


def test_transform_defaults() -> None:
    tf = Transform()
    assert tf.location == Location(0.0, 0.0, 0.0)
    assert tf.rotation == Rotation(0.0, 0.0, 0.0)


def test_models_are_immutable() -> None:
    loc = Location(1.0, 2.0, 3.0)
    with pytest.raises(FrozenInstanceError):
        loc.x = 9.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("throttle", "brake", "steer", "gear"),
    [
        (1.5, 0.0, 0.0, 1),
        (-0.1, 0.0, 0.0, 1),
        (0.0, 2.0, 0.0, 1),
        (0.0, 0.0, 1.5, 1),
        (0.0, 0.0, 0.0, -1),
    ],
)
def test_vehicle_command_bounds_rejected(
    throttle: float, brake: float, steer: float, gear: int
) -> None:
    with pytest.raises(ValueError):
        VehicleCommand(throttle=throttle, brake=brake, steer=steer, gear=gear)


def test_vehicle_command_valid_defaults() -> None:
    cmd = VehicleCommand()
    assert cmd.throttle == 0.0 and cmd.gear == 1 and cmd.hand_brake is False
