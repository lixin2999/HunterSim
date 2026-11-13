"""内置场景执行器（V2.3.0）。

把平台下发的 ``SceneConfig`` 真正落到 CARLA 世界：
生成自车、生成交通参与者、按需挂载碰撞/车道偏离传感器，
使 REST 控制面「创建实例 → 下发场景 → 启动 → 查询进度 → 停止」闭环可用。

本模块是**最小可用实现**：
- 自车按 ``ego_vehicle.spawn_point`` 生成，``autopilot`` 为真时交由 Traffic Manager 驾驶；
- 交通参与者按配置生成 Actor，车辆类参与者非静止行为时启用 autopilot，
  行人/骑行者仅生成静态 Actor（walker AI 控制器属 P1 范围）；
- 传感器挂载仅覆盖场景事件声明所需类型（碰撞 / 车道偏离）。

所有 CARLA 依赖均为惰性导入：无 SDK 环境下由 ``SceneRunnerService``
在装配阶段拦截，不会影响 API 服务启动。
"""

from __future__ import annotations

import math
import os
import queue
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Optional

from hunter_sim.common.exceptions import (
    CarlaSimulationError,
    ConfigurationError,
    SensorSimulationError,
)
from hunter_sim.common.metrics import SENSOR_FRAMES_DROPPED_TOTAL
from hunter_sim.common.models import SensorType
from hunter_sim.common.utils import get_logger
from hunter_sim.scene_runner.custom_scenario import CustomScenarioBase
from hunter_sim.scene_runner.event_detector import EventDetector, TriggerMetrics
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
    ScenarioBehaviorType,
    SceneConfig,
    SceneEventType,
    SensorSpec,
    SpawnPoint,
    TrafficParticipantConfig,
    TriggerType,
)
from hunter_sim.scene_runner.scene_state_manager import SceneStateManager
from hunter_sim.scene_runner.spawn_resolver import (
    is_placeholder_origin,
    relative_offset_from_ego,
    resolve_actor_spawn,
    resolve_ego_spawn_candidates,
    scene_key_of,
    transform_to_spawn_point,
)
from hunter_sim.sensor_sim.frame_hub import (
    FrameHub,
    STREAM_DEPTH,
    STREAM_OVERVIEW,
    STREAM_RGB,
)
from hunter_sim.sensor_sim.image_codec import depth_bgra_to_meters, encode_rgb_frame
from hunter_sim.sensor_sim.mcap_recorder import (
    McapRecorder,
    build_image_message,
    build_imu_message,
    build_pointcloud2_message,
)
from hunter_sim.sensor_sim.profiles import resolve_profiles
from hunter_sim.sensor_sim.sensor_factory import RGBCameraConfig, SensorBlueprintFactory
from hunter_sim.sensor_sim.sensor_mount import (
    SensorMountManager,
    load_overview_camera_config,
)
from hunter_sim.traffic_sim.actor_behaviors import (
    CutInBehavior,
    ScriptedActorController,
    create_behavior_from_config,
)
from hunter_sim.traffic_sim.trigger_conditions import create_trigger_from_config
from hunter_sim.traffic_sim.walker_controller import WalkerControllerWrapper

logger = get_logger(__name__)

# 生成位姿工厂签名：(world, spawn_point) -> carla.Transform
TransformFactory = Callable[[Any, SpawnPoint], Any]

# 自车/参与者默认离地高度（米）：spawn z<=0 时按地面高度 + 该值放置
_DEFAULT_CLEARANCE_M = 0.3
# 无法查询地面时的兜底高度（米）
_FALLBACK_Z_M = 0.5

_AUTOPILOT_TM_PORT = 8000

#: 环视相机挂载位置（configs/sensor_configs.json 的 mounts.overview_camera）
_OVERVIEW_MOUNT_ID = "overview_camera"

# 环视可视化帧队列容量：只保留最新画面，独立于数据传感器编码队列，
# 避免 RGB/深度/点云/IMU 高负载时可视化画面被饿死
_OVERVIEW_QUEUE_MAXSIZE = 4

#: 环视主视区 JPEG 质量下限：主视区是验收画面的唯一来源，不再套用数据流质量
#: （数据流默认 75 只保证传输）。V3.4.3 现场反馈 1080p + 质量 90 编码耗时偏长
#: （满 10Hz 编码吃 CPU，画面发脏/发顿），降到 85：分辨率不变，画质肉眼看不出
#: 差异，单帧体积和编码耗时降约 20%~30%。
_OVERVIEW_JPEG_QUALITY = 85

#: 环视主视区 JPEG 质量阶梯（V3.5.5）：编码持续超预算逐档下探，长期富余则升回。
#: 首档就是 ``_OVERVIEW_JPEG_QUALITY``，因此默认画质与 V3.4.4 起完全一致，
#: 只在真跑不动时才牺牲画质换帧率（JPEG 编码单线程 + GIL 受限，多核补不了）。
#: 注：生效值不低于 ``screenshot_quality``（主视区画质不低于数据流），被夹住的
#: 档位会被跳过，日志只报真实变化。
_OVERVIEW_QUALITY_LADDER: tuple[int, ...] = (_OVERVIEW_JPEG_QUALITY, 78, 72)
#: 连续多少帧超预算才降一档：单帧抖动（GC / 其他线程抢 CPU）不动画质，避免来回跳变
_OVERVIEW_SLOW_STREAK = 8
#: 降档后连续多少帧在预算内则升回一档（机器空下来了画质要能自己回来）
_OVERVIEW_FAST_STREAK = 150

#: 环视单帧编码耗时告警阈值（秒）：1080p@10Hz 的单帧预算约 100 ms，
#: 超过该值说明编码跟不上下集，画面会表现为卡顿，需要在日志里直接可见
_OVERVIEW_SLOW_ENCODE_S = 0.12
#: 环视慢编码告警限频窗口（秒）
_OVERVIEW_SLOW_LOG_INTERVAL_S = 60.0

#: 交通参与者「可视距离」告警阈值（米）：超出该距离时环视相机基本拍不到，
#: 日志给出 WARNING 便于定位「参与者生成了但画面里看不到」
_MAX_VISIBLE_DISTANCE_M = 80.0

#: 环视相机安装参数（与 ``configs/sensor_configs.json`` 的 overview_camera 一致）：
#: 挂在自车后缘再向后 5m、水平 FOV 100°、pitch -20°。仅用于粗估参与者是否落在
#: 主视区内（日志告警 / 可视化面板的「可见」判据），不做精确投影。
_OVERVIEW_CAMERA_BACK_M = 5.0
_OVERVIEW_HALF_FOV_DEG = 50.0

#: 行人脚本化横穿的到达判定阈值（米）
_WALKER_ARRIVE_EPS_M = 0.3

#: V3.8.18：脚本化行人与自车的接触判定阈值（米，水平距离）。
#: 行人物理禁用后碰撞传感器不再触发，用该阈值补报碰撞（行人半径≈0.5m
#: + 自车半宽≈1.0m 的近似）；离开 2 倍阈值后复位去重标记。
_SCRIPTED_WALKER_CONTACT_M = 1.5

#: 行人速度物理范围（m/s）：平台下发的 crossing_speed 可能高达 3+ m/s，
#: 不能沿用旧版 1.4 m/s 硬上限，否则鬼探头会「慢动作」。
_WALKER_MIN_SPEED = 0.3
_WALKER_MAX_SPEED = 4.0

#: sudden_cross（鬼探头/突然窜出）语义参数：行人先在路缘等待，自车水平距离
#: 近到 ``_SUDDEN_CROSS_TRIGGER_M`` 内才以 ``_SUDDEN_CROSS_DASH_SPEED_MS`` 快速窜
#: 过车道——起步即匀速横穿会把「突然」这一关键时机抹掉，也让自车来不及反应。
#: 窜入速度略高于常规步速上限，突出「突然冲出」而非「慢悠悠过街」。
_SUDDEN_CROSS_TRIGGER_M = 15.0
_SUDDEN_CROSS_DASH_SPEED_MS = 4.5

#: 横穿终点需越出路缘后保留的安全余量（米），避免行人 done 后杵在行车道上，
#: 触发 Traffic Manager 持续刹车、把自车卡在低速。
_CURB_CLEARANCE_M = 3.0
#: 无法读取地图车道信息时，几何估算横穿终点的兜底距离（米）。
_CROSSING_FALLBACK_OFFSET_M = 16.0
#: 逐点探测路面时用 ``get_waypoint(project_to_road=True)``：投影后落点与原探测点
#: 偏离超过该阈值（米）即视为已离开行车道（路缘外）。
_ROAD_SNAP_TOL_M = 2.5

# ── V3.8.0 确定性纵向控制（SIL 直道自车驱动）─────────────────────────────
# 现场发现 Traffic Manager 在同步模式下对自车速度环存在油门/刹车极限环（净速度
# 仅 ~0.7 m/s、IMU 纵向 ±15~22 m/s²），且与行人远近无关。直道场景改用确定性纵向
# 控制（P + 巡航前馈）绕开 TM 速度环；弯道/复杂路况仍交 TM。
#: 判定「直道」的前向探测距离（米）：沿车道 waypoint 逐段累计航向变化。
_EGO_STRAIGHT_LOOKAHEAD_M = 40.0
#: 直道探测步长（米）。
_EGO_STRAIGHT_STEP_M = 4.0
#: 累计航向变化超过该角度（度）即视为弯道，保留 Traffic Manager。
_EGO_STRAIGHT_MAX_HEADING_DEG = 8.0
#: V3.8.3：确定性控制车道保持（pure-pursuit）的前视距离（米）。
_EGO_LANE_LOOKAHEAD_M = 10.0
#: 车道保持自行车模型轴距（米）与 steer=1 对应的最大前轮转角（弧度）。
_EGO_LANE_WHEELBASE_M = 2.9
_EGO_LANE_MAX_STEER_RAD = 0.7
#: 车道保持转向重算间隔（秒）：降低 get_waypoint RPC 频率，间隔内保持上次转向。
_EGO_LANE_STEER_INTERVAL_S = 0.1
#: V3.8.6：物理卡滞判定窗口（秒）：油门高却几乎不动持续超此时长→停油保持制动。
_EGO_PHYS_STUCK_S = 3.0
#: V3.8.8：卡滞 latch 恢复探测——原实现 latch 后施加 brake=_EGO_BRAKE_HOLD 使车速
#: 恒 <0.1，复位条件「throttle≤0.5 或 current≥0.1」被自身制动阻断，形成自锁死锁
#: （实机 SCN-2085 动物已离开路径后仍冻结 214s）。改为周期探测状态机：每
#: PROBE_INTERVAL_S 释放制动给 PROBE_THROTTLE 探 PROBE_S，速度达 CLEAR_MS 即解除。
_EGO_PHYS_STUCK_PROBE_S = 1.0
_EGO_PHYS_STUCK_PROBE_INTERVAL_S = 5.0
_EGO_PHYS_STUCK_PROBE_THROTTLE = 0.6
#: 解除卡滞的前向速度阈值（m/s）。与 latch 阈值（_EGO_PHYS_STUCK_LATCH_MS）拉开
#: 区间形成滞回，避免「动了 0.05→解除→又判卡滞」高频抖动。
_EGO_PHYS_STUCK_CLEAR_MS = 0.3
#: V3.8.8：卡滞 latch 判定速度阈值（m/s），沿用 V3.8.6 的 current<0.1 语义。
_EGO_PHYS_STUCK_LATCH_MS = 0.1
#: 确定性速度控制：比例增益 / 巡航前馈油门 / 制动增益 / 稳态死区（m/s）。
_EGO_SPEED_KP = 0.6
_EGO_SPEED_FEEDFORWARD = 0.12
_EGO_BRAKE_GAIN = 0.8
_EGO_SPEED_DEADBAND = 0.15
#: 确定性控制收到事件制动且已减速到目标后的保持制动（防溜车，0~1）。
_EGO_BRAKE_HOLD = 0.3
#: V3.8.1：平台事件动作中判定为「驱动自车减速」的 action_type 关键字
#: （子串匹配，覆盖 brake / emergency_brake / emergency_stop / decelerate / stop）。
_EGO_BRAKE_ACTION_KEYWORDS = ("brake", "stop", "decelerate")
#: V3.8.5：平台事件动作中判定为「改变自车期望速度」的 action_type 关键字
#: （子串匹配，覆盖 speed_change / change_speed / set_speed / adjust_speed）。
_EGO_SPEED_CHANGE_KEYWORDS = ("speed_change", "change_speed", "set_speed", "adjust_speed")
#: V3.8.7：平台 V1.27.0+ 事件动作语义标记——动作是评估基线而非指令，
#: 替自车执行期望动作会使评估 oracle 恒真。按标记分支留痕，不改变现有行为。
_ACTION_SEMANTICS_EXPECTATION = "expectation_only"
#: V3.8.2：事件制动的「在路径内」横向半宽（米）。只有参与者落在自车行驶路径
#: （前方且 |横向| ≤ 该值）时才真正制动；等待型横穿（sudden_cross）伏在路缘
#: （横向数米）时不制动，避免「自车刹停 ↔ 行人等自车靠近」互锁死锁。
_EGO_BRAKE_LANE_HALF_M = 2.0

#: V3.8.4：事件制动释放滞回半宽（米）。制动一旦生效（锁存），需危险物横向越过
#: 该更宽阈值或落到自车后方才释放，避免横穿物在 2m 边界来回穿越导致
#: 「制动↔巡航」高频切换（画面抖动 + 近距擦碰）。
_EGO_BRAKE_RELEASE_HALF_M = 3.0

#: V3.8.10：事件制动最长保持时长（秒）兜底默认值。脚本化横穿（如 deer 回退为
#: pedestrian）可能被刹停的自车物理楔住、永远走不出行驶路径，导致滞回锁存恒真、
#: 自车整场 0 m/s 冻结（掩盖真实评估结果）。连续「在路径内」制动超过该时长即
#: 一次性强制释放恢复巡航，打破死锁；正常横穿（数秒内离开路径）计时每 tick 复位，
#: 不会误触。可用 ``HUNTER_SIM_EGO_EVENT_BRAKE_MAX_HOLD_S`` 覆盖（<=0 关闭兜底=旧行为）。
_EGO_EVENT_BRAKE_MAX_HOLD_S_DEFAULT = 15.0
_EGO_EVENT_BRAKE_MAX_HOLD_S_ENV = "HUNTER_SIM_EGO_EVENT_BRAKE_MAX_HOLD_S"


def _ego_event_brake_max_hold_s() -> float:
    """事件制动最长保持时长（秒）；env 覆盖，非法/空回退默认，<=0 表示关闭兜底。"""
    raw = os.getenv(_EGO_EVENT_BRAKE_MAX_HOLD_S_ENV, "").strip()
    if not raw:
        return _EGO_EVENT_BRAKE_MAX_HOLD_S_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _EGO_EVENT_BRAKE_MAX_HOLD_S_DEFAULT


#: V3.8.4：确定性控制输出（油门/刹车/转向）一阶低通系数，抑制 bang-bang 切换
#: 引起的悬架俯仰/侧倾阶跃（环视相机刚性挂自车，表现为画面抖动）。
_EGO_CTRL_EMA_ALPHA = 0.35
#: V3.8.4：最小车距采样上限（防超长场景内存膨胀）。
_MAX_MIN_DISTANCE_SAMPLES = 20000

#: 事件度量纵向加速度的一阶低通系数（与面板/遥测口径一致）。
_ACCEL_EMA_ALPHA = 0.5

#: 场景行人是否使用 CARLA 原生 ``controller.ai.walker`` 导航（默认关闭）。
#: 场景行人的出生点与横穿目标是平台下发的任意坐标，不保证落在
#: RecastNavigation 导航网格上；对 off-mesh 点调用 go_to_location 会触发
#: CARLA 原生深层递归 → Windows 栈溢出（0xC00000FD / code=3221225725），
#: Python 无法捕获，直接带走控制面进程。置 1 仅在确认坐标都在导航网格上时启用。
_WALKER_AI_CONTROLLER_ENV = "SCENE_WALKER_AI_CONTROLLER"


def _walker_ai_controller_enabled() -> bool:
    """场景行人是否启用原生 AI 导航控制器（默认否，走脚本化横穿）。"""
    return os.getenv(_WALKER_AI_CONTROLLER_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# 仿真传感器类型 → FrameHub 流名（事件传感器不在其中）
_SIM_STREAM_BY_TYPE: dict[SensorType, str] = {
    SensorType.LIDAR: "lidar",
    SensorType.RGB_CAMERA: "rgb",
    SensorType.DEPTH_CAMERA: "depth",
    SensorType.IMU: "imu",
}

# 每点字节数（x/y/z/intensity 均为 float32）
_LIDAR_POINT_BYTES = 16

# 各流的坐标系 ID（与 ROS 话题约定一致）
_FRAME_IDS: dict[str, str] = {
    "lidar": "lidar_top",
    "rgb": "camera_front",
    "depth": "camera_depth",
    "imu": "imu_link",
}

# 传感器帧编码队列容量：满载丢帧，绝不阻塞 CARLA tick
_SENSOR_QUEUE_MAXSIZE = 32
# 编码线程停止时等待排空/退出的超时（秒）
_SENSOR_WORKER_JOIN_TIMEOUT_S = 1.0
# 帧失败告警限频窗口（秒）：每流首次立即报，之后窗口内聚合成一条
_SENSOR_ERROR_LOG_INTERVAL_S = 30.0


@dataclass
class _ScriptedWalker:
    """脚本化行人（walker AI 控制器不可用时的兜底）。

    CARLA 的 ``controller.ai.walker`` 依赖蓝图库与 TM 状态，服务端缺资源时
    行人会原地不动，可视化里就只看到自车在跑。兜底方案是每个 tick 按速度把
    行人直行推向横穿目标点，保证画面里始终能看到「行人过街」这一行为。

    Attributes:
        actor: CARLA walker Actor。
        destination: 目标点 ``(x, y, z)``。
        speed_ms: 直行速度（m/s）。
        carla_module: 已导入的 carla 模块（构造 Transform/Location/Rotation）。
        spawn: 行人登记时的真实出生点（用于识别首 tick 前的占位原点读数）。
        done: 是否已到达/放弃（到达后不再抖动）。
        mode: ``"cross"`` 匀速横穿；``"dash"`` sudden_cross 时机化窜入。
        waiting: ``dash`` 模式下是否仍在等待自车接近（未到触发距离前不移动）。
        trigger_distance_m: ``dash`` 模式下自车水平距离小于该值才发起窜入。
        dash_speed_ms: ``dash`` 窜入阶段的速度（>0 时覆盖 speed_ms）。
        contact_reported: V3.8.18 是否已上报过与自车的接触碰撞（去重，
            物理禁用后碰撞传感器不再触发，改由距离接触判定补报一次）。
    """

    actor: Any
    destination: tuple[float, float, float]
    speed_ms: float
    carla_module: Any
    spawn: SpawnPoint
    done: bool = False
    mode: str = "cross"
    waiting: bool = False
    trigger_distance_m: float = 0.0
    dash_speed_ms: float = 0.0
    contact_reported: bool = False


@dataclass(frozen=True)
class _SensorFrame:
    """CARLA 回调线程 → 编码线程的帧载荷。

    只装已拷贝的基本类型（bytes / 尺寸 / 数值元组），不跨线程持有 CARLA 对象：
    CARLA 的测量对象生命周期属于回调，不能假定在其他线程里仍然有效。
    """

    stream: str
    ts: float
    payload: bytes = b""
    width: int = 0
    height: int = 0
    accel: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gyro: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def as_measurement(self) -> Any:
        """还原为 converter / ROS 桥可直接读取的轻量对象。"""
        return SimpleNamespace(
            data=self.payload,
            width=self.width,
            height=self.height,
            timestamp=self.ts,
            accelerometer=self.accel,
            gyroscope=self.gyro,
        )


class BuiltinScenario(CustomScenarioBase):
    """由 SceneConfig 驱动的最小可用场景执行器。

    Args:
        config: 已验证的场景配置。
        state_manager: 场景状态机（由 SceneRunnerService 持有）。
        event_detector: 事件检测器（由 SceneRunnerService 持有）。
        transform_factory: 生成位姿工厂，默认惰性导入 carla 构造 Transform；
            单元测试可注入桩实现以脱离 CARLA SDK。
        frame_hub: 传感器帧汇聚中心（控制面 MJPEG/截图取用）；None 则不回传画面。
        recorder: mcap 录制器；None 则不落盘。
        ros_bridge: ROS2 桥接器；None 或不可用时不发布话题。
        screenshot_quality: JPEG 质量（Pillow 可用时生效）。
        stream_max_width: 回传画面最大宽度（0 表示不降采样）。
    """

    def __init__(
        self,
        config: SceneConfig,
        state_manager: SceneStateManager,
        event_detector: EventDetector,
        transform_factory: Optional[TransformFactory] = None,
        frame_hub: Optional[FrameHub] = None,
        recorder: Optional[McapRecorder] = None,
        ros_bridge: Optional[Any] = None,
        screenshot_quality: int = 75,
        stream_max_width: int = 0,
        traffic_manager: Optional[Any] = None,
        traffic_manager_port: int = _AUTOPILOT_TM_PORT,
    ) -> None:
        super().__init__(config, state_manager, event_detector)
        self._transform_factory = transform_factory
        self._frame_hub = frame_hub
        self._recorder = recorder
        self._ros_bridge = ros_bridge
        self._screenshot_quality = screenshot_quality
        self._stream_max_width = stream_max_width
        # Traffic Manager：同步模式下 autopilot 车辆是否移动取决于 TM 是否同步
        self._traffic_manager = traffic_manager
        self._traffic_manager_port = int(traffic_manager_port)
        # 由 config.sensors 在挂载时确定（任一 spec 开启 ros 即发布）
        self._ros_enabled = False
        # 传感器帧异步编码：CARLA tick 线程只做拷贝+入队
        self._frame_queue: "queue.Queue[_SensorFrame]" = queue.Queue(
            maxsize=_SENSOR_QUEUE_MAXSIZE
        )
        self._frame_worker: Optional[threading.Thread] = None
        self._frame_worker_lock = threading.Lock()
        self._frame_stop = threading.Event()
        # 待处理帧计数（含已从队列取出但尚未编码完成的），供 drain 精确等待
        self._frame_cond = threading.Condition()
        self._frame_pending = 0
        self._frame_accepting = True
        # 帧失败告警限频状态（按流名，跨回调/编码线程访问）
        self._frame_error_lock = threading.Lock()
        self._frame_error_at: dict[str, float] = {}
        self._frame_error_suppressed: dict[str, int] = {}
        self._ego: Any = None
        # 自车实际生成位姿（原点替换/兜底后），参与者相对偏移以它为基准
        self._ego_spawn: Optional[SpawnPoint] = None
        self._actors: list[Any] = []
        self._sensors: list[Any] = []
        # 仅可视化用环视相机：不进入 _sensors 计数，不录 mcap / 不发 ROS
        self._overview_sensor: Any = None
        # 环视相机独立编码队列/线程（防止被数据传感器队列饿死）
        self._overview_queue: "queue.Queue[_SensorFrame]" = queue.Queue(
            maxsize=_OVERVIEW_QUEUE_MAXSIZE
        )
        self._overview_worker: Optional[threading.Thread] = None
        self._overview_worker_lock = threading.Lock()
        self._overview_stop = threading.Event()
        # 慢编码告警限频状态（仅环视编码线程访问）
        self._overview_slow_at = 0.0
        self._overview_slow_suppressed = 0
        # 编码自适应降质状态（仅环视编码线程读写，不需加锁）
        self._overview_quality_index = 0
        self._overview_slow_streak = 0
        self._overview_fast_streak = 0
        self._overview_floor_at = 0.0
        # 场景化行为控制：参与者 ID → 控制器；行人 AI 控制器单独登记
        self._participant_controllers: list[ScriptedActorController] = []
        self._walker_controllers: list[WalkerControllerWrapper] = []
        self._walker_ai_controllers: list[Any] = []
        # 行人 AI 不可用时的脚本化横穿兜底（保证可视化可见）
        self._scripted_walkers: list[_ScriptedWalker] = []
        self._cut_in_done: set[int] = set()
        # 自车非 autopilot 时的简单速度保持
        self._ego_manual_speed: float = 0.0
        self._ego_initial_steer: float = 0.0
        # V3.8.0：直道确定性纵向控制是否接管自车（解除 TM 托管）
        self._ego_deterministic: bool = False
        # V3.8.3：setup 注入的世界句柄（车道保持取 waypoint 用）与转向缓存
        self._world: Any = None
        self._ego_lane_steer: float = 0.0
        self._ego_lane_steer_age_s: float = 0.0
        # V3.8.4：事件制动滞回锁存 + 控制输出低通状态 + 最小车距采样（评估用）。
        self._ego_brake_latched: bool = False
        self._ego_ctrl_throttle: float = 0.0
        self._ego_ctrl_brake: float = 0.0
        self._ego_ctrl_steer: float = 0.0
        # V3.8.6：物理卡滞保护计时与告警标志（油门高却不动→停油保持制动）。
        self._ego_phys_stuck_since_s: float = 0.0
        self._ego_phys_stuck_warned: bool = False
        # V3.8.8：卡滞 latch 恢复探测状态（latch 时刻 / 下次探测时刻 / 探测窗口截止）。
        self._ego_phys_stuck_latched_at_s: float = 0.0
        self._ego_phys_stuck_next_probe_at_s: float = 0.0
        self._ego_phys_stuck_probe_until_s: float = 0.0
        self.min_distance_samples: list[float] = []
        # V3.8.18：传感器诊断——每流编码队列丢帧计数与标称帧率（面板展示
        # 实际/标称 fps 与丢帧数，定位「fps 低于标称」是丢帧还是 tick 上限）。
        self._frame_drops: dict[str, int] = {}
        self._nominal_fps: dict[str, float] = {}
        # V3.8.1：事件驱动的自车减速目标速度（m/s）；None=未触发。仅确定性控制下生效，
        # 由 ev-*.action_type=brake 越沿触发置位，使直道绕开 TM 的自车能响应前车/窜出物制动。
        self._ego_event_brake_ms: Optional[float] = None
        # V3.8.10：事件制动连续「在路径内」起始时刻（None=当前未在持续制动）与
        # 超时强制释放标记（本次武装内不再回检，避免与楔住的横穿物反复制动↔巡航）。
        self._ego_event_brake_since_s: Optional[float] = None
        self._ego_event_brake_hold_expired: bool = False
        # 已行驶里程积分（平台事件 traveled_distance 用）
        self._traveled_distance_m: float = 0.0
        # 车类 prop（停放/事故/故障车）生成位置，boarding 行为据此选择目标
        self._parked_spawns: list[SpawnPoint] = []
        # 参与者 ID → 已生成 Actor / 实际出生点：可视化「参与者」面板与
        # 「生成了但拍不到」排障都靠它按 ID 回查
        self._actor_by_participant: dict[str, Any] = {}
        self._participant_spawns: dict[str, SpawnPoint] = {}
        # TM 托管的参与者 (actor, 期望速度)：同步模式下首 tick 前下发的
        # set_autopilot / set_desired_speed 会被 TM 静默丢弃，需首 tick 后补发
        self._tm_managed: list[tuple[Any, float]] = []
        self._post_tick_done = False
        # 事件度量纵向加速度估计器（前向速度的时间导数 + 一阶低通）：
        # 与面板/遥测口径一致，避免 get_acceleration() 瞬时抖动误触 emergency_brake
        self._accel_prev_fwd: Optional[float] = None
        self._accel_ema: float = 0.0
        # SIL 现场可观测性：自车长期远低于期望速度时定位“卡低速”原因
        self._slow_since_s: float = 0.0
        self._slow_warned_at_s: float = 0.0
        # 「跟车条件不成立」告警限频时间戳
        self._no_lead_warned_at = 0.0
        self._cleaned = False

    @property
    def ego_actor(self) -> Any:
        """自车 Actor（未生成时为 None）。"""
        return self._ego

    @property
    def spawned_actor_ids(self) -> list[int]:
        """本场景生成的全部 Actor ID（自车 + 交通参与者）。"""
        ids: list[int] = []
        for actor in ([self._ego] if self._ego is not None else []) + self._actors:
            actor_id = getattr(actor, "id", None)
            if actor_id is not None:
                ids.append(int(actor_id))
        return ids

    @property
    def sensor_diagnostics(self) -> dict[str, Any]:
        """传感器诊断快照（V3.8.18：每流丢帧计数与标称帧率）。

        供可视化面板展示「实际/标称 fps」与丢帧数，定位 fps 低于标称
        是编码队列丢帧还是仿真 tick 上限。只读拷贝，不持 CARLA 句柄。
        """
        return {
            "dropped": dict(self._frame_drops),
            "nominal_fps": dict(self._nominal_fps),
        }

    def participant_states(self) -> list[dict[str, Any]]:
        """交通参与者状态快照（可视化「参与者」面板数据源）。

        平台下发的每个参与者都返回一条记录：身份 / 类型 / 期望行为 + 实时位姿，
        并附带车体系偏移 ``ahead_m``（前正后负）与 ``left_m``（左正右负）。
        ``source_behavior_type`` 是平台原始行为标签（如 ``sudden_cross``）：
        内部 ``behavior`` 是映射后的执行行为（如 ``pedestrian_cross``），面板
        行为标签须回显平台原值，否则现场无法与下发配置对齐（V3.8.7）。

        只给世界坐标是不够的：自车基准 yaw 常被替换成地图出生点航向（Town01
        常见 90°），此时「正前方 18m」在世界系里表现为 y 方向 +18m，无法判断
        「在前还是在右」。``visible`` 是环视主视区的粗估结论，用于快速定位
        「参与者生成了但画面里看不到」。

        仅由 tick 线程调用（读取 CARLA Actor 位姿属 RPC，非线程安全），结果由
        ``SceneRunnerService`` 缓存后供 executor / HTTP 线程读取。
        """
        base = self._reference_pose()
        states: list[dict[str, Any]] = []
        for participant in self._config.traffic_participants:
            actor = self._actor_by_participant.get(participant.participant_id)
            spawned = self._participant_spawns.get(participant.participant_id)
            pose = self._actor_spawn_point(actor)
            if pose is not None and _pose_not_synced(pose, spawned):
                pose = None  # 首 tick 前的占位读数，不是参与者真实位置
            if pose is None:
                pose = spawned
            state: dict[str, Any] = {
                "id": participant.participant_id,
                "actor_type": participant.actor_type,
                "behavior": str(
                    getattr(participant.behavior, "value", participant.behavior)
                ),
                "source_behavior_type": participant.source_behavior_type,
                "blueprint": getattr(actor, "type_id", participant.blueprint),
                "spawned": actor is not None,
                "speed_ms": (
                    round(self._participant_speed_ms(actor), 2)
                    if actor is not None
                    else 0.0
                ),
                "x": None,
                "y": None,
                "z": None,
                "yaw_deg": None,
                "ahead_m": None,
                "left_m": None,
                "distance_to_ego_m": None,
                "visible": False,
            }
            if pose is not None:
                ahead, left = relative_offset_from_ego(base, pose)
                state.update(
                    {
                        "x": round(pose.x, 2),
                        "y": round(pose.y, 2),
                        "z": round(pose.z, 2),
                        "yaw_deg": round(pose.yaw_deg, 1),
                        "ahead_m": round(ahead, 2),
                        "left_m": round(left, 2),
                        "distance_to_ego_m": round(math.hypot(ahead, left), 2),
                        "visible": self._estimate_participant_visible(ahead, left),
                    }
                )
            states.append(state)
        return states

    # ── CustomScenarioBase 实现 ───────────────────────────────────────────

    def setup(self, world: Any) -> None:
        """生成自车、交通参与者、事件传感器与仿真传感器。

        V3.6.0：逐阶段落 INFO 锚点。原生崩溃（``0xC00000FD``/``0xC0000409``）不留
        traceback，只能靠「最后一条日志是什么」判定崩在哪一步；旧版只在生成
        成功后打日志，崩溃时刚好看不到正在执行的那一次 ``spawn_actor``。
        """
        if self._ego is not None:
            # 同一 runner 重复下发场景：先回收上一场景 Actor，避免残影
            self.cleanup(world)
        self._cleaned = False
        self._frame_accepting = True
        self._participant_controllers.clear()
        self._walker_controllers.clear()
        self._walker_ai_controllers.clear()
        self._scripted_walkers.clear()
        self._cut_in_done.clear()
        self._ego_manual_speed = 0.0
        self._ego_initial_steer = 0.0
        self._ego_deterministic = False
        self._ego_event_brake_ms = None
        self._ego_event_brake_since_s = None
        self._ego_event_brake_hold_expired = False
        self._world = world
        self._ego_lane_steer = 0.0
        self._ego_lane_steer_age_s = 0.0
        self._ego_brake_latched = False
        self._ego_ctrl_throttle = 0.0
        self._ego_ctrl_brake = 0.0
        self._ego_ctrl_steer = 0.0
        self._ego_phys_stuck_since_s = 0.0
        self._ego_phys_stuck_warned = False
        self._ego_phys_stuck_latched_at_s = 0.0
        self._ego_phys_stuck_next_probe_at_s = 0.0
        self._ego_phys_stuck_probe_until_s = 0.0
        self.min_distance_samples = []
        self._frame_drops = {}
        self._nominal_fps = {}
        self._traveled_distance_m = 0.0
        self._parked_spawns.clear()
        self._actor_by_participant.clear()
        self._participant_spawns.clear()
        self._tm_managed.clear()
        self._post_tick_done = False
        self._accel_prev_fwd = None
        self._accel_ema = 0.0
        self._slow_since_s = 0.0
        self._slow_warned_at_s = 0.0
        self._no_lead_warned_at = 0.0

        library = self._blueprint_library(world)
        logger.info(
            f"scenario_setup stage=blueprints scene={self._config.scene_id} "
            f"participants_declared={len(self._config.traffic_participants)}"
        )
        self._ego = self._spawn_ego(world, library)
        self._collect_parked_spawns()
        requested = list(self._config.traffic_participants)
        spawned_ids: list[str] = []
        for index, participant in enumerate(requested, start=1):
            logger.info(
                f"scenario_setup stage=participant_begin index={index}/{len(requested)} "
                f"id={participant.participant_id} "
                f"type={getattr(participant, 'actor_type', 'unknown')} "
                f"behavior={getattr(participant, 'behavior', 'unknown')}"
            )
            if self._spawn_participant(world, library, participant):
                spawned_ids.append(participant.participant_id)
        logger.info(
            f"scenario_setup stage=sensors spawned={len(spawned_ids)}/{len(requested)}"
        )
        self._attach_event_sensors(world, library)
        self._attach_sim_sensors(world, library)
        logger.info("scenario_setup stage=motion")
        self._configure_ego_motion(world)

        logger.info(
            f"Builtin scenario '{self._config.scene_id}' ready: "
            f"ego={getattr(self._ego, 'id', 'n/a')}, "
            f"actors={len(self._actors)}/{len(requested)}, "
            f"sensors={len(self._sensors)}"
        )
        if len(self._actors) < len(requested):
            missing = [
                participant.participant_id
                for participant in requested
                if participant.participant_id not in spawned_ids
            ]
            logger.warning(
                "Builtin scenario '%s' 参与者未全部生成：%d/%d，未生成=%s",
                self._config.scene_id,
                len(self._actors),
                len(requested),
                missing or "未知",
            )
        self._log_scene_expectations()

    def _log_scene_expectations(self) -> None:
        """把「场景期望」落成日志：参与者期望行为 + 事件期望动作。

        平台事件上的 ``action_type``（如「前车紧急制动」）自 V3.8.1 起会在
        直道确定性纵向控制下驱动自车减速（见 :meth:`_apply_scene_event_actions`）；
        TM 托管时仍只做期望留档（避让由 TM 负责）。现场看到「期望刹车但前车
        没减速」时，可从这行日志确认驱动方式与是否属契约范围。

        同时打车体系 ``offset`` 与叠加后的 ``world``：只打一个时无法区分「下发错」
        与「算错」，旧版把 offset 印成 ``@(18.5,0.0)`` 更易被误读成世界坐标。
        """
        participants: list[str] = []
        for item in self._config.traffic_participants:
            spawn = self._participant_spawns.get(item.participant_id)
            world_text = (
                f"world=({spawn.x:.1f},{spawn.y:.1f},yaw={spawn.yaw_deg:.0f})"
                if spawn is not None
                else "world=未生成"
            )
            participants.append(
                f"{item.participant_id}[{item.actor_type}/"
                f"{getattr(item.behavior, 'value', item.behavior)}"
                f" speed={item.speed_ms:.1f}m/s"
                f" offset=({item.spawn_point.x:.1f},{item.spawn_point.y:.1f})"
                f" {world_text}]"
            )
        logger.info(
            "Scene expectations: participants=%d %s",
            len(participants),
            " ".join(participants) if participants else "无（场景无交通参与者）",
        )
        actions = [
            f"{event.event_id}:"
            f"{getattr(event.event_type, 'value', event.event_type)}"
            f"->{event.action_type}"
            for event in self._config.scene_events
            if event.action_type
        ]
        if actions:
            logger.info(
                "Scene event actions: %s（V3.8.1：brake/emergency_stop 类在确定性纵向"
                "控制下驱动自车减速；TM 托管时仅留存，避让由 TM 负责）",
                " ".join(actions),
            )
        # V3.8.7：事件动作语义标记落日志（平台 V1.27.0+ 顶层 action_semantics）：
        # 现场据此区分「期望基线」与「指令」，不再按平台版本猜。
        semantics = str(self._config.action_semantics or "").strip().lower()
        if semantics == _ACTION_SEMANTICS_EXPECTATION:
            logger.info(
                "Scene action semantics: expectation_only（事件动作是评估基线而非指令；"
                "现有确定性控制接管行为不变，仅分支留痕）"
            )
        elif semantics:
            logger.info(
                "Scene action semantics: %s（未识别标记，按历史行为处理）", semantics
            )
        else:
            logger.info(
                "Scene action semantics: 未声明（旧平台载荷，按历史行为处理）"
            )

    def tick(self, world: Any, timestamp: float, delta_seconds: float) -> None:
        """每帧推进自车与交通参与者行为。

        车辆由 Traffic Manager autopilot 保持车道内行驶；本方法在每个 tick
        把场景化行为（减速 / 切入 / 停车等）翻译成 TM 期望速度或手动控制，
        使可视化里自车与参与者真正按场景设置运动。行人由 AI 控制器导航。
        """
        self._reapply_post_spawn_controls_once()
        self._apply_ego_manual_speed(delta_seconds)
        self._advance_scripted_walkers(delta_seconds)
        self._update_traveled_distance(delta_seconds)
        self._warn_ego_speed_stuck()
        for controller in list(self._participant_controllers):
            try:
                action = controller.update(self._ego, delta_seconds)
            except Exception as exc:  # noqa: BLE001  单个参与者异常不阻断场景
                logger.debug(f"Participant behavior update failed: {exc}")
                continue
            if action is None:
                continue
            try:
                self._apply_participant_action(controller, action)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Participant behavior apply failed: {exc}")

        if self._config.extra_params.get("disable_scene_event_triggers"):
            return
        try:
            metrics = self._trigger_metrics(world, timestamp, delta_seconds)
            fired = self._event_detector.update_scene_triggers(timestamp, metrics)
            self._apply_scene_event_actions(fired)
        except Exception as exc:  # noqa: BLE001  事件评估失败不阻断仿真
            logger.debug(f"Scene trigger evaluation failed: {exc}")

    def check_finish(self, world: Any, timestamp: float) -> bool:
        """运行时长达到 ``duration_seconds`` 时结束场景。"""
        return self.elapsed_seconds >= self._config.duration_seconds

    def cleanup(self, world: Any) -> None:
        """销毁本场景生成的全部 Actor 与传感器（幂等）。

        V3.4.4 起每个阶段都有 INFO 日志（含待销毁数量）：CARLA 原生销毁偶发
        fail-fast（进程直接退出，traceback 无法落盘），崩溃后可以由最后一条日志
        定位到具体阶段，避免再“场景结束后无声退出”。
        """
        if self._cleaned:
            return
        self._cleaned = True

        logger.info(
            "Scenario cleanup begin: sensors=%d, overview=%s, participants=%d, "
            "ego=%s",
            len(self._sensors),
            self._overview_sensor is not None,
            len(self._actors),
            self._ego is not None,
        )

        # 先关回调入口并排空编码队列，再销毁传感器（避免后续帧写已关闭的录制器）
        self._frame_accepting = False
        logger.info("Scenario cleanup: stopping encoder threads")
        self._stop_frame_worker()
        self._stop_overview_worker()

        # 停止行人导航/控制器
        logger.info("Scenario cleanup: stopping participant controllers")
        for wrapper in self._walker_controllers:
            try:
                wrapper.stop_navigation()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Walker stop failed: {exc}")
        self._walker_controllers.clear()
        for controller in self._walker_ai_controllers:
            try:
                stop = getattr(controller, "stop", None)
                if callable(stop):
                    stop()
                destroy = getattr(controller, "destroy", None)
                if callable(destroy):
                    destroy()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Walker AI controller destroy failed: {exc}")
        self._walker_ai_controllers.clear()
        self._scripted_walkers.clear()
        self._participant_controllers.clear()
        self._cut_in_done.clear()

        logger.info("Scenario cleanup: destroying sensors")
        for sensor in (
            ([self._overview_sensor] if self._overview_sensor is not None else [])
            + self._sensors
        ):
            try:
                stop = getattr(sensor, "stop", None)
                if callable(stop):
                    stop()
                destroy = getattr(sensor, "destroy", None)
                if callable(destroy):
                    destroy()
            except Exception as exc:  # noqa: BLE001  传感器可能已随世界销毁
                logger.debug(f"Sensor stop failed: {exc}")
        self._sensors.clear()
        self._overview_sensor = None

        logger.info(
            "Scenario cleanup: destroying actors (ego=%s, participants=%d)",
            self._ego is not None,
            len(self._actors),
        )
        # V3.8.5：销毁前先解除 TM 托管。TM 仍登记着这些 Actor 时直接 destroy，
        # TM 内部状态与销毁 RPC 并发会触发原生 fail-fast（0xC0000409）。
        logger.info("Scenario cleanup: releasing Traffic Manager control")
        for actor in ([self._ego] if self._ego is not None else []) + self._actors:
            set_autopilot = getattr(actor, "set_autopilot", None)
            if callable(set_autopilot):
                try:
                    set_autopilot(False)
                except Exception as exc:  # noqa: BLE001  Actor 可能已失效
                    logger.debug(f"TM release failed: {exc}")
        self._tm_managed.clear()
        for actor in ([self._ego] if self._ego is not None else []) + self._actors:
            try:
                actor.destroy()
            except Exception as exc:  # noqa: BLE001  Actor 可能已被外部销毁
                logger.debug(f"Actor destroy failed: {exc}")
        self._ego = None
        self._ego_spawn = None
        self._world = None
        self._ego_brake_latched = False
        self._ego_event_brake_since_s = None
        self._ego_event_brake_hold_expired = False
        # V3.8.6：min_distance_samples 不在 cleanup 清空——评估在 cleanup 之后
        # 读取（executor._evaluate），清空会让最小车距=inf→近距擦碰假通过。
        # 新场景由 setup() 重置。
        self._actors.clear()
        self._actor_by_participant.clear()
        self._participant_spawns.clear()
        logger.info("Scenario cleanup done")

    # ── 内部实现 ─────────────────────────────────────────────────────────

    @staticmethod
    def _blueprint_library(world: Any) -> Any:
        getter = getattr(world, "get_blueprint_library", None)
        if not callable(getter):
            raise CarlaSimulationError(
                "builtin_scenario.setup",
                "World 不支持 get_blueprint_library()，无法按场景配置生成 Actor",
            )
        return getter()

    def _spawn_ego(self, world: Any, library: Any) -> Any:
        ego_cfg: EgoVehicleConfig = self._config.ego_vehicle
        try:
            blueprint = self._find_blueprint(library, ego_cfg.vehicle_blueprint, "vehicle.")
        except CarlaSimulationError as exc:
            # 标准 CARLA 不带 vehicle.hunter_se 资产：回退到通用车辆，
            # 保证「未部署 HUNTER SE 蓝图」时 REST 场景闭环仍能跑通
            blueprint = self._fallback_vehicle_blueprint(library)
            logger.info(
                f"Ego blueprint '{ego_cfg.vehicle_blueprint}' unavailable, "
                f"fallback to '{getattr(blueprint, 'id', blueprint)}': {exc}。"
                "如需实车尺寸请安装 vehicle.hunter_se.* 蓝图（挂载仍按实际包围盒换算）"
            )
        actor: Any = None
        actual_spawn: Optional[SpawnPoint] = None
        candidates = resolve_ego_spawn_candidates(
            world,
            ego_cfg.spawn_point,
            map_type=self._config.map_type,
            scene_key=scene_key_of(self._config.scene_id, self._config.scene_version),
            variant_index=self._spawn_variant_index(),
        )
        for index, candidate in enumerate(candidates):
            transform = self._make_transform(world, candidate)
            actor = self._spawn_actor(world, blueprint, transform)
            if actor is not None:
                actual_spawn = candidate
                if index > 0:
                    logger.warning(
                        "ego_spawn_fallback scene=%s attempt=%d spawn=(%.2f,%.2f,%.2f)",
                        self._config.scene_id,
                        index + 1,
                        candidate.x,
                        candidate.y,
                        candidate.z,
                    )
                break
            logger.warning(
                "ego_spawn_attempt_failed scene=%s attempt=%d spawn=(%.2f,%.2f,%.2f) "
                "(位置被占用或不在路面)",
                self._config.scene_id,
                index + 1,
                candidate.x,
                candidate.y,
                candidate.z,
            )

        if actor is None:
            raise CarlaSimulationError(
                "builtin_scenario.spawn_ego",
                (
                    f"自车生成失败（位置被占用或非法）：blueprint={ego_cfg.vehicle_blueprint}, "
                    f"spawn=({ego_cfg.spawn_point.x:.1f}, {ego_cfg.spawn_point.y:.1f}), "
                    f"已尝试 {len(candidates)} 个候选出生点"
                ),
            )
        self._ego_spawn = self._actual_ego_spawn(actor, actual_spawn)
        self._tag_ego_role(actor)
        if ego_cfg.autopilot:
            set_autopilot = getattr(actor, "set_autopilot", None)
            if callable(set_autopilot):
                try:
                    set_autopilot(True, self._traffic_manager_port)
                    logger.info(
                        f"Ego autopilot enabled (Traffic Manager port="
                        f"{self._traffic_manager_port})"
                    )
                except Exception as exc:  # noqa: BLE001  TM 未就绪时降级为手动控制
                    logger.warning(f"Ego autopilot enable failed: {exc}")
        self._ego_initial_steer = float(ego_cfg.initial_steer)
        if self._ego_initial_steer != 0.0:
            if ego_cfg.autopilot:
                logger.warning(
                    "ego_initial_steer_ignored_autopilot: initial_steer=%.3f 在 "
                    "autopilot 模式下会被 Traffic Manager 覆盖",
                    self._ego_initial_steer,
                )
            else:
                self._apply_vehicle_control(
                    actor,
                    throttle=0.0,
                    brake=0.0,
                    steer=self._ego_initial_steer,
                )
                logger.info(
                    "Ego initial steer applied: %.3f (manual control)",
                    self._ego_initial_steer,
                )
        return actor

    def _participant_base(self) -> SpawnPoint:
        """参与者相对偏移的基准：自车**确定性出生位姿** ``_ego_spawn``。

        平台把参与者 ``(x, y)`` 定义为「相对自车」的车体系偏移（x 前向 / y 左向），
        语义锚点是自车出生点，因此这里必须用生成时确定的位姿而不是运行时快照：

        - CARLA 同步模式（本服务默认 50Hz）下，``world.tick()`` 之前新生成的
          Actor ``get_transform()`` 仍返回世界原点 ``(0,0,0)``。V3.4.5~V3.5.2 在此
          优先读实时位姿，于是旋转叠加作用在占位原点上：``offset=(18.5, 0)`` 被
          算成世界坐标 ``(18.5, 0)``，而自车实际出生在 ``(392.5, 212.9, yaw=90)``
          —— 前车落到数百米外且不在路面上，跟车场景完全跑不起来（V3.5.3 修复）。
        - 以出生点为基准还保证同一场景重复下发时参与者布局可复现。

        运行期的相对方位（可视化面板 / 生成日志）用 :meth:`_reference_pose`。
        """
        if self._ego_spawn is not None:
            return self._ego_spawn
        pose = self._ego_live_pose()
        return pose or self._config.ego_vehicle.spawn_point

    def _reference_pose(self) -> SpawnPoint:
        """运行期自车参照位姿：优先实时位姿（未同步时忽略），其次出生点 / 配置。"""
        pose = self._ego_live_pose()
        if pose is not None:
            return pose
        return self._ego_spawn or self._config.ego_vehicle.spawn_point

    def _ego_live_pose(self) -> Optional[SpawnPoint]:
        """自车实时位姿；同步模式首 tick 前的占位读数按「未同步」忽略。"""
        pose = self._actor_spawn_point(self._ego)
        if pose is None:
            return None
        if _pose_not_synced(pose, self._ego_spawn):
            return None
        return pose

    @staticmethod
    def _actor_spawn_point(actor: Any) -> Optional[SpawnPoint]:
        """读取 Actor 当前位姿（Actor 为空 / 已被销毁时返回 None）。"""
        if actor is None:
            return None
        getter = getattr(actor, "get_transform", None)
        if not callable(getter):
            return None
        try:
            return transform_to_spawn_point(getter())
        except Exception as exc:  # noqa: BLE001  Actor 可能已被外部销毁
            logger.debug(f"actor_pose_unavailable: {exc}")
            return None

    @staticmethod
    def _estimate_participant_visible(ahead_m: float, left_m: float) -> bool:
        """粗估参与者是否落在环视主视区内（排障用，非精确投影）。"""
        if ahead_m <= -_OVERVIEW_CAMERA_BACK_M * 0.5:
            return False  # 在自车后方：环视相机朝前，拍不到
        if math.hypot(ahead_m, left_m) > _MAX_VISIBLE_DISTANCE_M:
            return False
        half_width = math.tan(math.radians(_OVERVIEW_HALF_FOV_DEG)) * max(
            1.0, ahead_m + _OVERVIEW_CAMERA_BACK_M
        )
        return abs(left_m) <= half_width

    def _spawn_participant(
        self, world: Any, library: Any, participant: TrafficParticipantConfig
    ) -> bool:
        """生成单个交通参与者，返回是否成功（失败只告警，不阻断场景）。

        可视化验收要求「画面里必须能看到场景声明的参与者」，因此这里不再静默
        跳过：蓝图缺失时按 ``actor_type`` 回退到同类通用蓝图，出生点被占用时
        沿自车航向 / 横向小幅偏移重试；成功后打印参与者 ID / 蓝图 / 坐标与到
        自车的距离，便于从日志判断它是否落在环视相机视野内。
        """
        is_pedestrian = participant.actor_type == "walker"
        is_cyclist = participant.actor_type == "cyclist"
        is_prop = participant.actor_type == "prop"
        if is_prop:
            prefix = (
                "vehicle."
                if participant.blueprint.startswith("vehicle.")
                else "static.prop."
            )
        elif is_cyclist:
            prefix = "vehicle."
        else:
            prefix = "walker." if is_pedestrian else "vehicle."
        blueprint = self._participant_blueprint(library, participant, prefix)
        if blueprint is None:
            logger.warning(
                f"Participant '{participant.participant_id}' spawn skipped: "
                f"蓝图 '{participant.blueprint}' 及同类回退蓝图均不可用"
            )
            return False
        base = self._participant_base()
        try:
            spawn_point = resolve_actor_spawn(
                base, participant.spawn_point, self._config.spawn_semantics
            )
        except Exception as exc:  # noqa: BLE001  坐标非法不影响其他参与者
            logger.warning(
                f"Participant '{participant.participant_id}' spawn skipped: {exc}"
            )
            return False
        if participant.actor_type == "vehicle":
            # 平台只给相对偏移，不知道自车会落在哪个车道：叠加后可能贴到
            # 路肩/隔离带，那种位置的车辆 TM 无法接管（前车不动）
            spawn_point = self._snap_to_driving_lane(
                world, spawn_point, participant.participant_id
            )
        # 平台偏移（车体系）与叠加后的世界坐标一起落日志：只打其中一个时，
        # 现场无法区分「平台偏移下发错」与「仿真端基准/旋转算错」。
        logger.info(
            "Participant '%s' spawn resolve: semantics=%s offset=(%.1f,%.1f,%.1f,yaw=%.0f) "
            "base=(%.1f,%.1f,%.1f,yaw=%.0f) world=(%.1f,%.1f,%.1f,yaw=%.0f)",
            participant.participant_id,
            self._config.spawn_semantics,
            participant.spawn_point.x,
            participant.spawn_point.y,
            participant.spawn_point.z,
            participant.spawn_point.yaw_deg,
            base.x,
            base.y,
            base.z,
            base.yaw_deg,
            spawn_point.x,
            spawn_point.y,
            spawn_point.z,
            spawn_point.yaw_deg,
        )

        actor: Any = None
        actual = spawn_point
        for candidate in self._participant_spawn_candidates(spawn_point):
            try:
                transform = self._make_transform(world, candidate)
                actor = self._spawn_actor(world, blueprint, transform)
            except CarlaSimulationError as exc:
                # 无 CARLA SDK / 变换构造失败：重试无意义，直接报原因
                logger.warning(
                    f"Participant '{participant.participant_id}' spawn skipped: {exc}"
                )
                return False
            if actor is not None:
                actual = candidate
                break
        if actor is None:
            logger.warning(
                f"Participant '{participant.participant_id}' spawn failed: "
                f"全部候选出生点被占用（起点 {spawn_point.x:.1f},{spawn_point.y:.1f}）"
            )
            return False

        self._actors.append(actor)
        self._actor_by_participant[participant.participant_id] = actor
        self._participant_spawns[participant.participant_id] = actual
        self._log_participant_spawned(participant, actor, actual)

        try:
            if is_prop:
                # 车类 prop（事故车/故障车/停放车）显式驻车；static.prop 无控制接口
                if participant.blueprint.startswith("vehicle."):
                    self._park_actor(actor)
                if participant.source_behavior_type == "door_open":
                    self._try_open_door(actor, participant)
                return True
            if is_pedestrian:
                if participant.behavior == ScenarioBehaviorType.STATIC:
                    return True  # 静止行人：不挂 AI 控制器，保持原地
                self._setup_walker_ai(world, library, actor, actual, participant)
                return True
            # vehicle / cyclist：TM autopilot + 场景控制器
            if participant.behavior == ScenarioBehaviorType.STATIC:
                # 静止障碍：显式刹停，避免被 TM 接管后自行开走
                self._park_actor(actor)
                return True
            # 车辆类参与者统一交给 TM autopilot 保持在车道内行驶；
            # 场景化行为再通过每帧期望速度 / 变道指令叠加。
            self._enable_autopilot(actor, participant)
            controller = self._build_participant_controller(actor, participant)
            if controller is not None:
                self._participant_controllers.append(controller)
        except Exception as exc:  # noqa: BLE001  单个参与者行为配置失败不影响场景
            logger.warning(
                f"Participant '{participant.participant_id}' motion setup failed: {exc}"
            )
        return True

    def _snap_to_driving_lane(
        self, world: Any, spawn_point: SpawnPoint, participant_id: str
    ) -> SpawnPoint:
        """车辆类参与者落在非行车道时，投影到最近行车道中心线。

        已在行车道内的坐标一律不动（保留平台意图）；只有原点不在行车道上时才
        投影：落在路肩/中央隔离带的车辆会被 Traffic Manager 忽略，现场表现为
        「前车以不到 1 m/s 抖动甚至完全不动」，跟车场景因此无法验证。
        """
        try:
            carla_map = world.get_map()
            get_waypoint = getattr(carla_map, "get_waypoint", None)
            if not callable(get_waypoint):
                return spawn_point
            import carla  # noqa: PLC0415  仅在真实 CARLA 环境下需要

            location = carla.Location(x=spawn_point.x, y=spawn_point.y, z=spawn_point.z)
            driving = carla.LaneType.Driving
            if (
                get_waypoint(location, project_to_road=False, lane_type=driving)
                is not None
            ):
                return spawn_point
            waypoint = get_waypoint(location, project_to_road=True, lane_type=driving)
        except Exception as exc:  # noqa: BLE001  无 SDK / 地图接口差异时不改写坐标
            logger.debug(f"participant lane snap skipped for {participant_id}: {exc}")
            return spawn_point
        if waypoint is None:
            return spawn_point
        lane_pose = transform_to_spawn_point(waypoint.transform)
        distance = math.hypot(lane_pose.x - spawn_point.x, lane_pose.y - spawn_point.y)
        logger.info(
            "participant_spawn_snapped: participant=%s 原坐标 (%.1f,%.1f,yaw=%.0f) 不在"
            "行车道上（投影偏移 %.1fm），改用行车道中心 (%.1f,%.1f,yaw=%.0f)",
            participant_id,
            spawn_point.x,
            spawn_point.y,
            spawn_point.yaw_deg,
            distance,
            lane_pose.x,
            lane_pose.y,
            lane_pose.yaw_deg,
        )
        return spawn_point.model_copy(
            update={"x": lane_pose.x, "y": lane_pose.y, "yaw_deg": lane_pose.yaw_deg}
        )

    def _park_actor(self, actor: Any) -> None:
        """把车辆类 Actor 显式驻车（含手刹），避免被重力/TM 带着滑动。"""
        self._apply_vehicle_control(actor, throttle=0.0, brake=1.0, steer=0.0)
        setter = getattr(actor, "set_hand_brake", None)
        if callable(setter):
            try:
                setter(True)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"set_hand_brake failed: {exc}")

    def _try_open_door(
        self, actor: Any, participant: TrafficParticipantConfig
    ) -> None:
        """探测并尝试开门；无 API 时只降级为路边停车，不判场景失败。"""
        opener = getattr(actor, "open_door", None)
        if not callable(opener):
            logger.warning(
                "door_api_unavailable: participant=%s actor has no open_door()",
                participant.participant_id,
            )
            return
        try:
            import carla  # noqa: PLC0415

            door = getattr(getattr(carla, "VehicleDoor", None), "FL", 0)
            opener(door, True)
            logger.info("Participant '%s' door opened", participant.participant_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "door_api_unavailable: participant=%s %s",
                participant.participant_id,
                exc,
            )

    def _collect_parked_spawns(self) -> None:
        """记录车类 prop 的生成位置，``boarding`` 行为据此选择最近车辆。"""
        base = self._participant_base()
        for participant in self._config.traffic_participants:
            if participant.object_kind not in (
                "parked_vehicle",
                "accident_vehicle",
                "broken_vehicle",
            ):
                continue
            try:
                self._parked_spawns.append(
                    resolve_actor_spawn(
                        base, participant.spawn_point, self._config.spawn_semantics
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "parked spawn resolve failed for %s: %s",
                    participant.participant_id,
                    exc,
                )

    @staticmethod
    def _participant_blueprint(
        library: Any, participant: TrafficParticipantConfig, prefix: str
    ) -> Any:
        """按主蓝图 + 候选链取参与者蓝图；全部缺失时按家族通配兜底。"""
        for candidate in [participant.blueprint, *participant.blueprint_candidates]:
            if not candidate:
                continue
            if candidate.endswith("*"):
                try:
                    matches = list(library.filter(candidate))
                except Exception:  # noqa: BLE001  蓝图库不支持 filter
                    matches = []
                if matches:
                    logger.warning(
                        f"Participant '{participant.participant_id}' 使用通配回退蓝图 "
                        f"'{getattr(matches[0], 'id', candidate)}'"
                    )
                    return matches[0]
                continue
            blueprint = _safe_find(library, candidate)
            if blueprint is not None:
                if candidate != participant.blueprint:
                    logger.warning(
                        f"Participant '{participant.participant_id}' 蓝图 "
                        f"'{participant.blueprint}' 不可用，回退到 '{candidate}'"
                    )
                return blueprint
        if prefix == "walker.":
            wildcard = "walker.pedestrian.*"
        elif prefix == "static.prop.":
            wildcard = "static.prop.*"
        else:
            wildcard = "vehicle.*"
        try:
            matches = list(library.filter(wildcard))
        except Exception:  # noqa: BLE001
            matches = []
        if matches:
            logger.warning(
                f"Participant '{participant.participant_id}' 使用家族通配兜底蓝图 "
                f"'{getattr(matches[0], 'id', wildcard)}'"
            )
            return matches[0]
        return None

    def _participant_spawn_candidates(self, spawn_point: SpawnPoint) -> list[SpawnPoint]:
        """参与者出生点候选：原坐标优先，被占用时做小幅偏移重试。

        CARLA 的 ``try_spawn_actor`` 在位置重叠时返回 None；若只试一个点，
        参与者会被静默丢弃（画面里就只剩自车）。这里沿自车航向前后 / 左右
        各偏移 2~6m 重试，仍失败才报 WARNING。
        """
        yaw = math.radians(self._participant_base().yaw_deg)
        forward = (math.cos(yaw), math.sin(yaw))
        lateral = (-math.sin(yaw), math.cos(yaw))
        offsets = (
            (0.0, 0.0),
            (2.0, 0.0),
            (-2.0, 0.0),
            (0.0, 2.0),
            (0.0, -2.0),
            (4.0, 2.0),
            (-4.0, -2.0),
            (6.0, 0.0),
            (-6.0, 0.0),
        )
        return [
            SpawnPoint(
                x=spawn_point.x + forward[0] * ahead + lateral[0] * side,
                y=spawn_point.y + forward[1] * ahead + lateral[1] * side,
                z=spawn_point.z,
                yaw_deg=spawn_point.yaw_deg,
            )
            for ahead, side in offsets
        ]

    def _distance_to_ego(self, spawn_point: SpawnPoint) -> Optional[float]:
        """参与者出生点到自车（实时位姿）的水平距离。"""
        base = self._participant_base()
        return math.hypot(spawn_point.x - base.x, spawn_point.y - base.y)

    def _log_participant_spawned(
        self, participant: TrafficParticipantConfig, actor: Any, spawn_point: SpawnPoint
    ) -> None:
        """参与者生成成功日志 + 「可能拍不到」告警（可视化验收排障用）。

        除世界坐标外同时打印 ``(ahead, left)`` 车体系偏移：世界坐标在自车 yaw
        非 0 时看不出「在前还是在右」，而这正是「车生成了、画面里却看不到」的
        唯一判据。
        """
        base = self._participant_base()
        ahead, left = relative_offset_from_ego(base, spawn_point)
        distance = math.hypot(ahead, left)
        behavior = getattr(participant.behavior, "value", participant.behavior)
        logger.info(
            "Participant '%s' spawned: actor=%s blueprint=%s type=%s behavior=%s "
            "spawn=(%.1f,%.1f,%.1f,yaw=%.0f) ego=(%.1f,%.1f,yaw=%.0f) "
            "relative=(ahead=%.1fm, left=%.1fm) distance_to_ego=%.1fm",
            participant.participant_id,
            getattr(actor, "id", "n/a"),
            getattr(actor, "type_id", participant.blueprint),
            participant.actor_type,
            behavior,
            spawn_point.x,
            spawn_point.y,
            spawn_point.z,
            spawn_point.yaw_deg,
            base.x,
            base.y,
            base.yaw_deg,
            ahead,
            left,
            distance,
        )
        if participant.behavior == ScenarioBehaviorType.STATIC:
            return
        if not self._estimate_participant_visible(ahead, left):
            logger.warning(
                "Participant '%s' 相对自车 ahead=%.1fm / left=%.1fm，落在环视主视区外"
                "（车后、超出 %.0fm 或侧向偏出 FOV）：可视化画面里看不到它的行为；"
                "请确认平台下发的相对偏移（x 前向 / y 左向，米）是否符合场景意图",
                participant.participant_id,
                ahead,
                left,
                _MAX_VISIBLE_DISTANCE_M,
            )

    # ── 运动/行为控制 ─────────────────────────────────────────────────────

    def _configure_ego_motion(self, world: Any) -> None:
        """应用自车初始速度：TM 优先，直道可切确定性纵向控制。

        V3.8.0：SIL 现场发现 TM 在同步模式下对自车速度环存在油门/刹车极限环
        （实测净速度 ~0.7 m/s、IMU 纵向 ±15~22 m/s²），且与行人远近无关。对直道
        场景改用确定性纵向控制（P + 巡航前馈）绕开 TM 速度环；弯道/复杂路况仍交
        TM。可用场景参数 ``ego_drive_mode`` 或环境变量 ``HUNTER_SIM_EGO_DRIVE``
        （auto|tm|manual）强制。
        """
        if self._ego is None:
            return
        speed = float(self._config.ego_vehicle.initial_speed_ms)
        if speed <= 0:
            return
        if self._config.ego_vehicle.autopilot:
            if self._resolve_ego_drive_mode(world) == "manual":
                self._enable_deterministic_ego_drive(speed)
                return
            if self._set_desired_speed(self._ego, speed):
                logger.info(f"Ego initial speed set via Traffic Manager: {speed:.1f} m/s")
                return
        self._ego_manual_speed = speed
        logger.info(f"Ego initial speed set via manual control: {speed:.1f} m/s")

    def _resolve_ego_drive_mode(self, world: Any) -> str:
        """决定自车纵向驱动方式：manual（确定性）或 tm（Traffic Manager）。"""
        forced = str(
            self._config.extra_params.get("ego_drive_mode")
            or os.getenv("HUNTER_SIM_EGO_DRIVE")
            or "auto"
        ).strip().lower()
        if forced in {"tm", "traffic_manager", "manager", "autopilot"}:
            return "tm"
        if forced in {"manual", "deterministic", "pcontrol"}:
            return "manual"
        return "manual" if self._road_is_straight(world) else "tm"

    def _road_is_straight(self, world: Any) -> bool:
        """沿自车出生车道向前累计航向变化，阈值内视为直道。

        地图/waypoint 接口缺失或异常时按「非直道」处理，保持既有 TM 行为（向后
        兼容：无 ``get_waypoint`` 的 Mock/旧客户端不会误切入确定性控制）。
        """
        spawn = self._ego_spawn or self._config.ego_vehicle.spawn_point
        try:
            carla_map = world.get_map()
            get_waypoint = getattr(carla_map, "get_waypoint", None)
            if not callable(get_waypoint):
                return False
            try:
                import carla  # noqa: PLC0415  真实 CARLA 环境下用原生 Location

                location: Any = carla.Location(x=spawn.x, y=spawn.y, z=spawn.z)
            except ImportError:
                location = SimpleNamespace(x=spawn.x, y=spawn.y, z=spawn.z)
            waypoint = get_waypoint(location, project_to_road=True)
            if waypoint is None:
                return False
            start_yaw = float(waypoint.transform.rotation.yaw)
            current = waypoint
            traveled = 0.0
            while traveled < _EGO_STRAIGHT_LOOKAHEAD_M:
                nxt = current.next(_EGO_STRAIGHT_STEP_M)
                if not nxt:
                    return True
                current = nxt[0]
                traveled += _EGO_STRAIGHT_STEP_M
                delta = abs(
                    self._wrap_deg(float(current.transform.rotation.yaw) - start_yaw)
                )
                if delta > _EGO_STRAIGHT_MAX_HEADING_DEG:
                    return False
            return True
        except Exception as exc:  # noqa: BLE001  Mock / 地图接口差异
            logger.debug(f"ego straight-road probe failed: {exc}")
            return False

    @staticmethod
    def _wrap_deg(angle_deg: float) -> float:
        """把角度折算到 [-180, 180)。"""
        return (angle_deg + 180.0) % 360.0 - 180.0

    def _enable_deterministic_ego_drive(self, speed: float) -> None:
        """解除 TM 托管，改由确定性纵向控制驱动自车（直道）。"""
        self._ego_deterministic = True
        set_autopilot = getattr(self._ego, "set_autopilot", None)
        if callable(set_autopilot):
            try:
                set_autopilot(False)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"ego autopilot disable failed: {exc}")
        self._ego_manual_speed = speed
        logger.info(
            "ego_drive_mode=deterministic_straight: 直道检测通过，自车改用确定性"
            "纵向控制（期望 %.1f m/s，已解除 TM），绕开 TM 速度环油门/刹车振荡",
            speed,
        )

    def _apply_scene_event_actions(self, fired: list[Any]) -> None:
        """执行本 tick 越沿触发的平台事件动作（V3.8.1：brake 类接入自车减速）。

        历史上事件 ``action_type`` 只「留存不执行」；确定性纵向控制绕开 TM 后，自
        车不再由 TM 避让前车/窜出物，因此需把 brake/emergency_stop 类动作显式接回自
        车。只在直道确定性控制下接管自车纵向；TM 托管时避让本由 TM 负责，不干预。

        V3.8.2：越沿触发只「武装」制动目标，不立即刹停；是否真正制动交由
        :meth:`_ego_event_brake_active` 每 tick 判定目标是否已进入自车行驶路径，
        避免与等待型横穿（sudden_cross）互锁死锁。

        V3.8.7：消费平台 ``action_semantics`` 标记——``expectation_only`` 时
        事件动作是评估基线而非指令，按标记分支落期望基线留痕；现有接管
        行为不变（防御性接入，后续若平台下发指令语义再在此分支扩展）。
        """
        semantics = str(getattr(self._config, "action_semantics", "") or "").strip().lower()
        for definition in fired:
            action_type = str(getattr(definition, "action_type", "") or "")
            if semantics == _ACTION_SEMANTICS_EXPECTATION and (
                self._is_ego_brake_action(action_type)
                or self._is_ego_speed_change_action(action_type)
            ):
                # 期望基线留痕：替自车执行期望动作会使评估 oracle 恒真，
                # 这里只声明语义不改变执行（接管行为维持 V3.8.2/V3.8.5 口径）。
                logger.info(
                    "scene_event_action expectation_baseline: event=%s action=%s "
                    "semantics=expectation_only（评估基线非指令，接管行为不变）",
                    getattr(definition, "event_id", "?"),
                    action_type,
                )
            if self._is_ego_speed_change_action(action_type):
                self._apply_ego_speed_change(definition, action_type)
                continue
            if not self._is_ego_brake_action(action_type):
                continue
            if not self._ego_deterministic:
                logger.info(
                    "scene_event_action deferred_to_traffic_manager: event=%s action=%s "
                    "（未启用确定性控制，自车避让由 TM 负责）",
                    getattr(definition, "event_id", "?"),
                    action_type,
                )
                continue
            params = getattr(definition, "action_parameters", {}) or {}
            raw = params.get("target_speed", params.get("target_speed_ms", 0.0))
            try:
                target_ms = float(raw)
            except (TypeError, ValueError):
                target_ms = 0.0
            self._ego_event_brake_ms = max(0.0, target_ms)
            # V3.8.10：新事件重新武装制动→复位最长保持计时与超时释放标记。
            self._ego_event_brake_since_s = None
            self._ego_event_brake_hold_expired = False
            logger.info(
                "scene_event_action ego_brake: event=%s action=%s -> 自车减速目标 %.1f m/s"
                "（确定性纵向控制接管；目标进入自车行驶路径后才制动，达目标后保持）",
                getattr(definition, "event_id", "?"),
                action_type,
                self._ego_event_brake_ms,
            )

    @staticmethod
    def _is_ego_brake_action(action_type: str) -> bool:
        """action_type 是否为驱动自车减速类（子串匹配，忽略大小写）。"""
        text = str(action_type or "").strip().lower()
        if not text:
            return False
        return any(keyword in text for keyword in _EGO_BRAKE_ACTION_KEYWORDS)

    @staticmethod
    def _is_ego_speed_change_action(action_type: str) -> bool:
        """V3.8.5：action_type 是否为改变自车期望速度类（子串匹配）。"""
        text = str(action_type or "").strip().lower()
        if not text:
            return False
        return any(keyword in text for keyword in _EGO_SPEED_CHANGE_KEYWORDS)

    def _apply_ego_speed_change(self, definition: Any, action_type: str) -> None:
        """V3.8.5：执行 speed_change 类事件动作，改自车期望速度。

        确定性控制改 ``_ego_manual_speed``；TM 托管改 ``set_desired_speed``。
        目标速度从 action_parameters 的 speed/target_speed/desired_speed/
        speed_ms/value 依次取；缺省/负值忽略并告警。补齐历史上
        「custom->speed_change 只留存不执行」的契约缺口。
        """
        params = getattr(definition, "action_parameters", {}) or {}
        target: Optional[float] = None
        for key in ("speed", "target_speed", "desired_speed", "speed_ms", "value"):
            raw = params.get(key)
            if raw is None:
                continue
            try:
                target = float(raw)
            except (TypeError, ValueError):
                continue
            break
        if target is None or target < 0.0:
            logger.warning(
                "scene_event_action speed_change_unsupported: event=%s action=%s "
                "params=%s（缺少非负速度参数，忽略）",
                getattr(definition, "event_id", "?"),
                action_type,
                params,
            )
            return
        if self._ego_deterministic:
            self._ego_manual_speed = target
        else:
            setter = getattr(self._ego, "set_desired_speed", None)
            if callable(setter):
                try:
                    setter(target)
                except Exception as exc:  # noqa: BLE001  TM 未就绪时降级
                    logger.debug(f"ego set_desired_speed failed: {exc}")
        logger.info(
            "scene_event_action ego_speed_change: event=%s action=%s -> 期望速度 %.1f m/s"
            "（%s）",
            getattr(definition, "event_id", "?"),
            action_type,
            target,
            "确定性纵向控制" if self._ego_deterministic else "TM 托管",
        )

    def _ego_event_brake_active(self) -> Optional[float]:
        """事件制动目标在本 tick 是否生效（V3.8.2：危险物在路径内才制动）。

        事件越沿触发只「武装」制动目标；若此时触发源（如伏在路缘等待的
        sudden_cross 动物）尚未进入自车行驶路径，立即刹停会让自车永远到不了
        行人的窜出触发距离，形成互锁死锁。故仅当存在前方且在车道横向半宽内的
        参与者时才返回制动目标，否则返回 None（恢复巡航）。

        V3.8.10：若危险物持续在路径内超过 ``_ego_event_brake_max_hold_s()``（多为
        脚本化横穿被刹停自车物理楔住、走不出去），则一次性强制释放（置
        ``_ego_event_brake_hold_expired``）恢复巡航，打破整场 0 m/s 冻结。
        """
        if self._ego_event_brake_ms is None:
            self._ego_brake_latched = False
            self._ego_event_brake_since_s = None
            self._ego_event_brake_hold_expired = False
            return None
        if self._ego_event_brake_hold_expired:
            # V3.8.10：本次武装已因超时强制释放，不再回检（避免与楔住的横穿物反复制动↔巡航）。
            return None
        if self._hazard_in_ego_path():
            now = self.elapsed_seconds
            if self._ego_event_brake_since_s is None:
                self._ego_event_brake_since_s = now
            max_hold = _ego_event_brake_max_hold_s()
            if max_hold > 0.0 and now - self._ego_event_brake_since_s >= max_hold:
                # 危险物长时间未离开行驶路径（多为脚本化横穿被刹停自车物理楔住）：
                # 强制释放制动恢复巡航，打破整场冻结（避免冻结掩盖真实评估结果）。
                self._ego_event_brake_hold_expired = True
                self._ego_brake_latched = False
                self._ego_event_brake_since_s = None
                logger.warning(
                    "ego_event_brake_hold_timeout: 事件制动已连续保持 %.0fs 而危险物仍未离开"
                    "行驶路径（多为脚本化横穿被刹停自车物理楔住），强制释放制动恢复巡航。",
                    max_hold,
                )
                return None
            return self._ego_event_brake_ms
        self._ego_event_brake_since_s = None  # 危险物已离开路径，复位持续制动计时
        return None

    def _hazard_in_ego_path(self) -> bool:
        """是否存在落在自车行驶路径内（前方且横向≤半宽）的参与者。

        V3.8.4：带滞回。未锁存时用进入半宽 ``_EGO_BRAKE_LANE_HALF_M`` 判定；一旦
        制动生效（锁存）改用更宽的释放半宽 ``_EGO_BRAKE_RELEASE_HALF_M`` 且要求仍
        在前方，直到危险物横向越界或落到后方才解锁，避免边界抖振与近距擦碰。
        """
        if self._ego is None:
            self._ego_brake_latched = False
            return False
        forward, left = self._ego_axes()
        try:
            location = self._ego.get_transform().location
            ex, ey = float(location.x), float(location.y)
        except Exception:  # noqa: BLE001
            return False
        half = (
            _EGO_BRAKE_RELEASE_HALF_M
            if self._ego_brake_latched
            else _EGO_BRAKE_LANE_HALF_M
        )
        for actor in self._actors:
            try:
                p = actor.get_transform().location
            except Exception:  # noqa: BLE001
                continue
            px, py = float(p.x), float(p.y)
            if is_placeholder_origin(SpawnPoint(x=px, y=py, z=0.0)):
                continue
            dx, dy = px - ex, py - ey
            ahead = dx * forward[0] + dy * forward[1]
            lateral = abs(dx * left[0] + dy * left[1])
            if ahead > 0.0 and lateral <= half:
                self._ego_brake_latched = True
                return True
        self._ego_brake_latched = False
        return False

    def _reapply_post_spawn_controls_once(self) -> None:
        """首个 tick 之后补发一次 autopilot / 期望速度指令。

        ``setup()`` 在 ``world.tick()`` 之前执行，此时 Traffic Manager 尚未掌握
        该 Actor 的位置/路由信息，下发的 ``set_autopilot`` / ``set_desired_speed``
        会被静默丢弃：现场表现为自车只以 2 m/s 爬行、前车基本不动，跟车行为无法
        产生。首 tick 后补发一次成本极低，且只执行一次。
        """
        if self._post_tick_done:
            return
        self._post_tick_done = True
        ego_cmds: list[str] = []
        ego_cfg = self._config.ego_vehicle
        if self._ego is not None and ego_cfg.autopilot and not self._ego_deterministic:
            set_autopilot = getattr(self._ego, "set_autopilot", None)
            if callable(set_autopilot):
                try:
                    set_autopilot(True, self._traffic_manager_port)
                    ego_cmds.append("autopilot")
                except Exception as exc:  # noqa: BLE001  TM 未就绪时保持原有降级路径
                    logger.debug(f"post-tick ego autopilot failed: {exc}")
            speed = float(ego_cfg.initial_speed_ms)
            if speed > 0 and self._set_desired_speed(self._ego, speed):
                ego_cmds.append(f"speed={speed:.1f}m/s")
        participant_count = 0
        for actor, speed_ms in self._tm_managed:
            set_autopilot = getattr(actor, "set_autopilot", None)
            if callable(set_autopilot):
                try:
                    set_autopilot(True, self._traffic_manager_port)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"post-tick participant autopilot failed: {exc}")
                    continue
            self._set_desired_speed(actor, speed_ms)
            participant_count += 1
        logger.info(
            "post_tick_controls_reapplied: ego=[%s] tm_participants=%d",
            " ".join(ego_cmds) if ego_cmds else "无",
            participant_count,
        )

    def _enable_autopilot(
        self, actor: Any, participant: TrafficParticipantConfig
    ) -> None:
        """启用参与者 autopilot（TM 端口来自实例配置）并设置初始速度。"""
        set_autopilot = getattr(actor, "set_autopilot", None)
        if callable(set_autopilot):
            try:
                set_autopilot(True, self._traffic_manager_port)
            except Exception as exc:  # noqa: BLE001  TM 未就绪时降级为手动行为
                if participant.actor_type == "cyclist":
                    logger.warning(
                        "cyclist_tm_unavailable: participant=%s %s；降级为手动控制",
                        participant.participant_id,
                        exc,
                    )
                else:
                    logger.warning(
                        f"Participant '{participant.participant_id}' autopilot failed: {exc}"
                    )
        self._set_desired_speed(actor, participant.speed_ms)
        # 登记供首 tick 后补发：setup 阶段的 TM 指令会被静默丢弃
        self._tm_managed.append((actor, float(participant.speed_ms)))

    def _set_desired_speed(self, actor: Any, speed_ms: float) -> bool:
        """设置 TM 期望速度（m/s），返回是否成功。"""
        if self._traffic_manager is None:
            return False
        try:
            self._traffic_manager.set_desired_speed(actor, max(0.0, float(speed_ms)))
            return True
        except Exception as exc:  # noqa: BLE001  个别 Actor 未注册 TM 时静默降级
            logger.debug(f"TM set_desired_speed failed: {exc}")
            return False

    def _build_participant_controller(
        self, actor: Any, participant: TrafficParticipantConfig
    ) -> Optional[ScriptedActorController]:
        """按配置构造参与者行为控制器（无行为时返回 None）。"""
        behavior_cfg = self._behavior_config(participant)
        if behavior_cfg is None:
            return None
        try:
            behavior = create_behavior_from_config(behavior_cfg)
        except ValueError as exc:
            logger.warning(
                f"Participant '{participant.participant_id}' behavior skipped: {exc}"
            )
            return None
        return ScriptedActorController(actor, behavior, self._trigger_for(participant))

    @staticmethod
    def _behavior_config(participant: TrafficParticipantConfig) -> Optional[dict[str, Any]]:
        """场景行为类型 → ``actor_behaviors`` 工厂配置。"""
        speed = float(participant.speed_ms)
        behavior = participant.behavior
        if behavior == ScenarioBehaviorType.STATIC:
            return None
        if behavior == ScenarioBehaviorType.CONSTANT_SPEED:
            return {"type": "constant_speed", "speed": speed}
        if behavior == ScenarioBehaviorType.ACCELERATE:
            # 无独立加速行为：以更高目标速度的匀速行为近似
            return {"type": "constant_speed", "speed": speed}
        if behavior in (ScenarioBehaviorType.DECELERATE, ScenarioBehaviorType.STOP_AND_GO):
            return {
                "type": "decelerate",
                "initial_speed": speed,
                "target_speed": 0.0,
                "deceleration": 2.0,
            }
        if behavior in (ScenarioBehaviorType.CUT_IN, ScenarioBehaviorType.CUT_OUT):
            return {
                "type": "cut_in",
                "speed": speed,
                "behavior_id": behavior.value,
            }
        if behavior == ScenarioBehaviorType.PEDESTRIAN_CROSS:
            return {"type": "pedestrian_cross", "speed": speed}
        return {"type": "constant_speed", "speed": speed}

    @staticmethod
    def _trigger_for(participant: TrafficParticipantConfig) -> Optional[Any]:
        """行为触发条件 → ``trigger_conditions`` 工厂实例。"""
        trigger = participant.behavior_trigger
        if trigger is None:
            return None
        trigger_type = trigger.trigger_type
        if trigger_type == TriggerType.DISABLED:
            logger.debug(
                f"Participant '{participant.participant_id}' behavior trigger disabled"
            )
            return None
        if trigger_type == TriggerType.POSITION:
            # SceneEventTrigger 不携带坐标，无法构造位置触发
            logger.debug(
                f"Participant '{participant.participant_id}' position trigger unsupported"
            )
            return None
        config: dict[str, Any] = {
            "type": trigger_type.value,
            "comparator": trigger.comparator,
        }
        if trigger_type == TriggerType.EVENT:
            config["event_name"] = trigger.event_name
        else:
            config["value"] = trigger.value
        try:
            return create_trigger_from_config(config)
        except ValueError as exc:
            logger.warning(
                f"Participant '{participant.participant_id}' trigger skipped: {exc}"
            )
            return None

    def _apply_participant_action(self, controller: ScriptedActorController, action: Any) -> None:
        """把行为输出落到参与者（TM 期望速度 / 手动控制 + 变道）。"""
        actor = controller.actor
        if actor is None:
            return
        if self._traffic_manager is not None and self._has_autopilot(actor):
            target = 0.0 if action.is_stop else float(action.target_speed_ms)
            self._set_desired_speed(actor, target)
        else:
            self._apply_vehicle_control(
                actor,
                throttle=float(action.throttle),
                brake=float(action.brake),
                steer=float(action.steering),
            )
        # 切入/切出：触发后只发一次变道指令，由 TM 完成平滑变道
        if isinstance(controller.behavior, CutInBehavior) and controller.is_triggered:
            key = id(actor)
            if key not in self._cut_in_done:
                self._cut_in_done.add(key)
                direction = self._lane_change_direction(actor, controller.behavior)
                if controller.behavior.behavior_id == "cut_out":
                    # 切出是朝远离自车的另一侧变道
                    direction = not direction
                self._force_lane_change(actor, direction)

    def _lane_change_direction(self, actor: Any, behavior: CutInBehavior) -> bool:
        """决定变道方向（CARLA 语义：True 向右，False 向左）。

        优先按自车相对参与者的横向位置判断（切向自车所在车道）；
        无法读取位姿时退回行为配置的 ``lateral_offset_m`` 符号。
        """
        lateral = self._ego_relative_lateral(actor)
        if lateral is not None and abs(lateral) >= 1.0:
            return lateral > 0.0
        return behavior.lateral_offset_m < 0.0

    def _ego_relative_lateral(self, actor: Any) -> Optional[float]:
        """自车相对 ``actor`` 的横向偏移（正数在右，负数在左）。"""
        if self._ego is None:
            return None
        try:
            actor_transform = actor.get_transform()
            ego_transform = self._ego.get_transform()
            dx = float(ego_transform.location.x) - float(actor_transform.location.x)
            dy = float(ego_transform.location.y) - float(actor_transform.location.y)
        except Exception:  # noqa: BLE001  Mock/无位姿 Actor
            return None
        get_right = getattr(actor_transform, "get_right_vector", None)
        if callable(get_right):
            try:
                right = get_right()
                return dx * float(right.x) + dy * float(right.y)
            except Exception:  # noqa: BLE001  退回按 yaw 计算
                pass
        yaw = math.radians(float(actor_transform.rotation.yaw))
        # 右向量 = (-sin(yaw), cos(yaw))
        return dx * -math.sin(yaw) + dy * math.cos(yaw)

    def _force_lane_change(self, actor: Any, direction: bool) -> None:
        """请求 TM 变道（CARLA 语义：direction=True 向右，False 向左）。"""
        if self._traffic_manager is None:
            return
        try:
            self._traffic_manager.force_lane_change(actor, bool(direction))
        except Exception as exc:  # noqa: BLE001  部分版本/未注册 Actor 不支持
            logger.debug(f"TM force_lane_change failed: {exc}")

    @staticmethod
    def _has_autopilot(actor: Any) -> bool:
        """判断 Actor 当前是否由 autopilot 接管（无法判断时按 True 处理）。"""
        getter = getattr(actor, "get_autopilot", None)
        if not callable(getter):
            return True
        try:
            return bool(getter())
        except Exception:  # noqa: BLE001
            return True

    def _apply_ego_manual_speed(self, delta_seconds: float) -> None:
        """自车确定性纵向速度控制（P + 巡航前馈，带稳态死区）。

        用于非 TM 驱动（``autopilot=False`` 或直道确定性模式）：以前向投影速度为
        反馈量，误差超过死区才给油/制动，稳态保留巡航前馈油门，避免 TM 速度环的
        油门/刹车极限环。

        V3.8.1：若事件制动已触发（``_ego_event_brake_ms`` 非空），目标速度改为事件
        制动的目标（默认 0=停车），并在达到目标后保持制动（而非前馈油门），使自车
        能对前车紧急制动/窜出物自动减速并停稳。

        V3.8.2：事件制动是否生效改由 :meth:`_ego_event_brake_active` 判定（危险物
        进入自车行驶路径才制动），等待型横穿伏在路缘时不刹停、恢复巡航。

        V3.8.3：确定性模式下转向不再固定为初始值，改由 :meth:`_lane_keep_steer`
        沿车道中心线纯追踪过弯，避免直道判定之后的弯道把自车甩出路外。

        V3.8.4：确定性模式下对油门/刹车/转向输出一阶低通，抑制 bang-bang 切换
        引起的悬架俯仰/侧倾阶跃（环视相机刚性挂自车，表现为画面抖动）。
        """
        if self._ego is None:
            return
        event_brake = self._ego_event_brake_active()
        if event_brake is not None:
            target = event_brake
            hold_brake = True
        else:
            if self._ego_manual_speed <= 0:
                return
            target = self._ego_manual_speed
            hold_brake = False
        forward, _ = self._ego_axes()
        projected = self._projected_speed(self._ego, forward)
        current = projected if projected is not None else self._current_speed(self._ego)
        error = target - current
        if error > _EGO_SPEED_DEADBAND:
            throttle = min(1.0, max(0.0, _EGO_SPEED_FEEDFORWARD + _EGO_SPEED_KP * error))
            brake = 0.0
        elif error < -_EGO_SPEED_DEADBAND:
            throttle = 0.0
            brake = min(1.0, max(0.0, _EGO_BRAKE_GAIN * -error))
        elif hold_brake:
            throttle = 0.0
            brake = _EGO_BRAKE_HOLD
        else:
            throttle = _EGO_SPEED_FEEDFORWARD
            brake = 0.0
        # V3.8.6：物理卡滞保护——油门高却几乎不动（路外/楔住/顶住障碍）持续
        # >_EGO_PHYS_STUCK_S 时停止给油并保持制动，避免 throttle=1 空转楔死整场
        # （实机 Town01 自车冲出路外后 throttle=1.00/actual=0.00刷 170s）。
        # V3.8.8：latch 态改为优先判定的周期探测状态机，修复自锁死锁——旧实现的
        # 复位/恢复检查嵌在「throttle>0.5 且 current<0.1」门内，latch 后速度一旦
        # 恢复就不再进入该门，而常规控制又恒算出 throttle=1.0，永不可达复位
        # （实机动物已离开路径仍冻结 214s）。
        if self._ego_phys_stuck_warned:
            # latched 态优先分支：恢复判定不受原门条件限制
            if current >= _EGO_PHYS_STUCK_CLEAR_MS:
                # 探测成功（或其它途径恢复移动）：解除 latch，复位计时与探测状态；
                # 控制量不干预，本 tick 即恢复常规 P 控制输出（与 V3.8.6 复位语义一致）
                logger.info(
                    "ego_physical_stuck_recovered: actual=%.2f m/s ≥ %.2f，"
                    "卡滞解除，恢复常规控制 latched=%.1fs",
                    current,
                    _EGO_PHYS_STUCK_CLEAR_MS,
                    self.elapsed_seconds - self._ego_phys_stuck_latched_at_s,
                )
                self._ego_phys_stuck_since_s = 0.0
                self._ego_phys_stuck_warned = False
                self._ego_phys_stuck_latched_at_s = 0.0
                self._ego_phys_stuck_next_probe_at_s = 0.0
                self._ego_phys_stuck_probe_until_s = 0.0
            elif self.elapsed_seconds < self._ego_phys_stuck_probe_until_s and (
                event_brake is None
            ):
                # 探测窗口内：给油不起制动，让车尝试移动（真楔住时物理上仍不动，
                # 窗口到期自动回锁）
                throttle = _EGO_PHYS_STUCK_PROBE_THROTTLE
                brake = 0.0
            elif self.elapsed_seconds >= self._ego_phys_stuck_next_probe_at_s and (
                event_brake is None
            ):
                # 开启新一轮探测窗口（下轮时刻与窗口截止在此一次性排定，
                # 回锁分支不重排，避免浮点边界把窗口腰斩）
                self._ego_phys_stuck_probe_until_s = (
                    self.elapsed_seconds + _EGO_PHYS_STUCK_PROBE_S
                )
                self._ego_phys_stuck_next_probe_at_s = (
                    self._ego_phys_stuck_probe_until_s + _EGO_PHYS_STUCK_PROBE_INTERVAL_S
                )
                logger.warning(
                    "ego_physical_stuck_probe: 释放制动探测恢复（持续已 %.0fs）"
                    "throttle=%.2f 探测 %.1fs nearest=%s",
                    self.elapsed_seconds - self._ego_phys_stuck_latched_at_s,
                    _EGO_PHYS_STUCK_PROBE_THROTTLE,
                    _EGO_PHYS_STUCK_PROBE_S,
                    self._nearest_actor_summary(),
                )
                throttle = _EGO_PHYS_STUCK_PROBE_THROTTLE
                brake = 0.0
            else:
                # 保持锁死：停油 + 保持制动（危险物仍在路径时禁止探测，避免冲撞）
                throttle = 0.0
                brake = _EGO_BRAKE_HOLD
        elif throttle > 0.5 and current < _EGO_PHYS_STUCK_LATCH_MS:
            if self._ego_phys_stuck_since_s == 0.0:
                self._ego_phys_stuck_since_s = self.elapsed_seconds
            elif self.elapsed_seconds - self._ego_phys_stuck_since_s > _EGO_PHYS_STUCK_S:
                self._ego_phys_stuck_warned = True
                self._ego_phys_stuck_latched_at_s = self.elapsed_seconds
                self._ego_phys_stuck_next_probe_at_s = (
                    self.elapsed_seconds + _EGO_PHYS_STUCK_PROBE_INTERVAL_S
                )
                logger.warning(
                    "ego_physical_stuck: throttle=%.2f 却 actual=%.2f m/s 持续 >%.0fs，"
                    "判定物理卡滞（路外/楔住/顶住障碍），停止给油并保持制动，"
                    "每 %.0fs 释放制动探测恢复 nearest=%s",
                    throttle,
                    current,
                    _EGO_PHYS_STUCK_S,
                    _EGO_PHYS_STUCK_PROBE_INTERVAL_S,
                    self._nearest_actor_summary(),
                )
                throttle = 0.0
                brake = _EGO_BRAKE_HOLD
        elif current < _EGO_PHYS_STUCK_CLEAR_MS:
            # 半动半停（0.1~0.3 m/s）：保持现有判定不打断计时也不复位，
            # 避免在滞回区间内高频抖动
            pass
        else:
            self._ego_phys_stuck_since_s = 0.0
            self._ego_phys_stuck_warned = False
            self._ego_phys_stuck_latched_at_s = 0.0
            self._ego_phys_stuck_next_probe_at_s = 0.0
            self._ego_phys_stuck_probe_until_s = 0.0
        steer = self._ego_initial_steer
        if self._ego_deterministic:
            steer = self._lane_keep_steer(delta_seconds)
            alpha = _EGO_CTRL_EMA_ALPHA
            self._ego_ctrl_throttle += alpha * (throttle - self._ego_ctrl_throttle)
            self._ego_ctrl_brake += alpha * (brake - self._ego_ctrl_brake)
            self._ego_ctrl_steer += alpha * (steer - self._ego_ctrl_steer)
            throttle = self._ego_ctrl_throttle
            brake = self._ego_ctrl_brake
            steer = self._ego_ctrl_steer
        self._apply_vehicle_control(
            self._ego,
            throttle=throttle,
            brake=brake,
            steer=steer,
        )

    def _nearest_actor_summary(self) -> str:
        """最近 Actor 观测摘要（ahead/lateral 米，自车坐标系），无可用 Actor 返 'n/a'。

        V3.8.8：卡滞 latch/探测日志附带，用于区分「顶住障碍（近距）」与
        「空转打滑/路外（无近距 Actor）」两类根因；仅读 get_transform，失败降级。
        """
        if self._ego is None:
            return "n/a"
        forward, left = self._ego_axes()
        try:
            location = self._ego.get_transform().location
            ex, ey = float(location.x), float(location.y)
        except Exception:  # noqa: BLE001
            return "n/a"
        best: tuple[float, float, float] | None = None
        for actor in self._actors:
            try:
                p = actor.get_transform().location
            except Exception:  # noqa: BLE001
                continue
            dx, dy = float(p.x) - ex, float(p.y) - ey
            ahead = dx * forward[0] + dy * forward[1]
            lateral = dx * left[0] + dy * left[1]
            dist = math.hypot(dx, dy)
            if best is None or dist < best[0]:
                best = (dist, ahead, lateral)
        if best is None:
            return "n/a"
        return f"{best[0]:.1f}m(ahead={best[1]:.1f},lat={best[2]:.1f})"

    def _lane_keep_steer(self, delta_seconds: float) -> float:
        """确定性控制的车道保持转向（V3.8.3：pure-pursuit 沿车道中心线）。

        直道判定只保证「出生点前 40m 是直道」，地图后续弯道仍会让固定转向的自车
        冲出路外（实机 Town06 冲上草地后物理卡滞、throttle=1 却 0 m/s）。这里按
        间隔用地图 waypoint 取前视点，按自行车模型纯追踪算前轮转角，使自车沿车道
        中心线过弯；地图/接口缺失时退回初始转向（保持既有行为）。
        """
        self._ego_lane_steer_age_s += max(0.0, float(delta_seconds))
        if self._ego_lane_steer_age_s < _EGO_LANE_STEER_INTERVAL_S:
            return self._ego_lane_steer
        self._ego_lane_steer_age_s = 0.0
        self._ego_lane_steer = self._compute_lane_keep_steer()
        return self._ego_lane_steer

    def _compute_lane_keep_steer(self) -> float:
        """按当前位姿与车道前视点计算纯追踪转向（-1~1）；失败退回初始转向。"""
        world = self._world
        if world is None or self._ego is None:
            return self._ego_initial_steer
        try:
            carla_map = world.get_map()
            get_waypoint = getattr(carla_map, "get_waypoint", None)
            if not callable(get_waypoint):
                return self._ego_initial_steer
            transform = self._ego.get_transform()
            location = transform.location
            try:
                import carla  # noqa: PLC0415  真实 CARLA 环境下用原生 Location

                probe: Any = carla.Location(
                    x=location.x, y=location.y, z=location.z
                )
            except ImportError:
                probe = SimpleNamespace(x=location.x, y=location.y, z=location.z)
            waypoint = get_waypoint(probe, project_to_road=True)
            if waypoint is None:
                return self._ego_initial_steer
            ahead = waypoint.next(_EGO_LANE_LOOKAHEAD_M)
            if not ahead:
                return self._ego_initial_steer
            yaw_deg = float(transform.rotation.yaw)
            # V3.8.6：路口 next() 返回多分支，ahead[0] 可能恰为左/右转分支，纯追踪
            # 会把自车甩离直线路径冲出路外物理卡滞（实机 Town01 steer=-0.72、
            # y 偏移 123m）。改取与当前航向夹角最小的分支（直行优先）。

            def _branch_deviation(wp: Any) -> float:
                loc = wp.transform.location
                bearing_deg = math.degrees(
                    math.atan2(
                        float(loc.y) - float(location.y),
                        float(loc.x) - float(location.x),
                    )
                )
                return abs(self._wrap_deg(bearing_deg - yaw_deg))

            target = min(ahead, key=_branch_deviation).transform.location
            bearing = math.degrees(
                math.atan2(
                    float(target.y) - float(location.y),
                    float(target.x) - float(location.x),
                )
            )
            alpha = math.radians(self._wrap_deg(bearing - yaw_deg))
            curvature = 2.0 * math.sin(alpha) / _EGO_LANE_LOOKAHEAD_M
            steer_rad = math.atan(_EGO_LANE_WHEELBASE_M * curvature)
            return max(-1.0, min(1.0, steer_rad / _EGO_LANE_MAX_STEER_RAD))
        except Exception as exc:  # noqa: BLE001  Mock / 地图接口差异
            logger.debug(f"ego lane-keep steer failed: {exc}")
            return self._ego_initial_steer

    def _update_traveled_distance(self, delta_seconds: float) -> None:
        """积分自车速度得到已行驶里程（供 traveled_distance 事件使用）。"""
        if self._ego is None:
            return
        step = max(0.0, min(float(delta_seconds), 1.0))
        self._traveled_distance_m += max(0.0, self._current_speed(self._ego)) * step

    def _warn_ego_speed_stuck(self) -> None:
        """自车长期远低于期望速度时限频告警，帮助定位 SIL「卡低速」根因。

        期望速度取 ``ego_vehicle.initial_speed_ms``（autopilot 下发给 TM）。
        实际速度连续 >4s 低于期望的 40% 时，每 15s 打一条 WARNING，附油门/
        刹车与脚本化行人状态——用于区分「TM 被行人/前车逼停」与「TM 根本没
        提速」两类问题。
        """
        if self._ego is None:
            return
        ego_cfg = self._config.ego_vehicle
        desired = float(getattr(ego_cfg, "initial_speed_ms", 0.0) or 0.0)
        if desired <= 0.0:
            return
        if self._ego_event_brake_active() is not None:
            # 事件制动生效中的主动停车不是「卡低速」，不告警（避免误归因 TM 避让）。
            self._slow_since_s = 0.0
            return
        actual = self._current_speed(self._ego)
        # V3.8.5：TM 托管且实际速度≈最近前车速度 = 正常跟车（非卡滞），不告警。
        # 现场 cyclist 群场景期望 15 m/s 但 TM 跟随 4 m/s 前车，属预期跟车行为。
        if not self._ego_deterministic:
            lead = self._tm_lead_speed()
            if lead is not None and abs(actual - lead) <= 1.0:
                self._slow_since_s = 0.0
                return
        now = self.elapsed_seconds
        if actual >= desired * 0.4:
            self._slow_since_s = 0.0
            return
        if self._slow_since_s == 0.0:
            self._slow_since_s = now
        if now - self._slow_since_s < 4.0:
            return
        if self._slow_warned_at_s and now - self._slow_warned_at_s < 15.0:
            return
        self._slow_warned_at_s = now
        try:
            ctrl = self._ego.get_control()
            throttle = round(float(getattr(ctrl, "throttle", 0.0)), 2)
            brake = round(float(getattr(ctrl, "brake", 0.0)), 2)
        except Exception:  # noqa: BLE001
            throttle = brake = -1.0
        walkers = " ".join(
            f"{getattr(getattr(w, 'actor', None), 'id', '?')}"
            f"[{w.mode},{'wait' if w.waiting else 'go'}]"
            for w in self._scripted_walkers
        )
        logger.warning(
            "ego_speed_stuck: desired=%.1f m/s actual=%.2f m/s (throttle=%.2f brake=%.2f) "
            "已持续 %.0fs；scripted_walkers=%s tm_managed=%d autopilot=%s"
            "—— brake 高且 walkers 非空多为 TM 为避让人/车而低速；throttle 高却不动多为物理卡滞/坡道/碰撞。",
            desired,
            actual,
            throttle,
            brake,
            now - self._slow_since_s,
            walkers or "无",
            len(self._tm_managed),
            getattr(ego_cfg, "autopilot", False),
        )

    def _tm_lead_speed(self) -> Optional[float]:
        """V3.8.5：TM 托管时最近前车的沿航向速度（m/s）；无前车返回 None。

        复用 :meth:`_relative_gaps` 的前车判定；用于区分「正常跟车低速」与
        「物理卡滞」：实际速度≈前车速度时为跟车。
        """
        if self._ego is None or not self._tm_managed:
            return None
        _distances, _lead_gap, lead_speed = self._relative_gaps()
        return lead_speed

    def _trigger_metrics(
        self, world: Any, timestamp: float, delta_seconds: float
    ) -> TriggerMetrics:
        """构造本 tick 的平台事件度量快照。"""
        metrics = TriggerMetrics(
            elapsed_s=timestamp,
            traveled_distance_m=self._traveled_distance_m,
        )
        if self._ego is None:
            return metrics
        metrics.speed_ms = self._current_speed(self._ego)
        # 纵向加速度：用前向投影速度的时间导数 + 一阶低通，不用 get_acceleration()
        # 的瞬时物理值（近静止/低速抖动会得到 ±数 m/s² 尖峰，会误触 emergency_brake）。
        try:
            vel = self._ego.get_velocity()
            forward, _ = self._ego_axes()
            fwd = float(vel.x) * forward[0] + float(vel.y) * forward[1]
            prev = self._accel_prev_fwd
            self._accel_prev_fwd = fwd
            if prev is None:
                self._accel_ema = 0.0
            else:
                dt = max(1e-3, min(float(delta_seconds), 1.0))
                raw = (fwd - prev) / dt
                self._accel_ema = (
                    _ACCEL_EMA_ALPHA * raw + (1.0 - _ACCEL_EMA_ALPHA) * self._accel_ema
                )
            metrics.acceleration_ms2 = self._accel_ema
        except Exception:  # noqa: BLE001  Mock/无位姿 Actor
            metrics.acceleration_ms2 = 0.0
        metrics.offroad = self._is_offroad(world)

        distances, lead_gap, lead_speed = self._relative_gaps()
        metrics.relative_distance_m = distances
        # V3.8.4：记录自车-参与者最小欧氏距离采样，供评估引擎判定 min_safe_distance。
        gap = distances.get("euclidean")
        if (
            gap is not None
            and gap != math.inf
            and len(self.min_distance_samples) < _MAX_MIN_DISTANCE_SAMPLES
        ):
            self.min_distance_samples.append(float(gap))
        if lead_gap is None:
            self._warn_follow_condition_unmet()
            return metrics
        metrics.time_headway_s = lead_gap / max(0.1, metrics.speed_ms)
        closing = metrics.speed_ms - (lead_speed or 0.0)
        if closing > 0.1:
            metrics.time_to_collision_s = lead_gap / closing
        return metrics

    def _warn_follow_condition_unmet(self) -> None:
        """跟车类事件永远不可能触发时限频告警（补上「0 条事件」的归因缺环）。

        ``time_headway`` / ``time_to_collision`` 只统计自车**前方**的车，前车
        被算到后方或生成在路面之外时，事件永远不触发，而旧版日志里看不出任何
        线索（现场只能看到「场景事件 0 条」）。限频 30s，避免逐 tick 刷屏。
        """
        watched = (TriggerType.TIME_HEADWAY, TriggerType.TIME_TO_COLLISION)
        declared = " ".join(
            event.event_id
            for event in self._config.scene_events
            if event.trigger.trigger_type in watched
        )
        if not declared:
            return
        now = time.time()
        if self._no_lead_warned_at and now - self._no_lead_warned_at < 30.0:
            return
        self._no_lead_warned_at = now
        base = self._reference_pose()
        layout: list[str] = []
        for participant_id, actor in list(self._actor_by_participant.items())[:5]:
            spawned = self._participant_spawns.get(participant_id)
            pose = self._actor_spawn_point(actor)
            # 同步模式首 tick 前参与者位姿仍是占位原点 (0,0,0)，直接采信会
            # 把出生坐标与原点做差，得到数百米量级的错误 ahead/left（V3.6.1 修复）。
            if pose is not None and _pose_not_synced(pose, spawned):
                pose = None
            if pose is None:
                pose = spawned
            if pose is None:
                continue
            ahead, left = relative_offset_from_ego(base, pose)
            layout.append(f"{participant_id}(ahead={ahead:.1f}m,left={left:.1f}m)")
        logger.warning(
            "follow_condition_unmet: 场景声明跟车事件 [%s]，但自车前方找不到目标车"
            "（当前布局：%s）：请核对平台下发的相对偏移（x 前向为正）与仿真端偏移基准",
            declared,
            " ".join(layout) if layout else "无已生成参与者",
        )

    def _relative_gaps(
        self,
    ) -> tuple[dict[str, float], Optional[float], Optional[float]]:
        """计算参与者相对自车的距离指标与前方最近车。

        Returns:
            ``(按关系类型的最小距离, 前车纵向间隔, 前车沿自车航向速度)``。
            无前方车辆时间隔/速度为 None。
        """
        distances: dict[str, float] = {}
        if self._ego is None:
            return distances, None, None
        forward, left = self._ego_axes()
        try:
            ego_location = self._ego.get_transform().location
            ex, ey = float(ego_location.x), float(ego_location.y)
        except Exception:  # noqa: BLE001
            return distances, None, None
        # V3.6.3：首 tick 前自车/参与者位姿均为占位原点 (0,0,0)，两者做差会得到
        # euclidean=0 的假距离，误触发 DISTANCE 事件（actual=0.000）。自车读数落在
        # 原点而已知出生点不在 → 视为未同步，本 tick 不产出距离度量。
        if _pose_not_synced(
            SpawnPoint(x=ex, y=ey, z=0.0), self._ego_spawn
        ):
            return distances, None, None

        lead_gap: Optional[float] = None
        lead_speed: Optional[float] = None
        for actor in self._actors:
            try:
                location = actor.get_transform().location
            except Exception:  # noqa: BLE001
                continue
            px, py = float(location.x), float(location.y)
            # 跳过未同步的占位原点读数（Town 地图出生点不会落在原点）。
            if is_placeholder_origin(SpawnPoint(x=px, y=py, z=0.0)):
                continue
            dx = px - ex
            dy = py - ey
            longitudinal = dx * forward[0] + dy * forward[1]
            lateral = abs(dx * left[0] + dy * left[1])
            euclidean = math.hypot(dx, dy)
            distances["euclidean"] = min(
                distances.get("euclidean", math.inf), euclidean
            )
            distances["sightline"] = min(
                distances.get("sightline", math.inf), euclidean
            )
            distances["lateral"] = min(distances.get("lateral", math.inf), lateral)
            if longitudinal > 0:
                distances["longitudinal"] = min(
                    distances.get("longitudinal", math.inf), longitudinal
                )
                if lead_gap is None or longitudinal < lead_gap:
                    lead_gap = longitudinal
                    lead_speed = self._projected_speed(actor, forward)
        return distances, lead_gap, lead_speed

    def _ego_axes(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """自车航向的前向/左向单位向量（无位姿时退回生成点 yaw）。"""
        yaw = None
        if self._ego is not None:
            try:
                yaw = float(self._ego.get_transform().rotation.yaw)
            except Exception:  # noqa: BLE001
                yaw = None
        if yaw is None:
            spawn = self._ego_spawn or self._config.ego_vehicle.spawn_point
            yaw = float(spawn.yaw_deg)
        heading = math.radians(yaw)
        return (math.cos(heading), math.sin(heading)), (
            -math.sin(heading),
            math.cos(heading),
        )

    @staticmethod
    def _projected_speed(
        actor: Any, forward: tuple[float, float]
    ) -> Optional[float]:
        """参与者速度沿自车前向的投影（读取失败返回 None）。"""
        try:
            velocity = actor.get_velocity()
            return float(velocity.x) * forward[0] + float(velocity.y) * forward[1]
        except Exception:  # noqa: BLE001
            return None

    def _is_offroad(self, world: Any) -> bool:
        """按 waypoint 车道类型判断是否越界；接口缺失时按「未越界」容错。"""
        if self._ego is None:
            return False
        get_map = getattr(world, "get_map", None)
        if not callable(get_map):
            return False
        try:
            carla_map = get_map()
            getter = getattr(self._ego, "get_location", None)
            if callable(getter):
                location = getter()
            else:
                location = self._ego.get_transform().location
            get_waypoint = getattr(carla_map, "get_waypoint", None)
            if not callable(get_waypoint):
                return False
            waypoint = get_waypoint(location, project_to_road=False)
        except Exception:  # noqa: BLE001  Mock/接口差异
            return False
        if waypoint is None:
            return True
        try:
            import carla  # noqa: PLC0415

            driving = carla.LaneType.Driving
        except ImportError:
            return False
        lane_type = getattr(waypoint, "lane_type", None)
        if lane_type is None:
            return False
        return bool(lane_type != driving)

    @staticmethod
    def _current_speed(actor: Any) -> float:
        try:
            velocity = actor.get_velocity()
            return math.sqrt(velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2)
        except Exception:  # noqa: BLE001
            return 0.0

    def _participant_speed_ms(self, actor: Any) -> float:
        """参与者面板速度（m/s）。

        V3.8.4：脚本化行人（sudden_cross 等）用 ``set_transform`` 推进，CARLA
        ``get_velocity()`` 恒为 0，面板会误显「0.0 m/s 却在移动」。这里改报脚本
        的有效速度（等待/到达=0，dash 用窜入速度），非脚本参与者仍读物理速度。
        """
        for walker in self._scripted_walkers:
            if walker.actor is actor:
                if walker.done or walker.waiting:
                    return 0.0
                if walker.mode == "dash" and walker.dash_speed_ms > 0:
                    return float(walker.dash_speed_ms)
                return float(walker.speed_ms)
        return self._current_speed(actor)

    @staticmethod
    def _apply_vehicle_control(
        actor: Any, throttle: float, brake: float, steer: float
    ) -> None:
        """下发车辆控制（无 CARLA SDK 时用轻量对象兼容单测桩）。"""
        apply = getattr(actor, "apply_control", None)
        if not callable(apply):
            return
        try:
            import carla  # noqa: PLC0415

            control: Any = carla.VehicleControl(
                throttle=throttle, steer=steer, brake=brake
            )
        except ImportError:
            control = SimpleNamespace(throttle=throttle, steer=steer, brake=brake)
        try:
            apply(control)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"apply_control failed: {exc}")

    def _setup_walker_ai(
        self,
        world: Any,
        library: Any,
        actor: Any,
        spawn_point: SpawnPoint,
        participant: TrafficParticipantConfig,
    ) -> None:
        """为行人挂载 AI 控制器并导航横穿自车车道。

        AI 控制器必须用**相对** transform 附着到行人上：CARLA 的
        ``spawn_actor(..., attach_to=actor)`` 语义下 transform 是相对父级的偏移，
        传世界坐标会把控制器摆到「出生点×2」的位置，导航目标因此错位，画面里
        就看不到行人过街。AI 控制器缺失 / 导航启动失败时退化为脚本化直行，
        保证可视化里仍能看到行人行为。
        """
        try:
            import carla  # noqa: PLC0415  惰性导入：无 SDK 环境跳过
        except ImportError:
            logger.warning("carla SDK 不可用，行人保持静态")
            return
        speed = float(participant.speed_ms) if participant.speed_ms > 0 else 1.2
        speed = max(_WALKER_MIN_SPEED, min(_WALKER_MAX_SPEED, speed))
        if speed > 2.0:
            logger.info(
                "Participant '%s' walker speed %.2f m/s 高于常规步速，按平台语义执行",
                participant.participant_id,
                speed,
            )
        destination = self._walker_destination(spawn_point, carla, participant, world)

        # V3.6.2：默认走脚本化横穿（set_transform 逐帧推进，纯运动学、不碰原生
        # 导航）。场景行人的出生点/目标是平台下发的任意坐标，不保证落在导航网格
        # 上；对 off-mesh 点调用 controller.ai.walker.go_to_location 会触发 CARLA
        # 原生 RecastNavigation 深层递归 → Windows 栈溢出（0xC00000FD /
        # code=3221225725），Python try/except 抓不住，直接带走控制面进程（崩在
        # tick 循环开始前，日志表现为 spawn 后无 tick 行）。仅在显式开启
        # SCENE_WALKER_AI_CONTROLLER 且坐标确认在导航网格上时才用原生 AI 控制器。
        if not _walker_ai_controller_enabled():
            self._register_scripted_walker(
                actor, spawn_point, destination, speed, participant, carla
            )
            return

        blueprint = _safe_find(library, "controller.ai.walker")
        if blueprint is None:
            logger.warning(
                f"Participant '{participant.participant_id}' controller.ai.walker "
                "蓝图缺失，退化为脚本化横穿"
            )
            self._register_scripted_walker(
                actor, spawn_point, destination, speed, participant, carla
            )
            return
        try:
            controller = world.spawn_actor(blueprint, carla.Transform(), attach_to=actor)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Participant '{participant.participant_id}' walker controller spawn "
                f"failed: {exc}；退化为脚本化横穿"
            )
            self._register_scripted_walker(
                actor, spawn_point, destination, speed, participant, carla
            )
            return
        self._walker_ai_controllers.append(controller)
        try:
            wrapper = WalkerControllerWrapper(actor, controller)
            wrapper.start_navigation(destination, speed_ms=speed)
            self._walker_controllers.append(wrapper)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Participant '{participant.participant_id}' walker navigation "
                f"failed: {exc}；退化为脚本化横穿"
            )
            self._register_scripted_walker(
                actor, spawn_point, destination, speed, participant, carla
            )
            return
        logger.info(
            "Walker '%s' crossing at %.2f m/s: from=(%.1f,%.1f) to=(%.1f,%.1f) "
            "distance_to_ego=%s",
            participant.participant_id,
            speed,
            spawn_point.x,
            spawn_point.y,
            float(getattr(destination, "x", 0.0)),
            float(getattr(destination, "y", 0.0)),
            _format_distance(self._distance_to_ego(spawn_point)),
        )

    def _walker_destination(
        self,
        spawn_point: SpawnPoint,
        carla_module: Any,
        participant: Optional[TrafficParticipantConfig] = None,
        world: Any = None,
    ) -> Any:
        """计算行人横穿目的地：朝自车车道方向走过整条车道。

        方向取「垂直于自车航向」的横向：行人在自车左侧就向右走，反之向左走。
        这样无论平台下发的 yaw 是「朝车道」还是「顺车道」，画面里都能看到行人
        从路边走向 / 穿过自车行驶线；距离按横向偏离量 + 安全余量取 8~18m，
        比固定 8m 更容易在环视相机里持续可见。

        ``boarding`` 行为改为走向最近的停放车辆（短程），避免长距离横穿。
        """
        if participant is not None and participant.source_behavior_type == "boarding":
            boarding_target = self._boarding_destination(
                spawn_point, participant, carla_module
            )
            if boarding_target is not None:
                return boarding_target
        ego = self._ego_spawn
        heading = math.radians(ego.yaw_deg if ego is not None else spawn_point.yaw_deg)
        # 自车左侧单位向量（yaw 以北向顺时针为正）
        left_x, left_y = -math.sin(heading), math.cos(heading)
        side = 0.0
        if ego is not None:
            side = (spawn_point.x - ego.x) * left_x + (spawn_point.y - ego.y) * left_y
        direction = -1.0 if side > 0 else 1.0
        base_offset = min(18.0, max(8.0, abs(side) + 6.0))
        offset = self._crossing_offset(
            world, carla_module, spawn_point, direction, left_x, left_y,
            base_offset, abs(side),
        )
        x = spawn_point.x + direction * left_x * offset
        y = spawn_point.y + direction * left_y * offset
        z = spawn_point.z if spawn_point.z > 0 else _FALLBACK_Z_M
        return carla_module.Location(x=x, y=y, z=z)

    def _crossing_offset(
        self,
        world: Any,
        carla_module: Any,
        spawn_point: SpawnPoint,
        direction: float,
        left_x: float,
        left_y: float,
        base_offset: float,
        lateral_offset_from_ego: float,
    ) -> float:
        """横穿距离：尽量把终点落到对向路缘外（离开行车道）。

        历史版本用固定几何估算（8~18m），宽路面下终点会落在行车道中间；行人
        done 后长期杵在自车行驶线上，Traffic Manager 会持续刹车、把自车卡在
        低速。现在沿横穿方向逐点探测路面，越过最靠外仍在路面的点后加安全余量；
        无法读图时退回“至少跨过自车车道 + 余量”的几何估算。
        """
        # 保证终点至少跨过自车行驶线再往外留出余量（几何下限）。
        geo_floor = max(base_offset, lateral_offset_from_ego + _CURB_CLEARANCE_M)
        if world is None:
            return min(geo_floor, 30.0)
        try:
            get_map = getattr(world, "get_map", None)
            if not callable(get_map):
                return min(geo_floor, 30.0)
            carla_map = get_map()
            get_waypoint = getattr(carla_map, "get_waypoint", None)
            if not callable(get_waypoint):
                return min(geo_floor, 30.0)
        except Exception:  # noqa: BLE001  Mock / 地图接口差异
            return min(geo_floor, 30.0)
        z = spawn_point.z if spawn_point.z > 0 else _FALLBACK_Z_M
        step = 1.0
        max_len = 40.0
        travelled = 0.0
        last_on_road = 0.0
        on_road_seen = False
        try:
            while travelled <= max_len:
                px = spawn_point.x + direction * left_x * travelled
                py = spawn_point.y + direction * left_y * travelled
                probe = carla_module.Location(x=px, y=py, z=z)
                wp = get_waypoint(probe, project_to_road=True)
                dev = None
                if wp is not None:
                    wloc = wp.transform.location
                    dev = math.hypot(float(wloc.x) - px, float(wloc.y) - py)
                if wp is not None and dev is not None and dev <= _ROAD_SNAP_TOL_M:
                    on_road_seen = True
                    last_on_road = travelled
                elif on_road_seen:
                    break  # 已经走过路面，现在离开 → 对向路缘
                travelled += step
        except Exception:  # noqa: BLE001  单点探测失败不阻断
            return min(geo_floor, 30.0)
        if not on_road_seen:
            # 探测不到任何路面（坐标不在导航网格/地图上）：退回几何估算。
            return min(max(geo_floor, _CROSSING_FALLBACK_OFFSET_M), 30.0)
        return min(max(last_on_road + _CURB_CLEARANCE_M, geo_floor), 30.0)

    def _boarding_destination(
        self,
        spawn_point: SpawnPoint,
        participant: TrafficParticipantConfig,
        carla_module: Any,
    ) -> Optional[Any]:
        """``boarding`` 目标：最近停放车辆侧向 1.0 m；无车则向自车走 2.5 m。"""
        if not self._parked_spawns:
            logger.warning(
                "boarding_target_missing: participant=%s 场景内无停放车辆，"
                "退化为向自车方向走 2.5m",
                participant.participant_id,
            )
            return self._short_walk_toward_ego(spawn_point, carla_module)
        nearest = min(
            self._parked_spawns,
            key=lambda parked: math.hypot(
                parked.x - spawn_point.x, parked.y - spawn_point.y
            ),
        )
        distance = math.hypot(
            nearest.x - spawn_point.x, nearest.y - spawn_point.y
        )
        if distance > 15.0:
            logger.warning(
                "boarding_target_missing: participant=%s 最近停放车辆 %.1fm（>15m），"
                "退化为向自车方向走 2.5m",
                participant.participant_id,
                distance,
            )
            return self._short_walk_toward_ego(spawn_point, carla_module)
        if distance < 1e-3:
            return carla_module.Location(
                x=nearest.x,
                y=nearest.y,
                z=spawn_point.z if spawn_point.z > 0 else _FALLBACK_Z_M,
            )
        dx = (spawn_point.x - nearest.x) / distance
        dy = (spawn_point.y - nearest.y) / distance
        # 车辆侧向 1.0 m 处（从车辆中心朝行人方向偏移）
        return carla_module.Location(
            x=nearest.x + dx * min(1.0, distance),
            y=nearest.y + dy * min(1.0, distance),
            z=spawn_point.z if spawn_point.z > 0 else _FALLBACK_Z_M,
        )

    def _short_walk_toward_ego(
        self, spawn_point: SpawnPoint, carla_module: Any
    ) -> Optional[Any]:
        """退化目标：向自车方向走 2.5m（不足则走到自车位置）。"""
        ego = self._ego_spawn
        if ego is None:
            return None
        dx = ego.x - spawn_point.x
        dy = ego.y - spawn_point.y
        distance = math.hypot(dx, dy)
        if distance < 1e-3:
            return None
        step = min(2.5, distance)
        return carla_module.Location(
            x=spawn_point.x + dx / distance * step,
            y=spawn_point.y + dy / distance * step,
            z=spawn_point.z if spawn_point.z > 0 else _FALLBACK_Z_M,
        )

    def _register_scripted_walker(
        self,
        actor: Any,
        spawn_point: SpawnPoint,
        destination: Any,
        speed_ms: float,
        participant: TrafficParticipantConfig,
        carla_module: Any,
    ) -> None:
        """登记脚本化行人（AI 不可用时每个 tick 直行推向目的地）。

        ``sudden_cross``（鬼探头）走 ``dash`` 模式：先在出生点等待，自车接近
        到触发距离后才快速窜入；其余行为保持 ``cross`` 匀速横穿。
        """
        target = (
            float(getattr(destination, "x", spawn_point.x)),
            float(getattr(destination, "y", spawn_point.y)),
            float(getattr(destination, "z", spawn_point.z)),
        )
        is_sudden = participant.source_behavior_type == "sudden_cross"
        walker = _ScriptedWalker(
            actor=actor,
            destination=target,
            speed_ms=max(0.1, speed_ms),
            carla_module=carla_module,
            spawn=spawn_point,
        )
        # V3.8.18：脚本化横穿是纯运动学（set_transform 逐帧推进），禁用物理
        # 仿真避免双向楔死：旧版行人物理体被刹停自车顶住后引擎回推，横穿走廊
        # 永远走不完（_hazard_in_ego_path 恒真 → 整场 0 m/s 冻结）；自车 likewise
        # 被行人碰撞体楔住无法恢复。禁用后 set_transform 为唯一位姿权威，
        # 与静态障碍的碰撞不受影响（静态物仍走物理）。与自车的碰撞改由
        # _advance_scripted_walkers 的距离接触判定补报（评估不漏判）。
        physics_setter = getattr(actor, "set_simulate_physics", None)
        if callable(physics_setter):
            try:
                physics_setter(False)
            except Exception as exc:  # noqa: BLE001  旧版服务端/桩不支持时降级
                logger.debug(f"Walker set_simulate_physics(False) failed: {exc}")
        if is_sudden:
            walker.mode = "dash"
            walker.waiting = True
            walker.trigger_distance_m = _SUDDEN_CROSS_TRIGGER_M
            walker.dash_speed_ms = _SUDDEN_CROSS_DASH_SPEED_MS
        self._scripted_walkers.append(walker)
        logger.info(
            "Walker '%s' scripted %s crossing: speed=%.2f m/s from=(%.1f,%.1f) "
            "to=(%.1f,%.1f) distance_to_ego=%s%s",
            participant.participant_id,
            "sudden-dash" if is_sudden else "",
            speed_ms,
            spawn_point.x,
            spawn_point.y,
            target[0],
            target[1],
            _format_distance(self._distance_to_ego(spawn_point)),
            (
                f" [wait until ego<{_SUDDEN_CROSS_TRIGGER_M:.0f}m then dash "
                f"{_SUDDEN_CROSS_DASH_SPEED_MS:.1f}m/s]"
                if is_sudden
                else ""
            ),
        )

    def _advance_scripted_walkers(self, delta_seconds: float) -> None:
        """把脚本化行人按速度推向目标点（到达后停止，避免来回抖动）。"""
        if not self._scripted_walkers:
            return
        step_budget = max(0.0, float(delta_seconds))
        for walker in self._scripted_walkers:
            if walker.done:
                continue
            transform_getter = getattr(walker.actor, "get_transform", None)
            setter = getattr(walker.actor, "set_transform", None)
            if not callable(transform_getter) or not callable(setter):
                walker.done = True
                continue
            try:
                transform = transform_getter()
                location = getattr(transform, "location", None)
                if location is None:
                    walker.done = True
                    continue
                # V3.6.3：scenario.tick() 先于 world.tick() 执行，同步模式下首 tick
                # 前 get_transform() 返回占位原点 (0,0,0)。若不判据直接推进，就会把
                # 行人从真实出生点瞬移到世界原点、再横穿整张地图（表现为「动物不见了 /
                # 视区外几百米」）。读数落在原点而登记的出生点不在原点 → 视为未同步，
                # 本 tick 跳过推进（与 _pose_not_synced 同口径），等位姿同步后再走。
                if is_placeholder_origin(
                    SpawnPoint(
                        x=float(location.x),
                        y=float(location.y),
                        z=float(location.z),
                    )
                ) and not is_placeholder_origin(walker.spawn):
                    continue
                # sudden_cross：自车接近前保持在出生点不动（等待窜入时机）。
                # 用自车实时位姿（_ego_live_pose 已忽略首 tick 前的占位读数）判定
                # 水平距离；仍在触发距离之外就本 tick 不推进，到位后切窜入速度。
                if walker.mode == "dash" and walker.waiting:
                    ego_pose = self._ego_live_pose()
                    if ego_pose is None:
                        continue  # 自车尚未同步，本 tick 不判定，继续等待
                    gap = math.hypot(
                        ego_pose.x - float(location.x),
                        ego_pose.y - float(location.y),
                    )
                    if gap > walker.trigger_distance_m:
                        continue  # 自车尚远，保持在路缘等待
                    walker.waiting = False  # 时机已到，发起快速窜入
                dx = walker.destination[0] - float(location.x)
                dy = walker.destination[1] - float(location.y)
                distance = math.hypot(dx, dy)
                if distance <= _WALKER_ARRIVE_EPS_M:
                    walker.done = True
                    continue
                eff_speed = walker.speed_ms
                if walker.mode == "dash" and walker.dash_speed_ms > 0:
                    eff_speed = walker.dash_speed_ms
                step = min(distance, eff_speed * step_budget)
                carla_module = walker.carla_module
                rotation = getattr(transform, "rotation", None)
                new_transform = carla_module.Transform(
                    carla_module.Location(
                        x=float(location.x) + dx / distance * step,
                        y=float(location.y) + dy / distance * step,
                        z=float(location.z),
                    ),
                    carla_module.Rotation(
                        pitch=float(getattr(rotation, "pitch", 0.0) or 0.0),
                        yaw=math.degrees(math.atan2(dy, dx)),
                        roll=float(getattr(rotation, "roll", 0.0) or 0.0),
                    ),
                )
                setter(new_transform)
                # V3.8.18：物理禁用后碰撞传感器不再触发，用距离接触判定补报
                # 与自车的碰撞（每次接触只报一次，避免逐 tick 重复计数）。
                self._report_scripted_walker_contact(walker)
            except Exception as exc:  # noqa: BLE001  单帧推进失败不阻断场景
                logger.debug(f"Scripted walker update failed: {exc}")
                walker.done = True

    def _report_scripted_walker_contact(self, walker: _ScriptedWalker) -> None:
        """脚本化行人与自车接触时补报碰撞事件（V3.8.18，去重）。

        脚本化行人物理仿真已禁用（避免楔死），自车碰撞传感器对其不再
        触发；若不补报，「撞了行人却 no_collision=True」会形成新的假通过。
        接触阈值取行人半径+自车半宽近似（水平距离），与 min_distance_samples
        的欧氏距离口径一致；每次接触只报一次（离开后再次接触可再报）。
        """
        if self._ego is None:
            return
        ego_pose = self._ego_live_pose()
        if ego_pose is None:
            return
        try:
            location = walker.actor.get_transform().location
            gap = math.hypot(ego_pose.x - float(location.x), ego_pose.y - float(location.y))
        except Exception:  # noqa: BLE001  读数失败不补报
            return
        if gap <= _SCRIPTED_WALKER_CONTACT_M:
            if walker.contact_reported:
                return  # 持续接触去重：不得逐 tick 重复计数
            walker.contact_reported = True
            actor_id = int(getattr(walker.actor, "id", -1))
            logger.warning(
                "scripted_walker_contact: 自车与脚本化行人 actor=%s 接触（gap=%.2fm ≤ %.2fm，"
                "物理禁用后距离判定补报碰撞）",
                actor_id,
                gap,
                _SCRIPTED_WALKER_CONTACT_M,
            )
            self._event_detector.on_collision(self.elapsed_seconds, actor_id, 0.0)
        elif gap > _SCRIPTED_WALKER_CONTACT_M * 2.0:
            # 已离开接触区：复位去重标记，再次接触可再报
            walker.contact_reported = False

    def _attach_event_sensors(self, world: Any, library: Any) -> None:
        """按场景事件声明挂载碰撞/车道偏离传感器（事件回调驱动 EventDetector）。"""
        declared = {e.event_type for e in self._config.scene_events}
        if self._ego is None or not declared:
            return
        try:
            import carla  # noqa: PLC0415  惰性导入：无 SDK 环境由装配层拦截
        except ImportError:
            logger.warning("carla SDK 不可用，跳过事件传感器挂载")
            return

        if SceneEventType.COLLISION in declared:
            self._attach_sensor(
                world,
                library,
                "sensor.other.collision",
                carla.Transform(),
                lambda event: self._event_detector.on_collision(
                    self.elapsed_seconds,
                    int(getattr(getattr(event, "other_actor", None), "id", -1)),
                    float(getattr(event, "normal_impulse", carla.Vector3D()).length()),
                ),
            )
        if SceneEventType.LANE_INVASION in declared:
            self._attach_sensor(
                world,
                library,
                "sensor.other.lane_invasion",
                carla.Transform(),
                lambda event: self._event_detector.on_lane_invasion(
                    self.elapsed_seconds,
                    str(getattr(event, "crossed_lane_markings", "")),
                ),
            )

    def _attach_sensor(
        self, world: Any, library: Any, sensor_id: str, transform: Any, callback: Any
    ) -> None:
        blueprint = library.find(sensor_id)
        if blueprint is None:
            logger.warning(f"Sensor blueprint not found: {sensor_id}")
            return
        try:
            sensor = world.spawn_actor(blueprint, transform, attach_to=self._ego)
            sensor.listen(callback)
        except Exception as exc:  # noqa: BLE001  传感器缺失不影响场景主流程
            logger.warning(f"Attach sensor '{sensor_id}' failed: {exc}")
            return
        self._sensors.append(sensor)

    def _attach_sim_sensors(self, world: Any, library: Any) -> None:
        """按 ``config.sensors`` 型号剖面挂载仿真传感器（V3.0.0）。

        流程：剖面名合并 → 蓝图创建 → 按 HUNTER SE 安装位挂载 → 回调统一接入
        ``FrameHub`` / ``McapRecorder`` / ROS2 桥接。单型传感器失败只降级该型，
        不阻断场景（蓝图缺失常见于未部署 hunter_se 资产的环境）。

        另挂一台仅用于可视化主视区的环视相机（``overview`` 流）：它不进入
        ``_sensors`` 计数、不录 mcap、不发 ROS，因此不影响平台数据契约。

        Args:
            world: CARLA World。
            library: BlueprintLibrary。
        """
        if self._ego is None:
            return

        try:
            mount_manager = SensorMountManager(
                self._ego,
                sensor_mount_reference=self._config.sensor_mount_reference,
            )
        except SensorSimulationError as exc:
            logger.warning(f"Sim sensors skipped: mount resolution failed: {exc}")
            return
        except ConfigurationError as exc:
            logger.warning(f"Sim sensors skipped: sensor mount config invalid: {exc}")
            return

        self._attach_overview_camera(world, library, mount_manager)

        specs: list[SensorSpec] = [spec for spec in self._config.sensors if spec.enabled]
        if not specs:
            return

        bundle = resolve_profiles([spec.profile for spec in specs])
        try:
            blueprints = SensorBlueprintFactory(library).create_bundle(bundle)
        except Exception as exc:  # noqa: BLE001  蓝图库异常不得阻断场景
            logger.warning(f"Sim sensor blueprint creation failed: {exc}")
            return
        if not blueprints:
            logger.warning("Sim sensors skipped: no available blueprint")
            return

        attached = 0
        # V3.8.18：标称帧率按型号剖面记录（面板「实际/标称」对比用）
        nominal_by_type: dict[SensorType, float] = {}
        if bundle.lidar is not None:
            nominal_by_type[SensorType.LIDAR] = float(bundle.lidar.rotation_frequency_hz)
        if bundle.rgb_camera is not None:
            nominal_by_type[SensorType.RGB_CAMERA] = float(bundle.rgb_camera.fps)
        if bundle.depth_camera is not None:
            nominal_by_type[SensorType.DEPTH_CAMERA] = float(bundle.depth_camera.fps)
        if bundle.imu is not None:
            nominal_by_type[SensorType.IMU] = float(bundle.imu.frequency_hz)
        for sensor_type, blueprint in blueprints.items():
            stream = _SIM_STREAM_BY_TYPE.get(sensor_type)
            if stream is None:
                # 事件类传感器由 _attach_event_sensors 负责（回调语义不同）
                continue
            try:
                sensor = mount_manager.attach_sensor(sensor_type, blueprint, world)
            except SensorSimulationError as exc:
                logger.warning(f"Sim sensor '{sensor_type.value}' attach skipped: {exc}")
                continue
            self._listen_sensor(sensor, stream)
            self._sensors.append(sensor)
            attached += 1
            nominal = nominal_by_type.get(sensor_type)
            if nominal and nominal > 0:
                self._nominal_fps[stream] = nominal

        if attached:
            self._ros_enabled = any(spec.ros_enabled for spec in specs)
            logger.info(
                f"Sim sensors attached: {attached} (profile={bundle.profile_name}, "
                f"record={self._recorder is not None}, ros={self._ros_enabled})"
            )

    def _attach_overview_camera(
        self, world: Any, library: Any, mount_manager: SensorMountManager
    ) -> None:
        """挂载仅可视化用的环视相机（失败只告警，不影响数据传感器）。"""
        config = load_overview_camera_config()
        if not bool(config.get("enabled", True)):
            logger.info("Overview camera disabled by sensor config")
            return
        try:
            camera_config = RGBCameraConfig(
                width=int(config.get("width", 1920)),
                height=int(config.get("height", 1080)),
                fov_deg=float(config.get("fov_deg", 100.0)),
                fps=float(config.get("fps", 10.0)),
            )
            blueprint = SensorBlueprintFactory(library).create(
                SensorType.RGB_CAMERA, camera_config
            )
            sensor = mount_manager.attach_sensor(
                SensorType.RGB_CAMERA,
                blueprint,
                world,
                mount_id=_OVERVIEW_MOUNT_ID,
            )
        except Exception as exc:  # noqa: BLE001  可视化相机不得阻断场景
            logger.warning(f"Overview camera attach skipped: {exc}")
            return
        self._listen_sensor(sensor, STREAM_OVERVIEW)
        self._overview_sensor = sensor
        logger.info(
            "Overview camera attached: mount=%s fov=%.0f size=%dx%d "
            "(native encode, jpeg %s adaptive, no downscale)",
            _OVERVIEW_MOUNT_ID,
            camera_config.fov_deg,
            camera_config.width,
            camera_config.height,
            "→".join(str(item) for item in _OVERVIEW_QUALITY_LADDER),
        )

    def _listen_sensor(self, sensor: Any, stream: str) -> None:
        """注册传感器回调（回调异常不得冒泡到 CARLA 线程）。"""
        listen = getattr(sensor, "listen", None)
        if not callable(listen):
            return
        listen(lambda data, _stream=stream: self._on_sensor_data(_stream, data))

    def _on_sensor_data(self, stream: str, data: Any) -> None:
        """传感器回调（CARLA tick 线程）：只做拷贝 + 入有界队列。

        编码（BGRA→RGB / 降采样 / JPEG）与 mcap 写入（zstd + 全局锁）均在毫秒级，
        放在 ``world.tick()`` 调用线程里会把 50Hz 循环拖慢，导致 VIL 时间轴错位；
        因此交由 ``hunter-sensor-encoder`` 线程处理，队满时丢帧并计数。
        """
        if not self._frame_accepting:
            return
        try:
            frame = _frame_of(stream, data)
        except Exception as exc:  # noqa: BLE001  单帧抽取异常不得冒泡到 CARLA
            self._warn_frame_error("extract", stream, exc)
            return
        if stream == STREAM_OVERVIEW:
            self._enqueue_overview(frame)
            return
        self._ensure_frame_worker()
        with self._frame_cond:
            self._frame_pending += 1
        try:
            self._frame_queue.put_nowait(frame)
        except queue.Full:
            with self._frame_cond:
                self._frame_pending -= 1
            SENSOR_FRAMES_DROPPED_TOTAL.labels(stream=stream).inc()
            self._frame_drops[stream] = self._frame_drops.get(stream, 0) + 1

    def _ensure_frame_worker(self) -> None:
        """惰性启动传感器编码线程（幂等）。"""
        with self._frame_worker_lock:
            if self._frame_worker is not None and self._frame_worker.is_alive():
                return
            self._frame_stop.clear()
            self._frame_worker = threading.Thread(
                target=self._frame_worker_loop,
                name="hunter-sensor-encoder",
                daemon=True,
            )
            self._frame_worker.start()
            logger.debug("Sensor encoder thread started")

    def _frame_worker_loop(self) -> None:
        """消费队列执行编码/落盘/ROS 发布（单线程串行，保持帧序）。"""
        while not self._frame_stop.is_set():
            try:
                frame = self._frame_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._dispatch_sensor_data(frame.stream, frame.as_measurement())
            except Exception as exc:  # noqa: BLE001  单帧异常不得中断编码线程
                self._warn_frame_error("dispatch", frame.stream, exc)
            finally:
                with self._frame_cond:
                    self._frame_pending -= 1
                    self._frame_cond.notify_all()

    def drain_sensor_frames(self, timeout: float = _SENSOR_WORKER_JOIN_TIMEOUT_S) -> bool:
        """等待已入队的传感器帧处理完毕（收尾与单测使用）。

        Args:
            timeout: 最长等待秒数。

        Returns:
            True 表示全部帧已编码完成（含已出队但仍在处理的那一帧）。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._frame_cond:
            while self._frame_pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._frame_cond.wait(remaining)
        return True

    def _stop_frame_worker(self) -> None:
        """排空队列后停止编码线程（幂等）。"""
        with self._frame_worker_lock:
            worker, self._frame_worker = self._frame_worker, None
        if worker is None:
            return
        self.drain_sensor_frames()
        self._frame_stop.set()
        worker.join(timeout=_SENSOR_WORKER_JOIN_TIMEOUT_S)
        if worker.is_alive():
            # 不在此处强杀线程：join 超时说明正在编码大帧，继续等下一帧即可
            logger.warning(
                "Sensor encoder thread still alive after %.1fs; leaving it to exit "
                "(frames will be dropped)",
                _SENSOR_WORKER_JOIN_TIMEOUT_S,
            )
        # 丢弃残留帧并复位计数：避免下一场景的 worker 重放旧帧 / 计数漂移
        while True:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break
        with self._frame_cond:
            self._frame_pending = 0
        logger.debug("Sensor encoder thread stopped")

    # ── 环视可视化编码（独立队列/线程，避免被数据传感器饿死） ─────────────

    def _enqueue_overview(self, frame: _SensorFrame) -> None:
        """环视帧入独立队列；队满丢弃最旧帧，保证画面始终是最新一帧。"""
        self._ensure_overview_worker()
        try:
            self._overview_queue.put_nowait(frame)
        except queue.Full:
            try:
                self._overview_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._overview_queue.put_nowait(frame)
            except queue.Full:
                SENSOR_FRAMES_DROPPED_TOTAL.labels(stream=STREAM_OVERVIEW).inc()
                self._frame_drops[STREAM_OVERVIEW] = (
                    self._frame_drops.get(STREAM_OVERVIEW, 0) + 1
                )

    def _ensure_overview_worker(self) -> None:
        """惰性启动环视编码线程（幂等）。"""
        with self._overview_worker_lock:
            if self._overview_worker is not None and self._overview_worker.is_alive():
                return
            self._overview_stop.clear()
            self._overview_worker = threading.Thread(
                target=self._overview_worker_loop,
                name="hunter-overview-encoder",
                daemon=True,
            )
            self._overview_worker.start()
            logger.debug("Overview encoder thread started")

    def _overview_worker_loop(self) -> None:
        """消费环视队列并编码推流（与数据传感器编码互不阻塞）。"""
        while not self._overview_stop.is_set():
            try:
                frame = self._overview_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            # 队列里已有更新帧时丢弃当前帧：编码落后时直接追到最新画面，
            # 而不是把积压帧逐帧编完（后者会让画面延迟持续增长 = 卡顿）
            while True:
                try:
                    frame = self._overview_queue.get_nowait()
                except queue.Empty:
                    break
            started = time.perf_counter()
            try:
                self._dispatch_sensor_data(frame.stream, frame.as_measurement())
            except Exception as exc:  # noqa: BLE001
                self._warn_frame_error("overview", frame.stream, exc)
            finally:
                self._track_overview_encode_cost(time.perf_counter() - started)

    def _overview_quality(self) -> int:
        """当前环视 JPEG 质量（自适应阶梯当前档位，不低于数据流质量）。"""
        ladder = _OVERVIEW_QUALITY_LADDER
        index = min(max(self._overview_quality_index, 0), len(ladder) - 1)
        return max(int(self._screenshot_quality), int(ladder[index]))

    def _track_overview_encode_cost(self, cost_s: float) -> None:
        """统计环视单帧编码耗时：持续超预算逐档降质，持续宽裕升回（V3.5.5）。

        旧版只告警不处置（V3.4.4），结果是现场看到 ``Overview encode slow`` 后
        必须人工改配置重启；而卡顿的唯一自动缓解手段（丢积压帧）已在队列侧做掉，
        编码本身跟不上目标帧率时只能降单帧成本。
        """
        self._warn_overview_slow(cost_s)
        if cost_s > _OVERVIEW_SLOW_ENCODE_S:
            self._overview_fast_streak = 0
            self._overview_slow_streak += 1
            if self._overview_slow_streak >= _OVERVIEW_SLOW_STREAK:
                self._overview_slow_streak = 0
                self._adapt_overview_quality(down=True, cost_s=cost_s)
            return
        self._overview_slow_streak = 0
        self._overview_fast_streak += 1
        if self._overview_fast_streak >= _OVERVIEW_FAST_STREAK:
            self._overview_fast_streak = 0
            self._adapt_overview_quality(down=False, cost_s=cost_s)

    def _adapt_overview_quality(self, *, down: bool, cost_s: float) -> None:
        """按阶梯升降档，直到实际生效质量真的变化（V3.5.5）。

        阶梯值可能被 ``screenshot_quality`` 夹住（主视区画质不低于数据流是有意
        策略），因此逐档推进到生效值真的改变为止，避免日志报了一档、实际没变；
        已到端点仍无变化则：降档给限频提示，升档静默返回。
        """
        ladder = _OVERVIEW_QUALITY_LADDER
        floor = int(self._screenshot_quality)
        previous = self._overview_quality()
        index = min(max(self._overview_quality_index, 0), len(ladder) - 1)
        effective = previous
        for _ in range(len(ladder)):
            index += 1 if down else -1
            if index < 0 or index >= len(ladder):
                if down:
                    self._warn_overview_floor(previous, cost_s)
                return
            effective = max(floor, int(ladder[index]))
            if effective != previous:
                break
        self._overview_quality_index = index
        logger.warning(
            "Overview encode adapted: jpeg quality %d→%d (%.0f ms/frame, "
            "budget %.0f ms, %s after %d consecutive %s frames)",
            previous,
            effective,
            cost_s * 1000.0,
            _OVERVIEW_SLOW_ENCODE_S * 1000.0,
            "downgrade" if down else "restore",
            _OVERVIEW_SLOW_STREAK if down else _OVERVIEW_FAST_STREAK,
            "slow" if down else "fast",
        )

    def _warn_overview_floor(self, quality: int, cost_s: float) -> None:
        """已到最低档仍超预算：限频提示需改分辨率/帧率（需重建传感器，不自动做）。"""
        now = time.monotonic()
        if self._overview_floor_at and now - self._overview_floor_at < _OVERVIEW_SLOW_LOG_INTERVAL_S:
            return
        self._overview_floor_at = now
        logger.warning(
            "Overview encode still slow at lowest jpeg quality %d (%.0f ms/frame > "
            "%.0f ms budget): lower configs/sensor_configs.json overview_camera "
            "width/height or fps (and ADAPTER_STREAM_FPS) for a smoother main view",
            quality,
            cost_s * 1000.0,
            _OVERVIEW_SLOW_ENCODE_S * 1000.0,
        )

    def _warn_overview_slow(self, cost_s: float) -> None:
        """环视单帧编码耗时过长告警（限频，附带被抑制条数）。"""
        if cost_s <= _OVERVIEW_SLOW_ENCODE_S:
            return
        now = time.monotonic()
        if self._overview_slow_at and now - self._overview_slow_at < _OVERVIEW_SLOW_LOG_INTERVAL_S:
            self._overview_slow_suppressed += 1
            return
        suppressed = self._overview_slow_suppressed
        self._overview_slow_at = now
        self._overview_slow_suppressed = 0
        suffix = f" (+{suppressed} suppressed)" if suppressed else ""
        logger.warning(
            "Overview encode slow: %.0f ms/frame (> %.0f ms budget), "
            "main view may stutter%s",
            cost_s * 1000.0,
            _OVERVIEW_SLOW_ENCODE_S * 1000.0,
            suffix,
        )

    def _stop_overview_worker(self) -> None:
        """停止环视编码线程并清空队列（幂等）。"""
        with self._overview_worker_lock:
            worker, self._overview_worker = self._overview_worker, None
        if worker is None:
            return
        self._overview_stop.set()
        worker.join(timeout=_SENSOR_WORKER_JOIN_TIMEOUT_S)
        if worker.is_alive():
            logger.warning(
                "Overview encoder thread still alive after %.1fs; frames will be dropped",
                _SENSOR_WORKER_JOIN_TIMEOUT_S,
            )
        while True:
            try:
                self._overview_queue.get_nowait()
            except queue.Empty:
                break

    def _warn_frame_error(self, stage: str, stream: str, exc: Exception) -> None:
        """帧取数/编码失败告警：每流首次立即报，之后按窗口聚合成一条。

        真机测量对象形态不匹配时，静默降级会让画面/传感器数据全空且无任何线索
        （V3.3.4 现场即因此无法从日志定位），故必须留下可视化面板可见的 WARNING；
        限频是为了避免 50Hz 下形成日志风暴。
        """
        now = time.monotonic()
        with self._frame_error_lock:
            suppressed = self._frame_error_suppressed.get(stream, 0) + 1
            if stream in self._frame_error_at:
                if now - self._frame_error_at[stream] < _SENSOR_ERROR_LOG_INTERVAL_S:
                    self._frame_error_suppressed[stream] = suppressed
                    return
            else:
                suppressed = 1
            self._frame_error_at[stream] = now
            self._frame_error_suppressed[stream] = 0
        suffix = f" (+{suppressed - 1} suppressed)" if suppressed > 1 else ""
        logger.warning(f"Sensor frame {stage} failed ({stream}): {exc}{suffix}")

    def _dispatch_sensor_data(self, stream: str, data: Any) -> None:
        """实际分发逻辑（拆分便于单测直接调用）。

        V3.8.18 时钟统一：``ts`` 为 CARLA 仿真经过秒（仅供 FrameHub fps
        滑窗/面板仿真时刻列）；mcap 的 ``log_time_ns`` 改用墙钟 epoch ns，
        离线 bag 工具才能按真实时间轴回放；仿真经过秒另存消息 meta。
        """
        ts = float(getattr(data, "timestamp", 0.0)) or time.time()
        wall_ts = time.time()
        log_time_ns = int(wall_ts * 1e9)
        frame_id = _FRAME_IDS.get(stream, stream)

        if stream == STREAM_OVERVIEW:
            # 仅可视化：推 FrameHub 供主视区取帧，不录 mcap、不发 ROS。
            # 主视区必须看到 CARLA 原生画面：不再套用数据流的 stream_max_width
            # 降采样（默认 640 会把 1920x1080 压成 640 宽，主视区明显模糊），
            # JPEG 质量由自适应阶梯给出（默认档 = _OVERVIEW_JPEG_QUALITY）。
            raw, width, height = self._raw_image(data)
            quality = self._overview_quality()
            encoded = encode_rgb_frame(
                raw,
                width,
                height,
                quality=quality,
                max_width=0,
            )
            if self._frame_hub is not None:
                self._frame_hub.push(
                    stream,
                    ts,
                    encoded.payload,
                    {
                        "content_type": encoded.content_type,
                        "format": encoded.format,
                        "width": encoded.width,
                        "height": encoded.height,
                        "quality": quality,
                        "sim_ts": ts,
                    },
                )
            return

        if stream == STREAM_RGB:
            raw, width, height = self._raw_image(data)
            encoded = encode_rgb_frame(
                raw,
                width,
                height,
                quality=self._screenshot_quality,
                max_width=self._stream_max_width,
            )
            if self._frame_hub is not None:
                self._frame_hub.push(
                    stream,
                    ts,
                    encoded.payload,
                    {
                        "content_type": encoded.content_type,
                        "format": encoded.format,
                        "width": encoded.width,
                        "height": encoded.height,
                        "sim_ts": ts,
                    },
                )
            if self._recorder is not None:
                # 录制保留原始 BGRA（无损，便于离线重编码）
                self._recorder.push_stream(
                    stream,
                    log_time_ns,
                    build_image_message(raw, width, height, "bgra8", frame_id, log_time_ns),
                    meta={"sim_ts": ts},
                )
            self._publish_ros("rgb", data)
            return

        if stream == STREAM_DEPTH:
            raw, width, height = self._raw_image(data)
            if self._frame_hub is not None or self._recorder is not None:
                # 米制深度只算一次：FrameHub 暴露最新深度图（调试/上层取用），录制落 32FC1
                meters = depth_bgra_to_meters(raw, width, height).tobytes()
                if self._frame_hub is not None:
                    self._frame_hub.push(
                        stream,
                        ts,
                        meters,
                        {
                            "encoding": "32FC1",
                            "width": width,
                            "height": height,
                            "frame_id": frame_id,
                            "sim_ts": ts,
                        },
                    )
                if self._recorder is not None:
                    self._recorder.push_stream(
                        stream,
                        log_time_ns,
                        build_image_message(meters, width, height, "32FC1", frame_id, log_time_ns),
                        meta={"sim_ts": ts},
                    )
            self._publish_ros("depth", data)
            return

        if stream == "lidar":
            payload, points = self._lidar_xyz(points_bytes=_measurement_bytes(data))
            if self._frame_hub is not None:
                self._frame_hub.push(stream, ts, payload, {"points": points, "sim_ts": ts})
            if self._recorder is not None and payload:
                self._recorder.push_stream(
                    stream,
                    log_time_ns,
                    build_pointcloud2_message(payload, points, frame_id, log_time_ns),
                    meta={"sim_ts": ts},
                )
            self._publish_ros("lidar", data)
            return

        if stream == "imu":
            accel = _vec3(getattr(data, "accelerometer", None))
            gyro = _vec3(getattr(data, "gyroscope", None))
            if self._frame_hub is not None:
                self._frame_hub.push(
                    stream, ts, repr((accel, gyro)).encode("utf-8"), {"sim_ts": ts}
                )
            if self._recorder is not None:
                self._recorder.push_stream(
                    stream,
                    log_time_ns,
                    build_imu_message(accel, gyro, frame_id, log_time_ns),
                    meta={"sim_ts": ts},
                )
            self._publish_ros("imu", data)

    def _publish_ros(self, stream: str, data: Any) -> None:
        """可选 ROS2 话题发布（未启用/未初始化/无 rclpy 时静默跳过）。"""
        if not self._ros_enabled or self._ros_bridge is None:
            return
        if not getattr(self._ros_bridge, "is_initialized", False):
            return
        publishers = {
            "rgb": getattr(self._ros_bridge, "publish_rgb_image", None),
            "depth": getattr(self._ros_bridge, "publish_depth_image", None),
            "lidar": getattr(self._ros_bridge, "publish_lidar", None),
            "imu": getattr(self._ros_bridge, "publish_imu", None),
        }
        publish = publishers.get(stream)
        if not callable(publish):
            return
        try:
            publish(data)
        except Exception as exc:  # noqa: BLE001  ROS2 发布失败不影响仿真
            logger.debug(f"ROS2 publish skipped ({stream}): {exc}")

    @staticmethod
    def _raw_image(data: Any) -> tuple[bytes, int, int]:
        """取 CARLA 图像帧的原始字节与尺寸。"""
        raw = _measurement_bytes(data)
        return raw, int(getattr(data, "width", 0)), int(getattr(data, "height", 0))

    @staticmethod
    def _lidar_xyz(points_bytes: bytes) -> tuple[bytes, int]:
        """把 CARLA LiDAR（x,y,z,intensity float32 四元组）转为 PointCloud2 字节序。

        CARLA 输出已是小端 float32×4，与 PointCloud2 的 x/y/z/intensity 布局一致，
        因此仅做长度校验后直传，无需逐点重写。

        Returns:
            ``(payload, point_count)``；长度不合法时返回 ``(b"", 0)``。
        """
        if not points_bytes or len(points_bytes) % _LIDAR_POINT_BYTES != 0:
            return b"", 0
        return points_bytes, len(points_bytes) // _LIDAR_POINT_BYTES

    @staticmethod
    def _find_blueprint(library: Any, blueprint_id: str, prefix: str) -> Any:
        candidates = [blueprint_id]
        if prefix and not blueprint_id.startswith(prefix):
            candidates.append(f"{prefix}{blueprint_id}")
        for candidate in candidates:
            blueprint = _safe_find(library, candidate)
            if blueprint is not None:
                return blueprint
        raise CarlaSimulationError(
            "builtin_scenario.find_blueprint",
            f"未找到蓝图 '{blueprint_id}'（候选：{candidates}），请检查场景配置或 CARLA 资源",
        )

    @staticmethod
    def _fallback_vehicle_blueprint(library: Any) -> Any:
        """自车蓝图回退：优先 mpc.gen_01（尺寸最接近 HUNTER SE），其次 tesla.model3。"""
        for candidate in ("vehicle.micro.mpc.gen_01", "vehicle.tesla.model3"):
            blueprint = _safe_find(library, candidate)
            if blueprint is not None:
                return blueprint
        try:
            matches = library.filter("vehicle.*")
        except Exception:  # noqa: BLE001  蓝图库不支持 filter 时视为无可用车辆
            matches = []
        if matches:
            return matches[0]
        raise CarlaSimulationError(
            "builtin_scenario.fallback_blueprint",
            "蓝图库中没有可用车辆蓝图，无法生成自车",
        )

    def _spawn_variant_index(self) -> int:
        """同批实例分散序号（缺省 0，保持单实例可复现）。"""
        raw = self._config.extra_params.get("spawn_variant", 0)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _tag_ego_role(actor: Any) -> None:
        """给自车打 ``role_name=hero``：自车引用丢失时可按角色名从世界找回。"""
        setter = getattr(actor, "set_attribute", None)
        if not callable(setter):
            return
        try:
            setter("role_name", "hero")
        except Exception as exc:  # noqa: BLE001  个别 Actor 不支持属性写入
            logger.debug(f"ego role_name set failed: {exc}")

    @staticmethod
    def _actual_ego_spawn(actor: Any, fallback: Optional[SpawnPoint]) -> Optional[SpawnPoint]:
        """读取自车实际 transform 作为参与者偏移基准。

        同步模式下首个 tick 之前该读数是世界原点 ``(0,0,0)`` 而不是真实出生位姿，
        直接采信会把参与者相对偏移叠加到占位原点上（V3.5.3 根因）；此处按未同步
        判据回退到实际生效的候选出生点。
        """
        getter = getattr(actor, "get_transform", None)
        if callable(getter):
            try:
                pose = transform_to_spawn_point(getter())
            except Exception as exc:  # noqa: BLE001  读取失败退回候选点，不阻断场景
                logger.debug(f"ego_transform_unavailable: {exc}")
                return fallback
            if _pose_not_synced(pose, fallback):
                logger.warning(
                    "ego_transform_not_synced: 自车首 tick 前 get_transform() 返回占位原点，"
                    "改用出生点 (%.2f,%.2f,%.2f,yaw=%.1f) 作为参与者偏移基准",
                    fallback.x if fallback else 0.0,
                    fallback.y if fallback else 0.0,
                    fallback.z if fallback else 0.0,
                    fallback.yaw_deg if fallback else 0.0,
                )
                return fallback
            return pose
        return fallback

    def _make_transform(self, world: Any, spawn_point: SpawnPoint) -> Any:
        if self._transform_factory is not None:
            return self._transform_factory(world, spawn_point)
        try:
            import carla  # noqa: PLC0415
        except ImportError as exc:
            raise CarlaSimulationError(
                "builtin_scenario.make_transform",
                "CARLA Python SDK 不可用，无法生成 Actor（请安装 carla==0.9.16）",
            ) from exc

        z = spawn_point.z if spawn_point.z > 0 else self._ground_z(world, carla, spawn_point)
        return carla.Transform(
            carla.Location(x=spawn_point.x, y=spawn_point.y, z=z),
            carla.Rotation(
                pitch=spawn_point.pitch_deg,
                yaw=spawn_point.yaw_deg,
                roll=spawn_point.roll_deg,
            ),
        )

    @staticmethod
    def _ground_z(world: Any, carla_module: Any, spawn_point: SpawnPoint) -> float:
        """按地图车道高度推算生成高度（z<=0 时使用）。"""
        try:
            waypoint = world.get_map().get_waypoint(
                carla_module.Location(x=spawn_point.x, y=spawn_point.y, z=100.0)
            )
        except Exception:  # noqa: BLE001  地图无该车道/接口缺失时兜底
            return _FALLBACK_Z_M
        if waypoint is None:
            return _FALLBACK_Z_M
        try:
            return float(waypoint.transform.location.z) + _DEFAULT_CLEARANCE_M
        except Exception:  # noqa: BLE001
            return _FALLBACK_Z_M

    @staticmethod
    def _spawn_actor(world: Any, blueprint: Any, transform: Any) -> Any:
        try_spawn = getattr(world, "try_spawn_actor", None)
        if callable(try_spawn):
            return try_spawn(blueprint, transform)
        return world.spawn_actor(blueprint, transform)


def _frame_of(stream: str, data: Any) -> _SensorFrame:
    """把 CARLA 测量对象抽取为可跨线程的不可变载荷（数据在此一次性拷贝）。

    Args:
        stream: 流名（``rgb`` / ``depth`` / ``lidar`` / ``imu`` / ``overview``）。
        data: CARLA 传感器测量对象（或单测桩）。

    Returns:
        仅含基本类型的帧载荷。
    """
    ts = float(getattr(data, "timestamp", 0.0)) or time.time()
    if stream in (STREAM_RGB, STREAM_DEPTH, STREAM_OVERVIEW):
        raw, width, height = BuiltinScenario._raw_image(data)
        return _SensorFrame(stream=stream, ts=ts, payload=raw, width=width, height=height)
    if stream == "lidar":
        points = _measurement_bytes(data)
        return _SensorFrame(stream=stream, ts=ts, payload=points)
    if stream == "imu":
        return _SensorFrame(
            stream=stream,
            ts=ts,
            accel=_vec3(getattr(data, "accelerometer", None)),
            gyro=_vec3(getattr(data, "gyroscope", None)),
        )
    return _SensorFrame(stream=stream, ts=ts)


def _measurement_bytes(data: Any) -> bytes:
    """取测量对象的原始字节载荷。

    官方 ``carla.Image`` / ``carla.LidarMeasurement`` 的字节在 ``raw_data``（两者都
    没有 ``data`` 属性）；按 ``data`` 取使真机拿到空字节，表现为画面不出图、
    点云为空。``data`` 仅用于兼容旧版 SDK 与内部跨线程帧对象。
    """
    raw = getattr(data, "raw_data", None)
    if raw is None:
        raw = getattr(data, "data", b"")
    return bytes(raw or b"")


def _vec3(value: Any) -> tuple[float, float, float]:
    """把三元组量转为 ``(x, y, z)``（非法输入回退零向量）。

    官方 ``carla.Vector3D``（``IMUMeasurement.accelerometer/gyroscope``）只有
    ``x/y/z`` 属性且不可迭代，故先按属性取，再回退序列形态（兼容内部
    跨线程帧元组与单测桩）。
    """
    if value is None:
        return (0.0, 0.0, 0.0)
    try:
        return (float(value.x), float(value.y), float(value.z))
    except AttributeError:
        pass
    except (TypeError, ValueError):  # noqa: PERF203  属性存在但非数值时视为非法
        return (0.0, 0.0, 0.0)
    try:
        values = [float(item) for item in value]
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)
    if len(values) < 3:
        return (0.0, 0.0, 0.0)
    return (values[0], values[1], values[2])


def _pose_not_synced(
    pose: Optional[SpawnPoint], reference: Optional[SpawnPoint]
) -> bool:
    """实时位姿是否为「同步模式首 tick 前的未同步占位读数」。

    判据：读数落在世界原点，而已知的预期位置不在原点 —— Actor 不可能在两个
    tick 之间瞬移到 ``(0,0,0)``，此时读数是未同步的占位值而非真实位姿。
    """
    if pose is None or reference is None or not is_placeholder_origin(pose):
        return False
    return not is_placeholder_origin(reference)


def _safe_find(library: Any, blueprint_id: str) -> Any:
    """蓝图查找：CARLA 对不存在的 ID 抛 IndexError，这里统一降级为 None。"""
    try:
        return library.find(blueprint_id)
    except Exception:  # noqa: BLE001  缺失蓝图交由候选/回退逻辑处理
        return None


def _format_distance(distance: Optional[float]) -> str:
    """距离格式化（日志用；无法测量时返回 n/a）。"""
    return "n/a" if distance is None else f"{distance:.1f}m"


def build_default_scenario(
    config: SceneConfig,
    state_manager: SceneStateManager,
    event_detector: EventDetector,
    transform_factory: Optional[TransformFactory] = None,
) -> BuiltinScenario:
    """构造内置场景执行器（供 SceneRunnerService 默认使用）。"""
    return BuiltinScenario(config, state_manager, event_detector, transform_factory)


__all__ = ["BuiltinScenario", "TransformFactory", "build_default_scenario"]
