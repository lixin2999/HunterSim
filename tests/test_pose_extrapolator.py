"""位姿外推器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import math

from hunter_sim.common.models import Transform, VehicleState
from hunter_sim.vil_mapper.pose_extrapolator import PoseExtrapolator


def _make_state(
    x: float = 0.0,
    y: float = 0.0,
    yaw: float = 0.0,
    vx: float = 0.0,
    vy: float = 0.0,
    ax: float = 0.0,
    ay: float = 0.0,
    wz: float = 0.0,
    speed: float = 0.0,
) -> VehicleState:
    return VehicleState(
        time_stamp=0.0,
        transform=Transform(x=x, y=y, z=0.0, pitch=0.0, yaw=yaw, roll=0.0),
        velocity=(vx, vy, 0.0),
        acceleration=(ax, ay, 0.0),
        angular_velocity=(0.0, 0.0, wz),
        steering=0.0,
        throttle=0.0,
        brake=0.0,
        gear=1,
        vehicle_speed=speed,
    )


class TestPoseExtrapolatorConfig:
    def test_default_dt(self) -> None:
        assert PoseExtrapolator().extrapolation_dt == pytest_approx(0.15)

    def test_set_clamps_high(self) -> None:
        e = PoseExtrapolator()
        e.set_extrapolation_ms(5000)
        assert e.extrapolation_dt == pytest_approx(1.0)

    def test_set_clamps_low(self) -> None:
        e = PoseExtrapolator()
        e.set_extrapolation_ms(-100)
        assert e.extrapolation_dt == 0.0


def pytest_approx(v: float) -> float:
    import pytest

    return pytest.approx(v, rel=1e-6)


class TestExtrapolate:
    def test_zero_dt_returns_copy(self) -> None:
        e = PoseExtrapolator(extrapolation_ms=0)
        s = _make_state(x=1.0, vx=2.0)
        out = e.extrapolate(s)
        assert out.transform.x == 1.0
        assert out.velocity == (2.0, 0.0, 0.0)

    def test_straight_line_motion(self) -> None:
        e = PoseExtrapolator(extrapolation_ms=100)  # dt=0.1
        s = _make_state(x=0.0, yaw=0.0, vx=10.0, speed=10.0)
        out = e.extrapolate(s)
        # x_new = 0 + (10 + 0*0.1)*0.1 = 1.0
        assert out.transform.x == pytest_approx(1.0)
        assert out.transform.y == pytest_approx(0.0)

    def test_acceleration_increases_speed(self) -> None:
        e = PoseExtrapolator(extrapolation_ms=100)
        s = _make_state(vx=10.0, ax=5.0, speed=10.0)
        out = e.extrapolate(s)
        assert out.velocity[0] == pytest_approx(10.5)

    def test_yaw_extrapolation(self) -> None:
        e = PoseExtrapolator(extrapolation_ms=100)
        s = _make_state(yaw=0.0, wz=0.2)
        out = e.extrapolate(s)
        assert out.transform.yaw == pytest_approx(0.02)

    def test_original_state_unchanged(self) -> None:
        e = PoseExtrapolator(extrapolation_ms=100)
        s = _make_state(x=0.0, vx=10.0)
        e.extrapolate(s)
        assert s.transform.x == 0.0


class TestExtrapolateTransform:
    def test_zero_dt_returns_copy(self) -> None:
        e = PoseExtrapolator(extrapolation_ms=0)
        t = Transform(x=5.0, y=5.0, z=1.0, pitch=0.1, yaw=0.2, roll=0.0)
        out = e.extrapolate_transform(t, speed_ms=10.0, yaw_rate_rad_s=0.5)
        assert out.x == 5.0
        assert out.yaw == 0.2

    def test_forward_motion(self) -> None:
        e = PoseExtrapolator(extrapolation_ms=100)
        t = Transform(x=0.0, y=0.0, z=0.0, pitch=0.0, yaw=0.0, roll=0.0)
        out = e.extrapolate_transform(t, speed_ms=10.0, yaw_rate_rad_s=0.0)
        assert out.x == pytest_approx(1.0)
        assert out.y == pytest_approx(0.0)
        assert out.z == 0.0  # 高度不外推

    def test_yaw_rotation(self) -> None:
        e = PoseExtrapolator(extrapolation_ms=100)
        t = Transform(x=0.0, y=0.0, z=0.0, pitch=0.0, yaw=0.0, roll=0.0)
        out = e.extrapolate_transform(t, speed_ms=0.0, yaw_rate_rad_s=math.pi)
        assert out.yaw == pytest_approx(0.1 * math.pi)
