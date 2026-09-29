"""L1 仿真核心层的轻量几何 / 控制数据对象。

这些类型用于隔离 CARLA 原生对象，使 Protocol 契约不暴露 `carla.*` 类型，便于
测试注入与跨层传递。均为不可变 ``@dataclass(slots=True, frozen=True)``。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Location:
    """世界坐标位置（米）。"""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def distance_to(self, other: Location) -> float:
        """到另一点的欧氏距离。"""
        return math.sqrt(
            (self.x - other.x) ** 2 + (self.y - other.y) ** 2 + (self.z - other.z) ** 2
        )


@dataclass(frozen=True, slots=True)
class Rotation:
    """欧拉角（度）：pitch 俯仰 / yaw 航向 / roll 翻滚。"""

    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0


@dataclass(frozen=True, slots=True)
class Transform:
    """位姿：位置 + 朝向。"""

    location: Location = Location()
    rotation: Rotation = Rotation()


class ControlMode(StrEnum):
    """车辆控制模式（开发提示词 §3.3.2）。

    Attributes:
        VIL: 实车在环（Vehicle-in-the-Loop）。直接位姿控制：外部提供实车真实
            位姿与速度，虚拟车辆逐帧跟随，不依赖仿真物理引擎。
        SIL: 仿真在环（Simulation-in-the-Loop）。控制指令控制：自动驾驶算法输出
            油门/刹车/转向等指令，由仿真物理引擎积分计算车辆运动。
    """

    VIL = "vil"
    SIL = "sil"


@dataclass(frozen=True, slots=True)
class Vector3D:
    """三维向量（线速度 m/s 或角速度 rad/s）。"""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


@dataclass(frozen=True, slots=True)
class VehicleKinematicState:
    """VIL 模式下的运动学状态：位姿 + 线速度 + 角速度。

    用于实车在环时直接将真实车辆状态注入虚拟车辆，绕过仿真物理积分。

    Attributes:
        transform: 目标位姿（位置 + 朝向）。
        velocity: 线速度 (x, y, z)，单位 m/s（世界坐标系）。
        angular_velocity: 角速度 (x, y, z)，单位 rad/s。
    """

    transform: Transform = Transform()
    velocity: Vector3D = Vector3D()
    angular_velocity: Vector3D = Vector3D()


@dataclass(frozen=True, slots=True)
class VehicleCommand:
    """高层控制指令，映射到 ``carla.VehicleControl`` / ``GearControl``。

    Attributes:
        throttle: 油门 [0, 1]。
        brake: 刹车 [0, 1]。
        steer: 转向 [-1, 1]（右转为正）。
        hand_brake: 手刹。
        reverse: 倒车。
        gear: 目标档位（>=0）。
    """

    throttle: float = 0.0
    brake: float = 0.0
    steer: float = 0.0
    hand_brake: bool = False
    reverse: bool = False
    gear: int = 1

    def __post_init__(self) -> None:
        """校验控制指令取值范围。"""
        if not 0.0 <= self.throttle <= 1.0:
            raise ValueError(f"throttle 越界: {self.throttle}")
        if not 0.0 <= self.brake <= 1.0:
            raise ValueError(f"brake 越界: {self.brake}")
        if not -1.0 <= self.steer <= 1.0:
            raise ValueError(f"steer 越界: {self.steer}")
        if self.gear < 0:
            raise ValueError(f"gear 不能为负: {self.gear}")


__all__ = [
    "ControlMode",
    "Location",
    "Rotation",
    "Transform",
    "Vector3D",
    "VehicleCommand",
    "VehicleKinematicState",
]
