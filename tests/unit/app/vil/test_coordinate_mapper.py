"""VIL 坐标映射器单元测试（对应 §4.3.2）。"""

from __future__ import annotations

import math

import pytest

from hunter_sim.app.vil.coordinate_mapper import CoordinateMapperImpl
from hunter_sim.app.vil.models import VILCalibration


@pytest.fixture
def cal_zero() -> VILCalibration:
    """标定：原点、朝向 0。"""
    return VILCalibration(x0=0.0, y0=0.0, yaw0=0.0)


@pytest.fixture
def cal_offset() -> VILCalibration:
    """标定：偏移 + 90 度朝向。"""
    return VILCalibration(x0=100.0, y0=250.0, yaw0=math.radians(90.0))


def test_odom_to_map_identity_no_offset(cal_zero: VILCalibration) -> None:
    mapper = CoordinateMapperImpl(cal_zero)
    x_map, y_map, yaw_map = mapper.odom_to_map(3.0, 4.0, math.radians(10.0))
    # yaw0=0 → x_rot=x_odom, y_rot=y_odom, yaw_map=-yaw_odom
    assert x_map == pytest.approx(3.0)
    assert y_map == pytest.approx(4.0)
    assert yaw_map == pytest.approx(-math.radians(10.0))


def test_odom_to_map_with_calibration(cal_offset: VILCalibration) -> None:
    mapper = CoordinateMapperImpl(cal_offset)
    # odom (1, 0) 在 yaw0=90° 下旋转为 map (0, 1)，再加平移 (100, 250)。
    x_map, y_map, yaw_map = mapper.odom_to_map(1.0, 0.0, 0.0)
    assert x_map == pytest.approx(100.0 + 0.0)
    assert y_map == pytest.approx(250.0 + 1.0)
    assert yaw_map == pytest.approx(math.radians(90.0))


def test_odom_to_map_origin_cal(cal_zero: VILCalibration) -> None:
    mapper = CoordinateMapperImpl(cal_zero)
    # 车辆未动：odom=(0,0,0) → map=(0,0,0)
    x_map, y_map, yaw_map = mapper.odom_to_map(0.0, 0.0, 0.0)
    assert (x_map, y_map, yaw_map) == (0.0, 0.0, 0.0)


def test_odom_to_map_boundary_pi(cal_offset: VILCalibration) -> None:
    """yaw0=π/2 时输入 yaw=π 应得到 -π/2 的地图航向（考虑取反）。"""
    mapper = CoordinateMapperImpl(cal_offset)
    _, _, yaw_map = mapper.odom_to_map(0.0, 0.0, math.pi)
    assert yaw_map == pytest.approx(math.radians(90.0) - math.pi)


def test_object_to_map_matches_vehicle_mapping(cal_offset: VILCalibration) -> None:
    """目标点映射的 (x, y) 与 :meth:`odom_to_map` 中的一致。"""
    mapper = CoordinateMapperImpl(cal_offset)
    ox, oy = mapper.object_to_map(2.0, -1.0)
    vx, vy, _ = mapper.odom_to_map(2.0, -1.0, 0.0)
    assert (ox, oy) == (pytest.approx(vx), pytest.approx(vy))


def test_heading_to_map_sign_flip(cal_zero: VILCalibration) -> None:
    mapper = CoordinateMapperImpl(cal_zero)
    assert mapper.heading_to_map(math.radians(30.0)) == pytest.approx(-math.radians(30.0))


def test_calibration_property_returns_immutable(cal_offset: VILCalibration) -> None:
    mapper = CoordinateMapperImpl(cal_offset)
    assert mapper.calibration is cal_offset
