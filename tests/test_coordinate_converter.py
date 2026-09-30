"""坐标转换引擎单元测试（PROMPT-TEST-001 / PROMPT-ENG-002-B）。"""

from __future__ import annotations

import math

import pytest

from hunter_sim.engine.coordinate_converter import (
    CalibrationParams,
    CoordinateTransformer,
)


class TestCoordinateTransformer:
    """CoordinateTransformer 测试。"""

    def _make_transformer(
        self,
        x0: float = 0.0,
        y0: float = 0.0,
        yaw0: float = 0.0,
    ) -> CoordinateTransformer:
        """创建带标定参数的 transformer。"""
        t = CoordinateTransformer.__new__(CoordinateTransformer)
        from hunter_sim.engine.coordinate_converter import CalibrationParams
        t._cal = CalibrationParams(x0=x0, y0=y0, yaw0=yaw0)
        return t

    def test_identity_calibration_origin(self) -> None:
        """标定参数为 0，odom(0,0,0) -> CARLA(0,0,0)。"""
        t = self._make_transformer(x0=0.0, y0=0.0, yaw0=0.0)
        xc, yc, zc, pc, yawc, rc = t.odom_to_carla(0.0, 0.0, 0.0)
        assert xc == pytest.approx(0.0, abs=1e-6)
        assert yc == pytest.approx(0.0, abs=1e-6)
        assert yawc == pytest.approx(0.0, abs=1e-6)

    def test_identity_calibration_x_forward(self) -> None:
        """标定 yaw0=0，odom 前方运动 x=5 -> CARLA x=5（X 东方向）。"""
        t = self._make_transformer(x0=0.0, y0=0.0, yaw0=0.0)
        xc, yc, zc, pc, yawc, rc = t.odom_to_carla(5.0, 0.0, 0.0)
        assert xc == pytest.approx(5.0, abs=1e-4)
        assert yc == pytest.approx(0.0, abs=1e-4)

    def test_y_axis_inversion(self) -> None:
        """CARLA Y 轴方向与 odom Y 轴相反（左手系 vs 右手系）。"""
        t = self._make_transformer(x0=0.0, y0=0.0, yaw0=0.0)
        # odom 向左 y=3 在 CARLA 中 y 应为负
        xc, yc, zc, pc, yawc, rc = t.odom_to_carla(0.0, 3.0, 0.0)
        assert yc < 0  # Y 轴取反

    def test_yaw_inversion(self) -> None:
        """odom 逆时针 yaw=pi/4 对应 CARLA 负方向。"""
        t = self._make_transformer(x0=0.0, y0=0.0, yaw0=0.0)
        xc, yc, zc, pc, yawc, rc = t.odom_to_carla(0.0, 0.0, math.pi / 4)
        assert yawc < 0  # 航向取反

    def test_offset_calibration(self) -> None:
        """有初始偏移标定时：x_carla = x0 + x_odom（yaw0=0）。"""
        t = self._make_transformer(x0=100.0, y0=200.0, yaw0=0.0)
        xc, yc, zc, pc, yawc, rc = t.odom_to_carla(5.0, 3.0, 0.0)
        assert xc == pytest.approx(105.0, abs=1e-4)
        assert yc == pytest.approx(197.0, abs=1e-4)  # y0 - y_odom（Y 取反）

    def test_yaw90_calibration(self) -> None:
        """标定 yaw0=pi/2（车头朝 CARLA 北），odom 向前 x=10 -> CARLA y 方向。"""
        t = self._make_transformer(x0=0.0, y0=0.0, yaw0=math.pi / 2)
        xc, yc, zc, pc, yawc, rc = t.odom_to_carla(10.0, 0.0, 0.0)
        # x_rot = 10*cos(pi/2) - 0*sin(pi/2) = 0
        # y_rot = 10*sin(pi/2) + 0*cos(pi/2) = 10
        # x_carla = 0 + 0 = 0
        # y_carla = 0 - 10 = -10
        assert xc == pytest.approx(0.0, abs=1e-4)
        assert yc == pytest.approx(-10.0, abs=1e-4)

    def test_roundtrip_yaw_range(self) -> None:
        """转换后 yaw 在 [-pi, pi] 范围内。"""
        t = self._make_transformer(x0=0.0, y0=0.0, yaw0=0.0)
        for yaw_odom in [-math.pi, -math.pi / 2, 0, math.pi / 2, math.pi]:
            xc, yc, zc, pc, yawc, rc = t.odom_to_carla(1.0, 0.0, yaw_odom)
            assert -math.pi - 0.01 <= yawc <= math.pi + 0.01, f"yaw_out={yawc} for yaw_odom={yaw_odom}"


class TestInverseAndDegAPIs:
    """逆变换、度制接口与标定管理测试。"""

    def _tf(self, x0=0.0, y0=0.0, yaw0=0.0, z_source="map", fixed_z=0.0) -> CoordinateTransformer:
        return CoordinateTransformer(
            CalibrationParams(x0=x0, y0=y0, yaw0=yaw0, z_source=z_source, fixed_z=fixed_z)
        )

    def test_roundtrip_consistency(self) -> None:
        """odom -> CARLA -> odom 应还原。"""
        t = self._tf(x0=50.0, y0=-30.0, yaw0=0.6)
        xc, yc, _zc, _p, yawc, _r = t.odom_to_carla(4.0, 2.0, 0.3)
        x, y, yaw = t.carla_to_odom(xc, yc, yawc)
        assert x == pytest.approx(4.0, abs=1e-3)
        assert y == pytest.approx(2.0, abs=1e-3)
        assert yaw == pytest.approx(0.3, abs=1e-3)

    def test_z_source_fixed(self) -> None:
        t = self._tf(z_source="fixed", fixed_z=3.5)
        _, _, z, _, _, _ = t.odom_to_carla(1.0, 1.0, 0.0)
        assert z == 3.5

    def test_z_source_map_is_zero(self) -> None:
        t = self._tf(z_source="map")
        _, _, z, _, _, _ = t.odom_to_carla(1.0, 1.0, 0.0)
        assert z == 0.0

    def test_deg_api_yaw(self) -> None:
        t = self._tf()
        xc, yc, zc, p, yaw_deg, r = t.odom_to_carla_deg(0.0, 0.0, 90.0)
        assert yaw_deg == pytest.approx(-90.0, abs=1e-3)

    def test_carla_to_odom_deg_roundtrip(self) -> None:
        t = self._tf()
        xo, yo, yaw_deg = t.carla_to_odom_deg(5.0, -3.0, 45.0)
        assert isinstance(yaw_deg, float)

    def test_update_calibration(self) -> None:
        t = self._tf(x0=0.0)
        t.update_calibration(CalibrationParams(x0=100.0, y0=100.0, yaw0=0.0))
        assert t.calibration.x0 == 100.0

    def test_pitch_roll_passthrough(self) -> None:
        t = self._tf()
        _, _, _, p, _, r = t.odom_to_carla(0.0, 0.0, 0.0, pitch_odom=0.2, roll_odom=0.1)
        assert p == pytest.approx(0.2)
        assert r == pytest.approx(0.1)
