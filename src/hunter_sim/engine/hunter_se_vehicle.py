"""HUNTER SE 车辆模型模块（PROMPT-ENG-001-C）。

定义 HUNTER SE 底盘车的物理参数、蓝图生成、VIL/SIL 双模式控制器。

HUNTER SE 规格：
- 尺寸 820×640×310 mm，质量 60 kg，轴距 0.46 m，轮距 0.54 m
- 最大速度 4.8 m/s，最大转向角 0.4 rad，后轮驱动，前轮阿克曼转向
- 质心高度 0.15 m
- 传感器安装：LiDAR 车顶中心 (0.23, 0, 0.35)，相机车顶前向 (0.40, 0, 0.30)
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Optional, Protocol

from pydantic import BaseModel, Field, field_validator

from hunter_sim.common.exceptions import CarlaSimulationError, ValidationError
from hunter_sim.common.models import (
    HunterSESpec,
    Transform,
    VehicleControlCommand,
    VehicleState,
)
from hunter_sim.common.utils import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


# ─── 车辆物理参数 ─────────────────────────────────────────────────────────────


class HunterSEParameters(BaseModel):
    """HUNTER SE 车辆物理参数配置（对应 CARLA VehiclePhysicalControls）。

    Attributes:
        mass_kg: 整车质量（kg）。
        wheelbase_m: 轴距（m）。
        track_width_m: 轮距（m）。
        max_speed_ms: 最大速度（m/s）。
        max_steer_rad: 最大前轮转向角（rad）。
        cg_height_m: 质心高度（m）。
        engine_max_torque_nm: 发动机最大扭矩（N·m）。
        drag_coefficient: 空气阻力系数。
        front_wheel_radius_m: 前轮半径（m）。
        rear_wheel_radius_m: 后轮半径（m）。
    """

    mass_kg: float = Field(HunterSESpec.MASS_KG, gt=0.0)
    wheelbase_m: float = Field(HunterSESpec.WHEELBASE_M, gt=0.0)
    track_width_m: float = Field(HunterSESpec.TRACK_WIDTH_M, gt=0.0)
    max_speed_ms: float = Field(HunterSESpec.MAX_SPEED_MS, gt=0.0)
    max_steer_rad: float = Field(HunterSESpec.MAX_STEER_RAD, gt=0.0, le=1.0)
    cg_height_m: float = Field(HunterSESpec.CG_HEIGHT_M, gt=0.0)
    engine_max_torque_nm: float = Field(80.0, gt=0.0)
    drag_coefficient: float = Field(0.35, gt=0.0, le=2.0)
    front_wheel_radius_m: float = Field(0.10, gt=0.0)
    rear_wheel_radius_m: float = Field(0.10, gt=0.0)

    @field_validator("max_steer_rad")
    @classmethod
    def validate_max_steer(cls, v: float) -> float:
        """验证最大转向角合理性。"""
        if v > math.pi / 4:
            raise ValueError(f"max_steer_rad {v} exceeds safe limit (pi/4)")
        return v


# ─── 蓝图库协议 ───────────────────────────────────────────────────────────────


class BlueprintLibraryProtocol(Protocol):
    """CARLA BlueprintLibrary 协议。"""

    def filter(self, wildcard: str) -> list[Any]: ...
    def find(self, name: str) -> Optional[Any]: ...


class CarlaVehicleActorProtocol(Protocol):
    """CARLA Vehicle Actor 协议。"""

    def set_transform(self, transform: Any) -> None: ...
    def set_velocity(self, vector: Any) -> None: ...
    def set_angular_velocity(self, vector: Any) -> None: ...
    def apply_control(self, control: Any) -> None: ...
    def get_velocity(self) -> Any: ...
    def get_location(self) -> Any: ...
    def get_rotation(self) -> Any: ...
    def destroy(self) -> None: ...


# ─── 车辆控制器基类 ──────────────────────────────────────────────────────────


class VehicleControllerProtocol(Protocol):
    """车辆控制器协议（VIL/SIL 双模式公共接口）。"""

    def apply_state(self, state: VehicleState) -> None:
        """将车辆状态应用到仿真 Actor。"""
        ...

    def get_state(self) -> VehicleState:
        """从仿真 Actor 读取当前车辆状态。"""
        ...

    def destroy(self) -> None:
        """销毁车辆 Actor，释放资源。"""
        ...


class HunterSEVehicleController:
    """VIL 模式车辆控制器。

    VIL 模式下直接通过 set_transform() 设置车辆位姿，绕过物理引擎计算，
    保证虚拟车辆与实车位姿严格同步。

    Args:
        vehicle_actor: CARLA Vehicle Actor 引用。
        params: HUNTER SE 物理参数。
    """

    def __init__(
        self,
        vehicle_actor: CarlaVehicleActorProtocol,
        params: Optional[HunterSEParameters] = None,
    ) -> None:
        self._actor: CarlaVehicleActorProtocol = vehicle_actor
        self._params: HunterSEParameters = params or HunterSEParameters()
        self._destroyed: bool = False
        logger.debug("VIL mode controller created (direct pose control)")

    def apply_transform(self, transform: Transform) -> None:
        """直接设置车辆位姿（VIL 模式专用，不使用物理仿真）。

        Args:
            transform: 目标位姿（弧度制）。

        Raises:
            CarlaSimulationError: Actor 已销毁或 CARLA 调用失败。
        """
        self._check_alive()
        try:
            carla_tf = _make_carla_transform(transform)
            self._actor.set_transform(carla_tf)
        except Exception as exc:
            raise CarlaSimulationError("apply_transform", str(exc)) from exc

    def apply_velocity(self, vx: float, vy: float, vz: float) -> None:
        """设置车辆速度向量（VIL 模式下同步实车速度，设计文档 §4.4.1）。

        Args:
            vx: X 方向速度（m/s）。
            vy: Y 方向速度（m/s）。
            vz: Z 方向速度（m/s）。

        Raises:
            CarlaSimulationError: Actor 已销毁或 CARLA 调用失败。
        """
        self._check_alive()
        speed = math.sqrt(vx * vx + vy * vy)
        if speed > self._params.max_speed_ms * 1.1:
            logger.warning(f"Speed {speed:.2f} m/s exceeds HUNTER SE max {self._params.max_speed_ms} m/s")
        try:
            vec = _make_carla_vector3d(vx, vy, vz)
            self._actor.set_velocity(vec)
        except Exception as exc:
            raise CarlaSimulationError("apply_velocity", str(exc)) from exc

    def apply_angular_velocity(self, wx: float, wy: float, wz: float) -> None:
        """设置车辆角速度（设计文档 §3.3.2 直接位姿控制）。

        Args:
            wx: X 方向角速度（rad/s）。
            wy: Y 方向角速度（rad/s）。
            wz: Z 方向角速度（rad/s）。

        Raises:
            CarlaSimulationError: Actor 已销毁或 CARLA 调用失败。
        """
        self._check_alive()
        try:
            vec = _make_carla_vector3d(wx, wy, wz)
            self._actor.set_angular_velocity(vec)
        except Exception as exc:
            raise CarlaSimulationError("apply_angular_velocity", str(exc)) from exc

    def get_state(self) -> VehicleState:
        """从仿真 Actor 读取当前车辆状态。

        Returns:
            VehicleState 对象（弧度制）。

        Raises:
            CarlaSimulationError: Actor 已销毁或读取失败。
        """
        self._check_alive()
        try:
            loc = self._actor.get_location()
            rot = self._actor.get_rotation()
            vel = self._actor.get_velocity()
            speed = math.sqrt(vel.x ** 2 + vel.y ** 2 + vel.z ** 2)
            return VehicleState(
                time_stamp=_get_carla_time(),
                transform=Transform(
                    x=float(loc.x), y=float(loc.y), z=float(loc.z),
                    pitch=math.radians(float(rot.pitch)),
                    yaw=math.radians(float(rot.yaw)),
                    roll=math.radians(float(rot.roll)),
                ),
                velocity=(float(vel.x), float(vel.y), float(vel.z)),
                vehicle_speed=speed,
            )
        except Exception as exc:
            raise CarlaSimulationError("get_state", str(exc)) from exc

    def destroy(self) -> None:
        """销毁车辆 Actor。"""
        if not self._destroyed:
            self._actor.destroy()
            self._destroyed = True
            logger.info("HUNTER SE vehicle actor destroyed")

    def _check_alive(self) -> None:
        if self._destroyed:
            raise CarlaSimulationError("vehicle_ctrl", "Vehicle actor already destroyed")


class HunterSESILController:
    """SIL 模式车辆控制器（控制指令驱动，物理引擎计算运动）。

    通过 apply_control(VehicleControl) 驱动车辆，由 CARLA PhysX 引擎计算实际运动。

    Args:
        vehicle_actor: CARLA Vehicle Actor 引用。
        params: HUNTER SE 物理参数。
    """

    def __init__(
        self,
        vehicle_actor: CarlaVehicleActorProtocol,
        params: Optional[HunterSEParameters] = None,
    ) -> None:
        self._actor: CarlaVehicleActorProtocol = vehicle_actor
        self._params: HunterSEParameters = params or HunterSEParameters()
        self._destroyed: bool = False
        logger.debug("SIL mode controller created (control-input driven)")

    def apply_command(self, cmd: VehicleControlCommand) -> None:
        """应用控制指令，由物理引擎驱动车辆运动。

        Args:
            cmd: 控制指令（油门/转向/制动）。

        Raises:
            CarlaSimulationError: 应用失败。
        """
        self._check_alive()
        _validate_control_cmd(cmd, self._params)
        try:
            carla_ctrl = _make_carla_vehicle_control(cmd)
            self._actor.apply_control(carla_ctrl)
        except Exception as exc:
            raise CarlaSimulationError("apply_command", str(exc)) from exc

    def get_state(self) -> VehicleState:
        """从仿真 Actor 读取当前状态。"""
        self._check_alive()
        try:
            loc = self._actor.get_location()
            rot = self._actor.get_rotation()
            vel = self._actor.get_velocity()
            speed = math.sqrt(vel.x ** 2 + vel.y ** 2 + vel.z ** 2)
            return VehicleState(
                time_stamp=_get_carla_time(),
                transform=Transform(
                    x=float(loc.x), y=float(loc.y), z=float(loc.z),
                    pitch=math.radians(float(rot.pitch)),
                    yaw=math.radians(float(rot.yaw)),
                    roll=math.radians(float(rot.roll)),
                ),
                velocity=(float(vel.x), float(vel.y), float(vel.z)),
                vehicle_speed=speed,
            )
        except Exception as exc:
            raise CarlaSimulationError("get_state", str(exc)) from exc

    def destroy(self) -> None:
        """销毁车辆 Actor。"""
        if not self._destroyed:
            self._actor.destroy()
            self._destroyed = True
            logger.info("HUNTER SE SIL vehicle destroyed")

    def _check_alive(self) -> None:
        if self._destroyed:
            raise CarlaSimulationError("sil_ctrl", "Vehicle actor already destroyed")


# ─── 蓝图生成器 ───────────────────────────────────────────────────────────────


class VehicleBlueprintGenerator:
    """HUNTER SE 车辆蓝图生成器。

    通过 CARLA Python API 的 blueprint_library 创建 HUNTER SE 蓝图，
    并设置物理参数覆盖属性。

    Args:
        blueprint_library: CARLA BlueprintLibrary 实例。
        params: HUNTER SE 物理参数。
    """

    BLUEPRINT_BASE_VEHICLE: str = "vehicle.micro.mpc.gen_01"
    """最接近 HUNTER SE 尺寸的 CARLA 内置车辆蓝图（用于物理近似）。"""

    def __init__(
        self,
        blueprint_library: BlueprintLibraryProtocol,
        params: Optional[HunterSEParameters] = None,
    ) -> None:
        self._lib = blueprint_library
        self._params = params or HunterSEParameters()

    def create_blueprint(self) -> Any:
        """生成 HUNTER SE 蓝图并设置物理属性。

        Returns:
            CARLA ActorBlueprint 对象（可传递给 world.spawn_actor）。

        Raises:
            CarlaSimulationError: 蓝图库中找不到基础车辆。
        """
        bp = self._lib.find(self.BLUEPRINT_BASE_VEHICLE)
        if bp is None:
            # 回退到通用车辆
            matches = self._lib.filter("vehicle.*")
            if not matches:
                raise CarlaSimulationError(
                    "create_blueprint",
                    f"No vehicle blueprint found (tried '{self.BLUEPRINT_BASE_VEHICLE}')",
                )
            bp = matches[0]
            logger.warning(f"Falling back to generic vehicle blueprint: {bp.id}")

        # 设置 HUNTER SE 特有属性
        _apply_hunter_se_attributes(bp, self._params)
        logger.info("HUNTER SE vehicle blueprint configured")
        return bp


# ─── 私有工具函数 ─────────────────────────────────────────────────────────────


def _make_carla_transform(transform: Transform) -> Any:
    """将内部 Transform 转换为 carla.Transform 对象。"""
    import carla  # noqa: PLC0415
    return carla.Transform(
        carla.Location(x=transform.x, y=transform.y, z=transform.z),
        carla.Rotation(
            pitch=math.degrees(transform.pitch),
            yaw=math.degrees(transform.yaw),
            roll=math.degrees(transform.roll),
        ),
    )


def _make_carla_vector3d(x: float, y: float, z: float) -> Any:
    """创建 carla.Vector3D 对象（延迟导入）。"""
    import carla  # noqa: PLC0415
    return carla.Vector3D(x=x, y=y, z=z)


def _make_carla_vehicle_control(cmd: VehicleControlCommand) -> Any:
    """将 VehicleControlCommand 转换为 carla.VehicleControl 对象。"""
    import carla  # noqa: PLC0415
    return carla.VehicleControl(
        throttle=cmd.throttle,
        steer=cmd.steer,
        brake=cmd.brake,
        hand_brake=cmd.hand_brake,
        reverse=cmd.reverse,
        gear=cmd.gear,
    )


def _validate_control_cmd(cmd: VehicleControlCommand, params: HunterSEParameters) -> None:
    """验证控制指令范围。"""
    if cmd.throttle < 0.0 or cmd.throttle > 1.0:
        raise ValidationError("throttle", cmd.throttle, "range [0, 1]")
    if cmd.steer < -1.0 or cmd.steer > 1.0:
        raise ValidationError("steer", cmd.steer, "range [-1, 1]")
    if cmd.brake < 0.0 or cmd.brake > 1.0:
        raise ValidationError("brake", cmd.brake, "range [0, 1]")


def _apply_hunter_se_attributes(bp: Any, params: HunterSEParameters) -> None:
    """将 HUNTER SE 物理参数写入 CARLA ActorBlueprint 属性。"""
    try:
        bp.set_attribute("mass", str(params.mass_kg))
        bp.set_attribute("wheelbase", str(params.wheelbase_m))
        bp.set_attribute("track_width", str(params.track_width_m * 0.5))
        bp.set_attribute("max_rpm", "6000")
        bp.set_attribute("engine_max_torque", str(params.engine_max_torque_nm))
        bp.set_attribute("drag_coefficient", str(params.drag_coefficient))
        # 后轮驱动
        bp.set_attribute("drive_back", "1.0")
        bp.set_attribute("drive_front", "0.0")
    except (AttributeError, Exception) as exc:
        logger.warning(f"Could not set blueprint attribute (non-critical): {exc}")


def _get_carla_time() -> float:
    """获取当前 CARLA 世界仿真时间戳（延迟导入避免循环依赖）。"""
    try:
        import carla  # noqa: PLC0415
        return 0.0  # 实际使用时从 world.get_snapshot().timestamp 获取
    except ImportError:
        import time
        return time.time()
