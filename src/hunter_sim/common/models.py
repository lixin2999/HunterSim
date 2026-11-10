"""HunterSim 全局配置与数据模型。

本模块定义所有服务共用的 pydantic 配置模型和基础数据类型。
配置优先级：环境变量 > 配置文件 > 默认值。
"""

from __future__ import annotations

import enum
import ipaddress
import re
from typing import Annotated, Any, Literal, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# 全局配置需要统一组装管理面配置；``platform_adapter.config`` 只依赖 pydantic，
# 不反向引用本模块，不会形成循环导入。
from hunter_sim.platform_adapter.config import AdapterSettings


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
    """支持的传感器类型（含文档 §6.3.1 状态类话题发布类型）。"""

    LIDAR = "lidar"
    RGB_CAMERA = "rgb_camera"
    DEPTH_CAMERA = "depth_camera"
    IMU = "imu"
    GNSS = "gnss"
    COLLISION = "collision"
    LANE_INVASION = "lane_invasion"
    OBSTACLE = "obstacle"
    ODOMETRY = "odometry"
    VEHICLE_STATUS = "vehicle_status"


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
        lights: 灯光状态（left_turn/right_turn/low_beam/hazard）；
            缺省空字典表示实车未上报，仿真端不做任何灯光变更（V3.0.0）。
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
    lights: dict[str, bool] = Field(default_factory=dict)


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


# ─── 可选配置字段：空值即「未配置」 ───────────────────────────────────────────


def _blank_is_unset(value: Any) -> Any:
    """把空白字符串归一为 ``None``（回落字段默认值）。

    现场普遍以 ``cp .env.example .env`` 起步，模板里的留空项（``KEY=``）按文档约定是
    「用默认值」；但 pydantic-settings 会把空串原样交给字段校验，``Optional[int]``
    这类标量随即以 ``int_parsing`` 报错，**整份配置加载中断**（``huntersim up`` /
    ``huntersim doctor`` 起不来，且报错只提字段名，看不出是留空导致）。
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


# 可选标量/凭据字段统一走这组类型：留空 = 未配置，而不是校验失败或空凭据。
OptionalInt = Annotated[Optional[int], BeforeValidator(_blank_is_unset)]
OptionalStr = Annotated[Optional[str], BeforeValidator(_blank_is_unset)]
OptionalSecret = Annotated[Optional[SecretStr], BeforeValidator(_blank_is_unset)]


# ─── 服务配置模型 ─────────────────────────────────────────────────────────────


class CarlaSettings(BaseSettings):
    """CARLA 连接配置。"""

    model_config = SettingsConfigDict(
        env_prefix="CARLA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = Field("127.0.0.1", description="CARLA 服务器地址")
    rpc_port: int = Field(2000, ge=1024, le=65535, description="RPC 端口")
    stream_port: int = Field(2001, ge=1024, le=65535, description="数据流端口")
    tm_port: int = Field(8000, ge=1024, le=65535, description="Traffic Manager 端口")
    timeout_seconds: float = Field(10.0, gt=0.0, description="连接超时（秒）")
    map_load_timeout_seconds: float = Field(
        180.0,
        gt=0.0,
        le=1800.0,
        description=(
            "地图加载/切换（load_world）专用 RPC 超时（秒）。"
            "首次加载 Town06 等大地图/冷启动可达 20~60s，不能沿用 timeout_seconds"
        ),
    )
    read_timeout_seconds: float = Field(30.0, gt=0.0, description="读取超时（秒）")
    max_reconnect_attempts: int = Field(3, ge=0, description="最大重连次数")
    fixed_delta_seconds: float = Field(0.02, description="同步模式固定步长（秒），0.02=50Hz")
    substepping: bool = Field(True, description="是否启用 substepping")
    max_substep_delta_time: float = Field(0.01, gt=0.0, le=0.1, description="子步最大时间（秒，设计文档 §3.1.2）")
    max_substeps: int = Field(4, ge=1, le=10, description="最大子步数")
    # ── V2.8.0 P2-15：本地 CARLA 进程自愈（supervisor）──
    supervisor_enabled: bool = Field(
        False,
        description=(
            "是否由服务托管本机 CarlaUE4 进程（崩溃自动重启 + 事件写审计）；"
            "仅对回环地址端点生效，远程引擎不受影响（P2-15）"
        ),
    )
    supervisor_binary_path: str = Field(
        "", description="CarlaUE4 可执行文件绝对路径（空表示不可自愈，仅观测）"
    )
    supervisor_extra_args: str = Field(
        "", description="CARLA 启动附加参数（空格分隔，如 -quality-level=Low -RenderOffScreen）"
    )
    supervisor_max_restarts: int = Field(
        3, ge=0, le=20, description="单个巡检窗口内允许的最大自动重启次数（P2-15）"
    )
    supervisor_restart_backoff_seconds: float = Field(
        5.0, ge=0.0, le=600.0, description="两次自动重启之间的退避时长（秒）"
    )
    supervisor_ready_timeout_seconds: float = Field(
        60.0, gt=0.0, le=600.0, description="拉起后等待 RPC 端口就绪的超时（秒）"
    )
    supervisor_graceful_shutdown_seconds: float = Field(
        10.0, gt=0.0, le=120.0, description="关停时等待进程优雅退出的时长（秒）"
    )


class KafkaSettings(BaseSettings):
    """Kafka 数据总线配置（V2.10.0：消费 carla.{sim_id}.vil_state）。"""

    model_config = SettingsConfigDict(
        env_prefix="KAFKA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bootstrap_servers: str = Field("localhost:9092")
    group_id: str = Field("carla-vil", description="VIL 消费组 ID（设计文档 §4.2.2）")
    auto_offset_reset: str = Field("latest", pattern="^(earliest|latest|none)$")
    enable_auto_commit: bool = True
    telemetry_topic: str = Field(
        "carla.{sim_id}.vil_state",
        description="VIL 状态 Topic（平台转发；{sim_id} 在运行时渲染）",
    )
    legacy_telemetry_topic: str = Field(
        "", description="兼容单机调试的旧 telemetry_clean Topic（空则不订阅）"
    )
    sim_id: str = Field("", description="当前 VIL 实例的 sim_id（用于渲染 Topic 名）")
    username: OptionalSecret = None
    password: OptionalSecret = None
    # ── V3.1.2：消费侧接入安全（默认 PLAINTEXT，保持既有现场零改动）──
    security_protocol: str = Field(
        "PLAINTEXT",
        description="Kafka 安全协议：PLAINTEXT / SASL_PLAINTEXT / SSL / SASL_SSL",
    )
    sasl_mechanism: str = Field(
        "SCRAM-SHA-512", description="SASL 机制（仅 SASL_* 协议生效）"
    )
    ca_cert_path: OptionalStr = Field(
        None, description="SASL_SSL/SSL 的 CA 证书路径（None 用系统信任链）"
    )
    client_cert_path: OptionalStr = Field(
        None, description="mTLS 客户端证书路径（broker 强制 ssl.client.auth=required 时必填）"
    )
    client_key_path: OptionalStr = Field(
        None, description="mTLS 客户端私钥路径（须与 client_cert_path 成对配置）"
    )
    ssl_verify: bool = Field(
        True,
        description=(
            "是否校验 Kafka broker 服务端证书（False 时构造 CERT_NONE 的 "
            "ssl.SSLContext，仅限内网自签环境临时使用）"
        ),
    )
    auto_discover_certs: bool = Field(
        True,
        description=(
            "未显式配置证书路径时自动搜索 data/certs、data/kafka-certs、certs 目录"
        ),
    )
    # ── V2.7.0 P1-8：消费自愈与坏消息 DLQ ──
    enable_self_heal: bool = Field(
        True, description="连续轮询错误达阈值后自动重建 KafkaConsumer（P1-8）"
    )
    rebuild_after_errors: int = Field(
        10, ge=1, description="触发消费者重建的连续错误次数阈值（P1-8）"
    )
    dlq_topic: str = Field(
        "", description="坏消息死信队列 Topic（空则不转发，仅计数丢弃，P1-8）"
    )


class VILSettings(BaseSettings):
    """VIL 虚实映射参数配置。"""

    model_config = SettingsConfigDict(
        env_prefix="VIL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    extrapolation_ms: int = Field(150, ge=0, le=1000, description="位姿外推时间（毫秒）")
    max_data_latency_ms: int = Field(500, ge=100, le=5000, description="最大数据延迟（超过则暂停仿真）")
    warning_latency_ms: int = Field(50, ge=10, le=500, description="数据延迟警告阈值")
    buffer_max_frames: int = Field(10, ge=1, le=100, description="RingBuffer 最大帧数")
    sync_timeout_ms: int = Field(500, ge=100, le=2000, description="tick 等待超时（毫秒）")
    calibration_x0: float = Field(0.0, description="初始标定 X 偏移（CARLA 地图坐标）")
    calibration_y0: float = Field(0.0, description="初始标定 Y 偏移")
    calibration_yaw0: float = Field(0.0, description="初始标定航向偏移（弧度）")
    # ── V2.7.0 P1-9：链路强化（乱序/去重、自动环深、延迟分位数） ──
    ordering_guard_enabled: bool = Field(
        True, description="按 timestamp+seq 丢弃乱序/重复遥测消息（P1-9）"
    )
    telemetry_frequency_hz: float = Field(
        10.0, gt=0.0, le=200.0, description="遥测标称频率（Hz），用于自动推导环深（P1-9）"
    )
    auto_buffer_depth: bool = Field(
        True,
        description="为真时环深按 ceil(extrapolation_ms/1000 × 频率)+余量自动推导（P1-9）",
    )
    # ── V3.0.0：栅格对齐与车辆状态同步开关 ──
    target_hz: float = Field(
        10.0,
        gt=0.0,
        le=50.0,
        description=(
            "仿真栅格频率（Hz）：10–50Hz 到达的实车帧降采样到该频率后再写 CARLA，"
            "50Hz tick 下每次栅格推进后 5 个 tick 中只有 1 个会改车辆位姿"
        ),
    )
    overlay_enabled: bool = Field(
        True, description="是否将实车感知结果叠加绘制到 CARLA 世界（每 3 个栅格一次）"
    )
    lights_enabled: bool = Field(
        True, description="是否同步实车灯光（刹车/左转/右转/近光）到虚拟车辆"
    )
    clock_offset_auto: bool = Field(
        False,
        description=(
            "是否自动估计并补偿实车与仿真端时钟偏置；关闭时假设两端时钟已同步"
            "（保持既有对齐行为，仅记录 skew）"
        ),
    )


class ResourceSettings(BaseSettings):
    """资源管理配置（V3.0.0：单世界串行后仅保留实例寿命与巡检周期）。

    实例超时回收由 ``platform_adapter.executor.SimulationExecutor`` 的看门狗
    承担，GPU 池/实例配额/镜像仓库等语义已随 ``resource_manager`` 一并移除。
    """

    model_config = SettingsConfigDict(
        env_prefix="RESOURCE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    instance_max_lifetime_seconds: int = Field(
        7200, ge=60, description="实例最大运行时间（秒），看门狗兼作二次上限"
    )
    health_check_interval_seconds: int = Field(10, ge=1, description="实例健康巡检周期（秒）")


class ExecutionSettings(BaseSettings):
    """执行面配置（V2.4.0：执行面分离）。

    一个仿真实例绑定一个 CARLA 引擎端点；通过 ``engine_endpoints`` 显式声明
    可用引擎库存后，实例创建时独占分配端点，未占用端点可并行运行场景。
    留空时退化为单引擎（``CarlaSettings.host:rpc_port``），同一时刻仍只允许
    一个场景运行器（V2.3.0 兼容行为）。
    """

    model_config = SettingsConfigDict(
        env_prefix="EXECUTION_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    engine_endpoints: str = Field(
        "",
        description=(
            "CARLA 引擎端点清单，逗号分隔 host:rpc[:stream[:tm]]；"
            "留空表示单引擎（取 CARLA_HOST/CARLA_RPC_PORT）"
        ),
    )
    max_engines: int = Field(8, ge=1, le=64, description="端点清单最大解析数量（防止误配置超量）")
    restore_scene_on_assemble: bool = Field(
        True, description="重新装配运行器时自动恢复已持久化的场景定义（重启恢复）"
    )
    # ── V2.7.0 P1-7：租约心跳与僵尸持有者回收 ──
    lease_heartbeat_enabled: bool = Field(
        True, description="实例运行/状态查询时续租引擎心跳（P1-7）"
    )
    lease_ttl_seconds: int = Field(
        120, ge=10, description="心跳超过该时长未续则视为僵尸租约（P1-7）"
    )


class ProbeSettings(BaseSettings):
    """只读运维探针面配置（V3.0.0，``huntersim probes``）。

    探针面只暴露 ``/health`` 与 ``/metrics``，不承载业务、不写数据库；真实的
    CARLA/VIL/传感器/云端状态由管理面 ``/health`` 汇总后经 ``adapter_url``
    拉取，因此只需知道管理面地址即可。
    """

    model_config = SettingsConfigDict(
        env_prefix="PROBE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = Field("127.0.0.1", description="探针面监听地址（仅内网）")
    port: int = Field(8080, ge=1024, le=65535, description="探针面监听端口")
    adapter_url: str = Field(
        "http://127.0.0.1:8090", description="管理面基础地址（:8090），用于聚合 /health"
    )


class VizSettings(BaseSettings):
    """可视化客户端配置（V3.1.0，管理面 ``/viz`` 页面）。

    客户端是控制面的**只读附属视图**：不新建 REST 业务面，也不持有实例状态，
    只读取控制面已有的帧汇聚中心、事件缓冲与进程内日志总线。因此这里的配置
    项只影响展示体积（日志条数/点云抽样/深度图宽度），不影响仿真行为。

    Attributes:
        enabled: 是否注册 ``/viz`` 页面与 ``/viz/api/*`` 只读端点。
        auto_open_browser: ``huntersim up`` 启动后是否自动打开浏览器。
        log_buffer_size: 日志总线环形缓冲容量（条）。
        log_history_limit: 页面首屏/断线重连最多回放的历史日志条数。
        event_limit: 事件面板最多返回条数。
        lidar_max_points: 点云预览抽样上限（点）。
        depth_max_width: 深度图预览输出宽度（像素，0 表示不缩放）。
    """

    model_config = SettingsConfigDict(
        env_prefix="VIZ_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = Field(True, description="是否启用 /viz 可视化客户端页面")
    auto_open_browser: bool = Field(True, description="huntersim up 启动后自动打开浏览器")
    log_buffer_size: int = Field(
        2000, ge=100, le=20000, description="日志总线环形缓冲容量（条）"
    )
    log_history_limit: int = Field(
        500, ge=1, le=5000, description="日志面板首屏回放条数上限"
    )
    event_limit: int = Field(200, ge=1, le=2000, description="事件面板最多返回条数")
    lidar_max_points: int = Field(4000, ge=100, le=50000, description="点云预览抽样上限")
    depth_max_width: int = Field(480, ge=0, le=4096, description="深度图预览宽度（0=原始尺寸）")


class CloudSettings(BaseSettings):
    """HunterCore 数据采集分析平台接入配置（V2.10.0：平台契约重构）。

    接入链路（方案 §5）：

    - Kafka 数据面：发布 `carla.{sim_id}.sensor_data`（6 类消息 type 区分），
      key=sim_id，acks=all，SASL_SSL/SCRAM-SHA-512；
      DLQ：`carla.{sim_id}.sensor_data.dlq`；
    - VIL 消费面：订阅 `carla.{sim_id}.vil_state`（由 KafkaSettings 管理）；
    - MinIO bag：经 data-collector `POST /api/v1/data/files/presign` 预签名直传。

    `enabled=false` 时连接器整体降级为 no-op，不影响本地仿真。
    凭据（SASL 口令 / JWT）仅经环境变量或 `.env` 注入，禁止硬编码。
    """

    model_config = SettingsConfigDict(
        env_prefix="HUNTER_CORE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = Field(
        True,
        description=(
            "是否启用 HunterCore 数据回传（V3.2.2 起默认打开）；未配凭据时整体降级为 "
            "no-op，不阻断本机仿真，纯本机联调可显式置 HUNTER_CORE_ENABLED=false 关闭"
        ),
    )
    # ── Kafka 数据面（发布 carla.{sim_id}.sensor_data）──
    kafka_bootstrap: str = Field("192.168.31.35:9092", description="Kafka broker（SASL_SSL）")
    security_protocol: str = Field("SASL_SSL", description="Kafka 安全协议")
    sasl_mechanism: str = Field("SCRAM-SHA-512", description="SASL 机制")
    sasl_username: OptionalSecret = None
    sasl_password: OptionalSecret = None
    ca_cert_path: OptionalStr = Field(None, description="SASL_SSL CA 证书路径（None 用系统信任链）")
    client_cert_path: OptionalStr = Field(
        None, description="mTLS 客户端证书路径（broker 强制 ssl.client.auth=required 时必填）"
    )
    client_key_path: OptionalStr = Field(
        None, description="mTLS 客户端私钥路径（须与 client_cert_path 成对配置）"
    )
    ssl_verify: bool = Field(
        True, description="是否校验 Kafka broker 服务端证书（False 仅限内网自签环境）"
    )
    auto_discover_certs: bool = Field(
        True, description="未显式配置证书路径时自动搜索 data/certs 等目录"
    )
    # ── sim_id 与 Topic 模板 ──
    sim_id: str = Field("", description="当前实例 sim_id（空=运行时由 instance_manager 分配）")
    sim_id_prefix: str = Field("sim", description="GUI 单实例模式 sim_id 前缀")
    sensor_data_topic_template: str = Field(
        "carla.{sim_id}.sensor_data",
        description="sensor_data Topic 模板（{sim_id} 在 SimDataProducer 初始化时渲染）",
    )
    sensor_data_dlq_template: str = Field(
        "carla.{sim_id}.sensor_data.dlq",
        description="sensor_data DLQ Topic 模板",
    )
    sensor_data_acks: str = Field("all", description="acks=all 保证可靠性")
    sensor_data_max_inflight: int = Field(5, ge=1, le=100, description="最大未确认批次")
    # ── 发布频率 ──
    status_report_interval_s: float = Field(1.0, description="sim_status 定时上报周期（秒）")
    telemetry_proxy_interval_s: float = Field(0.1, description="sim_telemetry 发布周期（秒，10Hz）")
    # ── data-collector 文件上传（经 api-gateway）──
    gateway_url: str = Field("http://192.168.31.35:8080", description="api-gateway 基础地址")
    jwt_token: OptionalSecret = Field(None, description="data-collector REST Bearer JWT")
    rosbag_bucket: str = Field("hunter-rosbag", description="bag 上传 Bucket")
    upload_timeout_s: float = Field(60.0, gt=0.0, description="预签名/直传 HTTP 超时（秒）")
    upload_queue_max_size: int = Field(
        32, ge=1, le=1024, description="bag 上传后台队列容量（有界防积压，满时丢最旧）"
    )
    upload_max_retries: int = Field(
        2, ge=0, le=5, description="bag 上传失败后的自动重试次数（0 表示不重试）"
    )
    upload_retry_backoff_s: float = Field(
        1.0, ge=0.0, le=30.0, description="bag 上传重试退避（秒），停止时立即中断"
    )


#: 仿真节点标识（与 HunterCore 契约 ``SimNodeIdPath`` 一致，禁止用 IP 命名）
SIM_NODE_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,63}$"
_SIM_NODE_ID_RE = re.compile(SIM_NODE_ID_PATTERN)


class SimNodeSettings(BaseSettings):
    """仿真节点控制面配置（HunterCore ``node`` 模式注册/心跳）。

    仿真机作为「仿真节点」接入平台后，平台从入站请求头自学习本机地址，
    再按该地址直推 CARLA 管理 API（:8090）；IP 漂移由下一次心跳自愈。

    ``enabled=false`` 时控制面完全保持既有行为，不会向平台发起任何注册请求。

    V3.2.2 起默认打开，但**无凭据即静默跳过**：``node_id``/服务令牌未配时
    ``SimNodeClient`` 只记一条日志便不启动心跳，绝不阻断本机仿真；
    ``huntersim doctor`` 对应项也从 fail 降级为 warn。

    Attributes:
        enabled: 是否启用节点注册/心跳（V3.2.2 起默认打开）。
        node_id: 节点永久标识（与 IP 无关，形如 ``sim-node-01``）；启用时必填。
        base_url: 平台基础地址（空表示复用 ``HUNTER_CORE_GATEWAY_URL``）。
        service_token: 平台签发的服务令牌（``service:{client_id}``，启用时必填）。
        heartbeat_interval_seconds: 心跳周期（秒，必须 ≤30s：平台租约默认 90s）。
        request_timeout_seconds: 注册/心跳单次 HTTP 超时（秒）。
        capacity: 可并行实例上限（1-32，参与平台选节点排序）。
        maps: 可用地图清单（逗号分隔；留空 = 不做地图过滤）。
        carla_version: CARLA 版本（留空时尝试从已安装 SDK 推导）。
        protocol: 平台回访本机管理 API 的协议（http/https）。
        manager_port: 管理 API 端口（空表示复用 ``ADAPTER_PORT``）。
        labels: 自定义标签（仅展示，如机房/显卡型号）。
        runtime_hint: 节点自述地址（仅展示，绝不参与寻址）。
        result_enabled: 是否向节点级 Topic 回传实例终态结果（V2.0 独立回传通道）。
        result_topic_template: 节点级回传 Topic 模板（``{node_id}`` 运行时渲染）。
        result_dlq_template: 节点级回传死信 Topic 模板。
    """

    model_config = SettingsConfigDict(
        env_prefix="HUNTER_CORE_SIM_NODE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(
        True,
        description=(
            "是否作为仿真节点接入平台（注册/心跳）；V3.2.2 起默认打开，"
            "无 node_id/服务令牌时静默跳过（不启动心跳，不阻断本机仿真）"
        ),
    )
    node_id: str = Field(
        "",
        validation_alias=AliasChoices("HUNTER_CORE_SIM_NODE_ID", "node_id"),
        description="节点永久标识（与 IP 无关，如 sim-node-01）",
    )
    base_url: str = Field(
        "", description="平台基础地址（空表示复用 HUNTER_CORE_GATEWAY_URL）"
    )
    service_token: OptionalSecret = Field(
        None, description="平台签发的服务令牌（service:{client_id}）"
    )
    heartbeat_interval_seconds: float = Field(
        20.0, ge=5.0, le=30.0, description="心跳周期（秒，平台要求 ≤30s）"
    )
    request_timeout_seconds: float = Field(
        5.0, gt=0.0, le=60.0, description="注册/心跳 HTTP 超时（秒）"
    )
    capacity: int = Field(1, ge=1, le=32, description="可并行实例上限")
    maps: str = Field("", description="可用地图清单（逗号分隔；空=不做地图过滤）")
    carla_version: str = Field("", description="CARLA 版本（空=尝试从 SDK 推导）")
    protocol: Literal["http", "https"] = Field("http", description="管理 API 协议")
    manager_port: OptionalInt = Field(
        None, ge=1, le=65535, description="管理 API 端口（空=复用 ADAPTER_PORT）"
    )
    labels: dict[str, str] = Field(
        default_factory=dict, description="自定义标签（仅展示）"
    )
    runtime_hint: str = Field("", description="节点自述地址（仅展示，不参与寻址）")
    # ── V2.0 节点级 Kafka 回传通道（方案 §2.5.4 / §8.1）──
    result_enabled: bool = Field(
        True,
        description=(
            "是否向节点级 Topic 回传实例终态结果（复用平台数据面凭据，"
            "未配凭据/未启用时安全 no-op）"
        ),
    )
    result_topic_template: str = Field(
        "simulation.{node_id}.result",
        description="节点级回传 Topic 模板（{node_id} 运行时渲染）",
    )
    result_dlq_template: str = Field(
        "simulation.{node_id}.result.dlq",
        description="节点级回传死信 Topic 模板",
    )

    def render_result_topic(self) -> str:
        """渲染节点级回传 Topic 全名（node_id 为空时返回空串，表示不可用）。"""
        if not self.node_id:
            return ""
        return self.result_topic_template.format(node_id=self.node_id)

    def render_result_dlq_topic(self) -> str:
        """渲染节点级回传死信 Topic 全名（node_id 为空时返回空串）。"""
        if not self.node_id:
            return ""
        return self.result_dlq_template.format(node_id=self.node_id)

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        """节点标识必须符合平台契约（空串表示未配置，由启动检查拦截）。"""
        text = (value or "").strip()
        if text and not _SIM_NODE_ID_RE.match(text):
            raise ValueError(
                f"node_id 必须匹配 {SIM_NODE_ID_PATTERN}（小写字母/数字开头，"
                "3–64 位，可含 . _ -；禁止用 IP 命名）"
            )
        if text:
            try:
                ipaddress.ip_address(text)
            except ValueError:
                pass
            else:
                raise ValueError("node_id 不得使用 IP 地址（标识必须与地址解耦）")
        return text


class HunterSimSettings(BaseSettings):
    """HunterSim 全局聚合配置。

    配置文件优先级：真实环境变量 > `.env` > 代码默认值；`.env` 需位于服务进程
    工作目录。各前缀分组（CARLA_/KAFKA_/VIL_/RESOURCE_/EXECUTION_/HUNTER_CORE_/
    ADAPTER_/PROBE_）同样读取该 `.env`，`extra="ignore"` 保证同一份文件中其他
    前缀的键不会触发校验错误。

    V3.0.0 起 ``adapter`` 随全局配置一并组装：管理面（:8090）与探针面（:8080）
    共用同一份配置对象，避免两处各自加载 ``ADAPTER_*`` 得出不同结论。
    """

    model_config = SettingsConfigDict(
        env_prefix="HUNTER_SIM_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: str = Field("dev", pattern="^(dev|staging|prod)$")
    log_level: str = Field("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    # ── V2.7.0 P1-11：日志统一（structlog JSON + 可选文件轮转）──
    log_json: bool = Field(
        False, description="为真时模块日志以 JSON 渲染（生产采集）；否则保留可读文本（P1-11）"
    )
    log_file: str = Field(
        "", description="日志文件路径（空表示仅 stdout）；启用时自动接入轮转文件处理器（P1-11）"
    )
    log_max_bytes: int = Field(
        10_485_760, ge=1024, description="单日志文件最大字节数，超过则轮转（P1-11）"
    )
    log_backup_count: int = Field(
        5, ge=0, le=50, description="轮转保留的历史日志文件数（P1-11）"
    )
    carla: CarlaSettings = Field(default_factory=CarlaSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    vil: VILSettings = Field(default_factory=VILSettings)
    resource: ResourceSettings = Field(default_factory=ResourceSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    cloud: CloudSettings = Field(default_factory=CloudSettings)
    sim_node: SimNodeSettings = Field(default_factory=SimNodeSettings)
    adapter: AdapterSettings = Field(default_factory=AdapterSettings)
    probe: ProbeSettings = Field(default_factory=ProbeSettings)
    viz: VizSettings = Field(default_factory=VizSettings)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """验证日志级别合法性。"""
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return v

    @model_validator(mode="after")
    def _inherit_kafka_from_cloud(self) -> "HunterSimSettings":
        """V3.3.0：VIL 消费端 Kafka 连接参数自动继承 CloudSettings（消除 .env 重复）。

        规则：
        - 若 cloud.sasl_username 已被显式配置（出现在 cloud.model_fields_set），
          表明用户正在接入平台 Kafka，则把整套连接参数（bootstrap / 协议 / 机制 /
          凭据 / 证书）继承到 KafkaSettings 中未被显式设定的字段。
        - 若用户显式配置了 KAFKA_* 环境变量，则保留用户值（不覆盖）。
        - 纯本机联调（未配任何 HUNTER_CORE_ 凭据）时不触发继承，KafkaSettings 维持
          默认 localhost:9092 PLAINTEXT。
        """
        cloud_fields = self.cloud.model_fields_set
        kafka_fields = self.kafka.model_fields_set

        # 触发条件：用户显式配了 cloud 的 SASL 身份（意味着正在接入平台 Kafka）
        # 留空（blank_is_unset → None）不触发
        if "sasl_username" not in cloud_fields or self.cloud.sasl_username is None:
            return self

        overrides: dict[str, Any] = {}

        # 1. Bootstrap
        if "bootstrap_servers" not in kafka_fields:
            overrides["bootstrap_servers"] = self.cloud.kafka_bootstrap

        # 2. 安全协议与 SASL 机制（cloud 默认值即为平台期望的 SASL_SSL/SCRAM-SHA-512）
        if "security_protocol" not in kafka_fields:
            overrides["security_protocol"] = self.cloud.security_protocol
        if "sasl_mechanism" not in kafka_fields:
            overrides["sasl_mechanism"] = self.cloud.sasl_mechanism

        # 3. SASL 凭据
        if "username" not in kafka_fields and self.cloud.sasl_username:
            overrides["username"] = self.cloud.sasl_username
        if "password" not in kafka_fields and self.cloud.sasl_password:
            overrides["password"] = self.cloud.sasl_password

        # 4. 证书路径
        if "ca_cert_path" not in kafka_fields and "ca_cert_path" in cloud_fields:
            if self.cloud.ca_cert_path:
                overrides["ca_cert_path"] = self.cloud.ca_cert_path
        if "client_cert_path" not in kafka_fields and self.cloud.client_cert_path:
            overrides["client_cert_path"] = self.cloud.client_cert_path
        if "client_key_path" not in kafka_fields and self.cloud.client_key_path:
            overrides["client_key_path"] = self.cloud.client_key_path
        if "ssl_verify" not in kafka_fields:
            overrides["ssl_verify"] = self.cloud.ssl_verify
        if "auto_discover_certs" not in kafka_fields:
            overrides["auto_discover_certs"] = self.cloud.auto_discover_certs

        if overrides:
            self.kafka = self.kafka.model_copy(update=overrides)

        return self


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
