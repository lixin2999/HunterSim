"""HunterSim 全局配置与数据模型。

本模块定义所有服务共用的 pydantic 配置模型和基础数据类型。
配置优先级：环境变量 > 配置文件 > 默认值。
"""

from __future__ import annotations

import enum
from typing import Optional

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ─── 枚举类型 ─────────────────────────────────────────────────────────────────


class SimMode(str, enum.Enum):
    """仿真运行模式。"""

    VIL = "vil"   # 车辆在环
    SIL = "sil"   # 软件在环（场景驱动）
    REPLAY = "replay"  # 数据回放


class QualityLevel(str, enum.Enum):
    """CARLA 渲染画质等级。"""

    LOW = "low"
    MEDIUM = "medium"
    EPIC = "epic"


class InstanceStatus(str, enum.Enum):
    """仿真实例生命周期状态。"""

    CREATED = "created"
    LOADING = "loading"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    DESTROYED = "destroyed"


class SceneStatus(str, enum.Enum):
    """场景运行状态机状态。"""

    CREATED = "created"
    LOADING = "loading"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class SensorType(str, enum.Enum):
    """支持的传感器类型。"""

    LIDAR = "lidar"
    RGB_CAMERA = "rgb_camera"
    DEPTH_CAMERA = "depth_camera"
    IMU = "imu"
    GNSS = "gnss"
    COLLISION = "collision"
    LANE_INVASION = "lane_invasion"
    OBSTACLE = "obstacle"


class EvalGrade(str, enum.Enum):
    """评估等级。"""

    S = "S"
    A = "A"
    B = "B"
    C = "C"
    D = "D"


# ─── 基础几何数据类 ───────────────────────────────────────────────────────────


class Transform(BaseModel):
    """6-DOF 位姿（位置和旋转）。

    内部统一使用弧度制，API 层转换为度制。

    Attributes:
        x: X 坐标（米）。
        y: Y 坐标（米）。
        z: Z 坐标（米）。
        pitch: 俯仰角（弧度）。
        yaw: 航向角（弧度）。
        roll: 横滚角（弧度）。
    """

    x: float = Field(0.0, description="X position (m)")
    y: float = Field(0.0, description="Y position (m)")
    z: float = Field(0.0, description="Z position (m)")
    pitch: float = Field(0.0, ge=-1.5708, le=1.5708, description="Pitch (rad)")
    yaw: float = Field(0.0, ge=-3.1416, le=3.1416, description="Yaw (rad)")
    roll: float = Field(0.0, ge=-0.7853, le=0.7853, description="Roll (rad)")


class VehicleControlCommand(BaseModel):
    """车辆控制指令（SIL模式）。

    Attributes:
        throttle: 油门，范围 [0, 1]。
        steer: 方向盘转向，范围 [-1, 1]。
        brake: 制动，范围 [0, 1]。
        hand_brake: 是否启用驻车制动。
        reverse: 是否倒车。
        gear: 挡位。
    """

    throttle: float = Field(0.0, ge=0.0, le=1.0)
    steer: float = Field(0.0, ge=-1.0, le=1.0)
    brake: float = Field(0.0, ge=0.0, le=1.0)
    hand_brake: bool = False
    reverse: bool = False
    gear: int = Field(1, ge=-1, le=6)


class VehicleState(BaseModel):
    """车辆实时状态。

    Attributes:
        time_stamp: Unix 时间戳（秒）。
        transform: 当前位姿。
        velocity: 速度向量 (vx, vy, vz)。
        acceleration: 加速度向量 (ax, ay, az)。
        angular_velocity: 角速度 (wx, wy, wz)。
        steering: 方向盘角度（弧度）。
        throttle: 油门开度。
        brake: 制动开度。
        gear: 当前挡位。
        vehicle_speed: 车速标量（m/s）。
    """

    time_stamp: float
    transform: Transform
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0)
    acceleration: tuple[float, float, float] = (0.0, 0.0, 0.0)
    angular_velocity: tuple[float, float, float] = (0.0, 0.0, 0.0)
    steering: float = 0.0
    throttle: float = 0.0
    brake: float = 0.0
    gear: int = 1
    vehicle_speed: float = 0.0


class DetectedObject(BaseModel):
    """感知目标对象。

    Attributes:
        object_id: 目标唯一 ID。
        object_type: 目标类型（vehicle/pedestrian/cyclist 等）。
        transform: 目标位姿（世界坐标系）。
        size: 目标尺寸 (length, width, height)。
        velocity: 目标速度向量。
        confidence: 检测置信度 [0, 1]。
    """

    object_id: int
    object_type: str
    transform: Transform
    size: tuple[float, float, float] = (4.0, 2.0, 1.5)
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0)
    confidence: float = Field(1.0, ge=0.0, le=1.0)


class PerceptionResult(BaseModel):
    """一帧感知结果。

    Attributes:
        time_stamp: 感知时间戳。
        objects: 检测到的目标列表。
    """

    time_stamp: float
    objects: list[DetectedObject] = Field(default_factory=list)


# ─── 服务配置模型 ─────────────────────────────────────────────────────────────


class CarlaSettings(BaseSettings):
    """CARLA 连接配置。"""

    model_config = SettingsConfigDict(env_prefix="CARLA_")

    host: str = Field("127.0.0.1", description="CARLA 服务器地址")
    rpc_port: int = Field(2000, ge=1024, le=65535, description="RPC 端口")
    stream_port: int = Field(2001, ge=1024, le=65535, description="数据流端口")
    tm_port: int = Field(8000, ge=1024, le=65535, description="Traffic Manager 端口")
    timeout_seconds: float = Field(10.0, gt=0.0, description="连接超时（秒）")
    read_timeout_seconds: float = Field(30.0, gt=0.0, description="读取超时（秒）")
    max_reconnect_attempts: int = Field(3, ge=0, description="最大重连次数")
    fixed_delta_seconds: float = Field(0.02, description="同步模式固定步长（秒），0.02=50Hz")
    substepping: bool = Field(True, description="是否启用 substepping")
    max_substeps: int = Field(4, ge=1, le=10, description="最大子步数")


class KafkaSettings(BaseSettings):
    """Kafka 数据总线配置。"""

    model_config = SettingsConfigDict(env_prefix="KAFKA_")

    bootstrap_servers: str = Field("localhost:9092")
    group_id: str = Field("hunter_sim_consumer")
    auto_offset_reset: str = Field("latest", pattern="^(earliest|latest|none)$")
    enable_auto_commit: bool = True
    telemetry_topic: str = Field("telemetry_clean")
    command_topic: str = Field("hunter.{vehicle_id}.command_result")
    username: Optional[SecretStr] = None
    password: Optional[SecretStr] = None


class VILSettings(BaseSettings):
    """VIL 虚实映射参数配置。"""

    model_config = SettingsConfigDict(env_prefix="VIL_")

    extrapolation_ms: int = Field(150, ge=0, le=1000, description="位姿外推时间（毫秒）")
    max_data_latency_ms: int = Field(500, ge=100, le=5000, description="最大数据延迟（超过则暂停仿真）")
    warning_latency_ms: int = Field(50, ge=10, le=500, description="数据延迟警告阈值")
    buffer_max_frames: int = Field(10, ge=1, le=100, description="RingBuffer 最大帧数")
    sync_timeout_ms: int = Field(500, ge=100, le=2000, description="tick 等待超时（毫秒）")
    calibration_x0: float = Field(0.0, description="初始标定 X 偏移（CARLA 地图坐标）")
    calibration_y0: float = Field(0.0, description="初始标定 Y 偏移")
    calibration_yaw0: float = Field(0.0, description="初始标定航向偏移（弧度）")


class APISettings(BaseSettings):
    """REST API 服务配置。"""

    model_config = SettingsConfigDict(env_prefix="API_")

    host: str = "0.0.0.0"
    port: int = Field(8080, ge=1024, le=65535)
    jwt_secret: SecretStr = Field(
        default=SecretStr("CHANGE_ME_IN_PRODUCTION"),
        description="JWT 签名密钥，生产环境必须通过环境变量覆盖",
    )
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(1440, ge=1, le=10080)
    cors_allow_origins: list[str] = Field(default=["*"])
    rate_limit_per_minute: int = Field(1000, ge=1)
    max_concurrent_instances: int = Field(2, ge=1, le=16, description="单 GPU 最大并发实例数")


class ResourceSettings(BaseSettings):
    """资源管理配置。"""

    model_config = SettingsConfigDict(env_prefix="RESOURCE_")

    instance_max_lifetime_seconds: int = Field(7200, ge=60, description="实例最大运行时间（2小时）")
    max_retry_count: int = Field(3, ge=0, le=10, description="实例异常重启最大次数")
    health_check_interval_seconds: int = Field(10, ge=1)
    gpu_memory_warning_threshold: float = Field(0.90, ge=0.0, le=1.0, description="显存告警阈值")
    docker_image: str = Field("carlasim/carla:0.9.16", description="CARLA Docker 镜像")


class HunterSimSettings(BaseSettings):
    """HunterSim 全局聚合配置。"""

    model_config = SettingsConfigDict(env_prefix="HUNTER_SIM_", env_file=".env", env_file_encoding="utf-8")

    env: str = Field("dev", pattern="^(dev|staging|prod)$")
    log_level: str = Field("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    carla: CarlaSettings = Field(default_factory=CarlaSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    vil: VILSettings = Field(default_factory=VILSettings)
    api: APISettings = Field(default_factory=APISettings)
    resource: ResourceSettings = Field(default_factory=ResourceSettings)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """验证日志级别合法性。"""
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return v


# ─── HUNTER SE 车辆规格常量 ───────────────────────────────────────────────────


class HunterSESpec:
    """HUNTER SE 底盘车硬件规格常量。

    来源：实车设计文档，禁止在业务代码中重复硬编码这些数值。
    """

    # 外形尺寸（米）
    LENGTH: float = 0.820
    WIDTH: float = 0.640
    HEIGHT: float = 0.310

    # 动力学参数
    MASS_KG: float = 60.0
    WHEELBASE_M: float = 0.46         # 轴距
    TRACK_WIDTH_M: float = 0.54       # 轮距
    CG_HEIGHT_M: float = 0.15         # 质心高度

    # 驱动/转向
    MAX_SPEED_MS: float = 4.8         # 最大速度 (m/s)
    MAX_STEER_RAD: float = 0.4        # 最大前轮转向角 (rad)
    DRIVE_WHEELS: str = "rear"        # 后轮驱动
    STEERING_TYPE: str = "ackermann"  # 前轮阿克曼转向

    # 传感器安装位置（车辆坐标系，base_link = 后轴中心地面投影点）
    LIDAR_OFFSET: tuple[float, float, float] = (0.23, 0.0, 0.35)    # (x, y, z) 车顶中心
    CAMERA_OFFSET: tuple[float, float, float] = (0.40, 0.0, 0.30)   # (x, y, z) 车顶前向

    # 仿真话题前缀
    SIM_TOPIC_PREFIX: str = "/carla/"


# ─── ROS2 话题映射常量 ────────────────────────────────────────────────────────


class RosTopicMapping(BaseModel):
    """单条 ROS2 话题映射记录。"""

    sim_topic: str
    real_topic: str
    msg_type: str
    frequency_hz: float


ROS2_TOPIC_MAP: list[RosTopicMapping] = [
    RosTopicMapping(
        sim_topic="/carla/lidar_points",
        real_topic="/lidar_points",
        msg_type="sensor_msgs/PointCloud2",
        frequency_hz=10.0,
    ),
    RosTopicMapping(
        sim_topic="/carla/camera/color/image_raw",
        real_topic="/camera/color/image_raw",
        msg_type="sensor_msgs/Image",
        frequency_hz=30.0,
    ),
    RosTopicMapping(
        sim_topic="/carla/camera/depth/image_rect_raw",
        real_topic="/camera/depth/image_rect_raw",
        msg_type="sensor_msgs/Image",
        frequency_hz=30.0,
    ),
    RosTopicMapping(
        sim_topic="/carla/imu/data",
        real_topic="/imu/data",
        msg_type="sensor_msgs/Imu",
        frequency_hz=100.0,
    ),
    RosTopicMapping(
        sim_topic="/carla/odometry",
        real_topic="/localization/odom",
        msg_type="nav_msgs/Odometry",
        frequency_hz=50.0,
    ),
    RosTopicMapping(
        sim_topic="/carla/vehicle_status",
        real_topic="/chassis/state",
        msg_type="custom_msgs/ChassisState",
        frequency_hz=10.0,
    ),
]
