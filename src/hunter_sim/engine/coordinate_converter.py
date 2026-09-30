"""坐标系转换工具（PROMPT-ENG-001-B / PROMPT-ENG-002-B）。

实现 CARLA 左手坐标系与实车右手坐标系之间的精确转换。

坐标系定义：
- CARLA 地图坐标（左手系）：X 东，Y 北，Z 上；单位米；航向角 yaw 逆时针为正（CW→CCW 取反）
- 实车 odom 坐标（右手系）：X 前，Y 左，Z 上；原点在车辆启动点

转换公式：
    x_rot = x_odom * cos(yaw0) - y_odom * sin(yaw0)
    y_rot = x_odom * sin(yaw0) + y_odom * cos(yaw0)
    x_map = x0 + x_rot
    y_map = y0 + y_rot   （CARLA Y 轴取反：y_carla = y0 - y_rot）
    yaw_carla = -(yaw0 + yaw_odom)  （左手系 yaw 方向与右手系相反）

所有内部计算统一使用弧度制，API 接口出入使用度制。
"""

from __future__ import annotations

import math
from typing import Tuple

from pydantic import BaseModel, Field

from hunter_sim.common.utils import get_logger, normalize_angle_rad

logger = get_logger(__name__)


class CalibrationParams(BaseModel):
    """实车到仿真地图的初始标定参数。

    Attributes:
        x0: 实车启动点在 CARLA 地图坐标系中的 X 坐标（米）。
        y0: 实车启动点在 CARLA 地图坐标系中的 Y 坐标（米）。
        yaw0: 实车启动时车头朝向在 CARLA 地图坐标系中的航向角（弧度）。
        z_source: 高度来源，"map" 表示由地图路面决定。
    """

    x0: float = Field(0.0, description="初始 X 偏移 (CARLA 地图坐标，米)")
    y0: float = Field(0.0, description="初始 Y 偏移 (CARLA 地图坐标，米)")
    yaw0: float = Field(0.0, description="初始航向角 (弧度)")
    z_source: str = Field("map", pattern="^(map|fixed)$", description="高度来源")
    fixed_z: float = Field(0.0, description="固定高度（仅 z_source='fixed' 时有效）")


class CoordinateTransformer:
    """实车 odom 坐标到 CARLA 地图坐标的双向转换器。

    使用初始标定参数（x0, y0, yaw0）进行坐标系统转换。
    所有方法内部使用弧度，API 层通过 _deg_ 后缀方法转换。

    Args:
        calibration: 标定参数。
    """

    def __init__(self, calibration: CalibrationParams) -> None:
        self._cal = calibration
        logger.debug(
            f"CoordinateTransformer initialized: x0={calibration.x0}, "
            f"y0={calibration.y0}, yaw0={calibration.yaw0:.4f} rad"
        )

    @property
    def calibration(self) -> CalibrationParams:
        """返回当前标定参数（只读）。"""
        return self._cal

    def update_calibration(self, calibration: CalibrationParams) -> None:
        """更新标定参数。

        Args:
            calibration: 新的标定参数。
        """
        self._cal = calibration
        logger.info("Calibration parameters updated")

    def odom_to_carla(
        self,
        x_odom: float,
        y_odom: float,
        yaw_odom: float,
        pitch_odom: float = 0.0,
        roll_odom: float = 0.0,
    ) -> Tuple[float, float, float, float, float, float]:
        """实车 odom → CARLA 地图坐标转换（内部弧度制）。

        Args:
            x_odom: 实车 X（前进方向）坐标（米）。
            y_odom: 实车 Y（左侧方向）坐标（米）。
            yaw_odom: 实车航向角（弧度，相对起点车头方向逆时针为正）。
            pitch_odom: 俯仰角（弧度，来自 IMU）。
            roll_odom: 横滚角（弧度，来自 IMU）。

        Returns:
            (x_carla, y_carla, z_carla, pitch_carla, yaw_carla, roll_carla) 元组。
            z_carla 由标定 z_source 决定，实际路面高度由调用方通过 MapHeightGetter 补充。
        """
        cos_y0 = math.cos(self._cal.yaw0)
        sin_y0 = math.sin(self._cal.yaw0)

        # 旋转（odom → map frame）
        x_rot = x_odom * cos_y0 - y_odom * sin_y0
        y_rot = x_odom * sin_y0 + y_odom * cos_y0

        # 平移 + CARLA 左手系 Y 轴翻转（实车 Y 左 = CARLA -Y）
        x_carla = self._cal.x0 + x_rot
        y_carla = self._cal.y0 - y_rot  # 左手系 Y 取反

        # 高度
        z_carla = self._cal.fixed_z if self._cal.z_source == "fixed" else 0.0

        # 航向：实车逆时针为正，CARLA 顺时针为正 → 取反
        yaw_carla = normalize_angle_rad(-(self._cal.yaw0 + yaw_odom))

        # 俯仰/横滚（IMU 数据，CARLA 与实车定义一致）
        pitch_carla = pitch_odom
        roll_carla = roll_odom

        return x_carla, y_carla, z_carla, pitch_carla, yaw_carla, roll_carla

    def carla_to_odom(
        self,
        x_carla: float,
        y_carla: float,
        yaw_carla: float,
    ) -> Tuple[float, float, float]:
        """CARLA 地图坐标 → 实车 odom 坐标逆变换（内部弧度制）。

        Args:
            x_carla: CARLA X 坐标（米）。
            y_carla: CARLA Y 坐标（米）。
            yaw_carla: CARLA 航向角（弧度）。

        Returns:
            (x_odom, y_odom, yaw_odom) 元组。
        """
        # 去平移，恢复 CARLA Y 取反
        dx = x_carla - self._cal.x0
        dy = -(y_carla - self._cal.y0)  # 还原 Y 翻转

        cos_y0 = math.cos(-self._cal.yaw0)
        sin_y0 = math.sin(-self._cal.yaw0)

        # 反旋转
        x_odom = dx * cos_y0 - dy * sin_y0
        y_odom = dx * sin_y0 + dy * cos_y0

        # 反航向
        yaw_odom = normalize_angle_rad(-yaw_carla - self._cal.yaw0)

        return x_odom, y_odom, yaw_odom

    def odom_to_carla_deg(
        self,
        x_odom: float,
        y_odom: float,
        yaw_odom_deg: float,
        pitch_deg: float = 0.0,
        roll_deg: float = 0.0,
    ) -> Tuple[float, float, float, float, float, float]:
        """实车 odom → CARLA（度制接口，供 API 层使用）。

        Args:
            x_odom: 实车 X（米）。
            y_odom: 实车 Y（米）。
            yaw_odom_deg: 实车航向角（度）。
            pitch_deg: 俯仰角（度）。
            roll_deg: 横滚角（度）。

        Returns:
            (x_carla, y_carla, z_carla, pitch_deg_carla, yaw_deg_carla, roll_deg_carla)。
        """
        yaw_rad = math.radians(yaw_odom_deg)
        pitch_rad = math.radians(pitch_deg)
        roll_rad = math.radians(roll_deg)

        x, y, z, p, yaw, r = self.odom_to_carla(x_odom, y_odom, yaw_rad, pitch_rad, roll_rad)
        return x, y, z, math.degrees(p), math.degrees(yaw), math.degrees(r)

    def carla_to_odom_deg(
        self,
        x_carla: float,
        y_carla: float,
        yaw_carla_deg: float,
    ) -> Tuple[float, float, float]:
        """CARLA → 实车 odom（度制接口，供 API 层使用）。

        Args:
            x_carla: CARLA X 坐标（米）。
            y_carla: CARLA Y 坐标（米）。
            yaw_carla_deg: CARLA 航向角（度）。

        Returns:
            (x_odom, y_odom, yaw_odom_deg)。
        """
        yaw_rad = math.radians(yaw_carla_deg)
        x, y, yaw = self.carla_to_odom(x_carla, y_carla, yaw_rad)
        return x, y, math.degrees(yaw)


class MapHeightGetterProtocol:
    """地图路面高度获取器协议（供 VIL 模块调用 CARLA API 获取 Z 值）。"""

    def get_ground_z(self, x: float, y: float) -> float:
        """返回地图坐标 (x, y) 处的路面 Z 坐标（米）。

        Args:
            x: CARLA 地图 X 坐标。
            y: CARLA 地图 Y 坐标。

        Returns:
            路面 Z 坐标（米）。
        """
        raise NotImplementedError
