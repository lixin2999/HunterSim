"""VIL 同步控制器单元测试（对应 §4.5）。"""

from __future__ import annotations

import math

import pytest

from hunter_sim.app.vil.models import (
    ChassisData,
    LocalizationData,
    PerceptionData,
    VehicleTelemetry,
    VILCalibration,
    VILConfig,
)
from hunter_sim.app.vil.sync_controller import SyncControllerImpl


@pytest.fixture
def vil_config() -> VILConfig:
    return VILConfig(
        target_vehicle_id="hunter-001",
        calibration=VILCalibration(x0=0.0, y0=0.0, yaw0=0.0),
        delay_compensation_ms=150.0,
        data_timeout_ms=500.0,
        extrapolation_threshold_ms=50.0,
    )


def _make_telemetry(
    *,
    x: float = 0.0,
    y: float = 0.0,
    heading: float = 0.0,
    velocity: float = 10.0,
    angular_velocity: float = 0.0,
    ts: float = 1_700_000_000.0,
) -> VehicleTelemetry:
    return VehicleTelemetry(
        vehicle_id="hunter-001",
        timestamp=ts,
        localization=LocalizationData(x=x, y=y, heading=heading),
        chassis=ChassisData(
            velocity=velocity,
            steering=0.0,
            throttle=0.5,
            brake=0.0,
            angular_velocity=angular_velocity,
        ),
        perception=PerceptionData(),
    )


def test_should_pause_boundary(vil_config: VILConfig) -> None:
    ctrl = SyncControllerImpl(vil_config)
    assert ctrl.should_pause(500.0) is False
    assert ctrl.should_pause(501.0) is True


def test_should_extrapolate_boundary(vil_config: VILConfig) -> None:
    ctrl = SyncControllerImpl(vil_config)
    assert ctrl.should_extrapolate(50.0) is False
    assert ctrl.should_extrapolate(51.0) is True


def test_compensate_zero_elapsed_returns_same_object(vil_config: VILConfig) -> None:
    ctrl = SyncControllerImpl(vil_config)
    tel = _make_telemetry()
    assert ctrl.compensate(tel, 0.0) is tel


def test_compensate_linear_motion(vil_config: VILConfig) -> None:
    ctrl = SyncControllerImpl(vil_config)
    tel = _make_telemetry(x=0.0, y=0.0, heading=0.0, velocity=10.0)
    out = ctrl.compensate(tel, 0.1)
    assert out.localization.x == pytest.approx(1.0)
    assert out.localization.y == pytest.approx(0.0)
    assert out.localization.heading == pytest.approx(0.0)


def test_compensate_with_angular_velocity(vil_config: VILConfig) -> None:
    ctrl = SyncControllerImpl(vil_config)
    tel = _make_telemetry(
        x=0.0, y=0.0, heading=0.0, velocity=10.0, angular_velocity=math.radians(30.0)
    )
    out = ctrl.compensate(tel, 0.1)
    expected_heading = math.radians(3.0)
    assert out.localization.heading == pytest.approx(expected_heading)
    # 位移量按外推后的 heading 分解。
    assert out.localization.x == pytest.approx(10.0 * math.cos(expected_heading) * 0.1)
    assert out.localization.y == pytest.approx(10.0 * math.sin(expected_heading) * 0.1)


def test_compensate_original_immutability(vil_config: VILConfig) -> None:
    """外推不应改变原始遥测对象（frozen 保证 + 显式验证）。"""
    ctrl = SyncControllerImpl(vil_config)
    tel = _make_telemetry(x=5.0, y=5.0)
    _ = ctrl.compensate(tel, 0.5)
    assert tel.localization.x == 5.0
    assert tel.localization.y == 5.0
