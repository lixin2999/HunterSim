"""位姿外推器（PROMPT-ENG-002-C）。

基于车辆速度和角速度，将上一帧位姿外推到当前时刻，
补偿数据传输延迟和处理延迟。仅用于可视化，不影响仿真物理。

外推模型：匀速圆周运动（CVUT）近似
    x_new = x + v * cos(yaw) * dt
    y_new = y + v * sin(yaw) * dt
    yaw_new = yaw + omega * dt
    v_new = v + a * dt（线性速度外推）
"""

from __future__ import annotations

import math
from typing import Tuple

from hunter_sim.common.models import Transform, VehicleState
from hunter_sim.common.utils import get_logger, normalize_angle_rad

logger = get_logger(__name__)


class PoseExtrapolator:
    """VIL 位姿延迟补偿外推器。

    Args:
        extrapolation_ms: 外推时间量（毫秒），默认 150ms，可配置。
    """

    def __init__(self, extrapolation_ms: int = 150) -> None:
        self._extrapolation_dt: float = extrapolation_ms / 1000.0
        logger.debug(f"PoseExtrapolator: extrapolation_dt={self._extrapolation_dt*1000:.0f}ms")

    @property
    def extrapolation_dt(self) -> float:
        """当前外推时间量（秒）。"""
        return self._extrapolation_dt

    def set_extrapolation_ms(self, ms: int) -> None:
        """动态更新外推时间量。

        Args:
            ms: 外推时间（毫秒），范围 [0, 1000]。
        """
        ms = max(0, min(1000, ms))
        self._extrapolation_dt = ms / 1000.0
        logger.info(f"Extrapolation time updated: {ms}ms")

    def extrapolate(self, state: VehicleState) -> VehicleState:
        """对车辆状态进行位姿外推。

        使用当前帧的速度、角速度、加速度将位姿向前推进 extrapolation_dt 秒。
        外推仅影响 transform 和 velocity 字段，其他控制状态字段保持不变。

        Args:
            state: 当前帧车辆状态（odom 坐标，弧度制）。

        Returns:
            外推后的车辆状态（新对象，原对象不变）。
        """
        if self._extrapolation_dt <= 0.0:
            return state.model_copy()

        dt = self._extrapolation_dt
        vx, vy, _ = state.velocity
        wx, wy, wz = state.angular_velocity
        ax, ay, _ = state.acceleration
        yaw = state.transform.yaw
        speed = state.vehicle_speed

        # 速度外推
        vx_new = vx + ax * dt
        vy_new = vy + ay * dt

        # 位姿外推（匀速圆周近似）
        # 在全局坐标系下使用当前航向角
        x_new = state.transform.x + vx_new * dt
        y_new = state.transform.y + vy_new * dt
        z_new = state.transform.z  # 高度不外推

        # 航向外推（使用 yaw 角速度，即 z 轴角速度）
        yaw_new = normalize_angle_rad(yaw + wz * dt)

        # 俯仰/横滚不外推（IMU 实时值）
        extrapolated_transform = Transform(
            x=x_new,
            y=y_new,
            z=z_new,
            pitch=state.transform.pitch,
            yaw=yaw_new,
            roll=state.transform.roll,
        )

        speed_new = math.sqrt(vx_new ** 2 + vy_new ** 2)

        result = state.model_copy(deep=False)
        result.transform = extrapolated_transform
        result.velocity = (vx_new, vy_new, 0.0)
        result.vehicle_speed = speed_new
        return result

    def extrapolate_transform(
        self,
        transform: Transform,
        speed_ms: float,
        yaw_rate_rad_s: float,
    ) -> Transform:
        """简化的位姿外推（仅使用速度和角速度标量，匀速圆周模型）。

        Args:
            transform: 当前位姿（弧度制）。
            speed_ms: 车速（m/s）。
            yaw_rate_rad_s: 偏航角速度（rad/s）。

        Returns:
            外推后的 Transform。
        """
        dt = self._extrapolation_dt
        if dt <= 0.0:
            return transform.model_copy()

        yaw = transform.yaw
        # 平均航向用于位移估算
        yaw_mid = yaw + yaw_rate_rad_s * dt * 0.5

        dx = speed_ms * math.cos(yaw_mid) * dt
        dy = speed_ms * math.sin(yaw_mid) * dt
        new_yaw = normalize_angle_rad(yaw + yaw_rate_rad_s * dt)

        return Transform(
            x=transform.x + dx,
            y=transform.y + dy,
            z=transform.z,
            pitch=transform.pitch,
            yaw=new_yaw,
            roll=transform.roll,
        )
