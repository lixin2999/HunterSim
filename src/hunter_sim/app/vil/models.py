"""VIL 虚实映射模块数据模型（不可变 pydantic 契约）。

对应开发提示词 §4：将实车遥测数据（定位、底盘、感知、IMU）标准化为不可变对象，
供坐标映射、状态同步、可视化与同步控制各子模块消费。所有模型使用 ``frozen=True``
确保跨线程/协程传递时的引用安全（Kafka 消费线程 → 事件循环）。

约定：
- 角度字段统一使用**弧度**，与仿真内部计算约定一致（AI 编码规则 §9.2）。
- ``PerceptionObject`` 中的 ``x``/``y``/``heading`` 视为**车辆 odom 系**下的相对量，
  由 :class:`CoordinateMapper` 转换至地图坐标系后再行绘制。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _FrozenModel(BaseModel):
    """VIL 内部不可变模型基类。"""

    model_config = ConfigDict(frozen=True)


class LocalizationData(_FrozenModel):
    """实车定位（odom 坐标系，右手系：X 前 / Y 左 / Z 上）。"""

    x: float = Field(description="纵向位移（米）")
    y: float = Field(description="横向位移（米）")
    heading: float = Field(description="航向角（弧度），逆时针为正")


class ChassisData(_FrozenModel):
    """底盘状态。"""

    velocity: float = Field(description="线速度大小（m/s）")
    steering: float = Field(ge=-1.0, le=1.0, description="方向盘转角（归一化 [-1,1]）")
    throttle: float = Field(ge=0.0, le=1.0)
    brake: float = Field(ge=0.0, le=1.0)
    angular_velocity: float = Field(default=0.0, description="yaw 角速度 (rad/s)")


class ImuData(_FrozenModel):
    """IMU 姿态（可选，若无则默认 0）。"""

    pitch: float = Field(default=0.0, description="俯仰角（弧度）")
    roll: float = Field(default=0.0, description="横滚角（弧度）")


class PerceptionObject(_FrozenModel):
    """感知目标（车辆 odom 系下相对坐标）。"""

    x: float
    y: float
    length: float = Field(gt=0, description="包围盒长（米，车辆前进方向）")
    width: float = Field(gt=0, description="包围盒宽（米）")
    height: float = Field(default=1.5, ge=0, description="包围盒高（米）")
    heading: float = Field(default=0.0, description="目标朝向（弧度，odom 系）")
    object_type: str = Field(default="unknown", description="类别：vehicle/pedestrian/...")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class PerceptionData(_FrozenModel):
    """感知与规划结果聚合。"""

    objects: list[PerceptionObject] = Field(default_factory=list)
    planning_trajectory: list[tuple[float, float]] = Field(
        default_factory=list, description="规划轨迹点列 (x, y) 于 odom 系"
    )


class VehicleTelemetry(_FrozenModel):
    """单帧实车遥测快照（Kafka ``telemetry_clean`` 消息解析后的标准形态）。"""

    vehicle_id: str = Field(min_length=1)
    timestamp: float = Field(description="实车 Unix 时间戳（秒）")
    localization: LocalizationData
    chassis: ChassisData
    perception: PerceptionData = Field(default_factory=PerceptionData)
    imu: ImuData | None = None
    behavior_state: str = Field(
        default="cruise", description="行为状态：cruise/follow/stop/lane_change/..."
    )


class VILCalibration(_FrozenModel):
    """初始标定：实车启动点在仿真地图中的对应位姿（§4.3.2）。"""

    x0: float = Field(description="仿真地图起始 X（米）")
    y0: float = Field(description="仿真地图起始 Y（米）")
    yaw0: float = Field(description="仿真地图起始航向（弧度，CARLA 定义）")


class VILConfig(_FrozenModel):
    """VIL 运行参数（从 :class:`VILSectionConfig` 派生，字段与场景配置对齐）。"""

    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_group_id: str = "carla-vil"
    telemetry_topic: str = "telemetry_clean"
    command_topic_pattern: str = "hunter.{vehicle_id}.command_result"
    target_vehicle_id: str = Field(min_length=1)
    calibration: VILCalibration
    delay_compensation_ms: float = Field(default=150.0, ge=0)
    data_timeout_ms: float = Field(default=500.0, ge=0)
    extrapolation_threshold_ms: float = Field(default=50.0, ge=0)
    sync_tick_interval_s: float = Field(default=0.02, gt=0, description="同步步长（秒）")
    ego_z_offset: float = Field(default=0.3, description="虚拟车辆 Z 偏移（米）")


__all__ = [
    "ChassisData",
    "ImuData",
    "LocalizationData",
    "PerceptionData",
    "PerceptionObject",
    "VILCalibration",
    "VILConfig",
    "VehicleTelemetry",
]
