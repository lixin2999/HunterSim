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

#: 环视单帧编码耗时告警阈值（秒）：1080p@10Hz 的单帧预算约 100 ms，
#: 超过该值说明编码跟不上下集，画面会表现为卡顿，需要在日志里直接可见
_OVERVIEW_SLOW_ENCODE_S = 0.12
#: 环视慢编码告警限频窗口（秒）
_OVERVIEW_SLOW_LOG_INTERVAL_S = 60.0

#: 交通参与者「可视距离」告警阈值（米）：超出该距离时环视相机基本拍不到，
#: 日志给出 WARNING 便于定位「参与者生成了但画面里看不到」
_MAX_VISIBLE_DISTANCE_M = 80.0

#: 行人脚本化横穿的到达判定阈值（米）
_WALKER_ARRIVE_EPS_M = 0.3

#: 行人速度物理范围（m/s）：平台下发的 crossing_speed 可能高达 3+ m/s，
#: 不能沿用旧版 1.4 m/s 硬上限，否则鬼探头会「慢动作」。
_WALKER_MIN_SPEED = 0.3
_WALKER_MAX_SPEED = 4.0

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
        done: 是否已到达/放弃（到达后不再抖动）。
    """

    actor: Any
    destination: tuple[float, float, float]
    speed_ms: float
    carla_module: Any
    done: bool = False


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
        # 已行驶里程积分（平台事件 traveled_distance 用）
        self._traveled_distance_m: float = 0.0
        # 车类 prop（停放/事故/故障车）生成位置，boarding 行为据此选择目标
        self._parked_spawns: list[SpawnPoint] = []
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

    # ── CustomScenarioBase 实现 ───────────────────────────────────────────

    def setup(self, world: Any) -> None:
        """生成自车、交通参与者、事件传感器与仿真传感器。"""
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
        self._traveled_distance_m = 0.0
        self._parked_spawns.clear()

        library = self._blueprint_library(world)
        self._ego = self._spawn_ego(world, library)
        self._collect_parked_spawns()
        requested = list(self._config.traffic_participants)
        spawned_ids: list[str] = []
        for participant in requested:
            if self._spawn_participant(world, library, participant):
                spawned_ids.append(participant.participant_id)
        self._attach_event_sensors(world, library)
        self._attach_sim_sensors(world, library)
        self._configure_ego_motion()

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

    def tick(self, world: Any, timestamp: float, delta_seconds: float) -> None:
        """每帧推进自车与交通参与者行为。

        车辆由 Traffic Manager autopilot 保持车道内行驶；本方法在每个 tick
        把场景化行为（减速 / 切入 / 停车等）翻译成 TM 期望速度或手动控制，
        使可视化里自车与参与者真正按场景设置运动。行人由 AI 控制器导航。
        """
        self._apply_ego_manual_speed(delta_seconds)
        self._advance_scripted_walkers(delta_seconds)
        self._update_traveled_distance(delta_seconds)
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
            self._event_detector.update_scene_triggers(timestamp, metrics)
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
        for actor in ([self._ego] if self._ego is not None else []) + self._actors:
            try:
                actor.destroy()
            except Exception as exc:  # noqa: BLE001  Actor 可能已被外部销毁
                logger.debug(f"Actor destroy failed: {exc}")
        self._ego = None
        self._ego_spawn = None
        self._actors.clear()
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
        base = (
            self._ego_spawn
            if self._ego_spawn is not None
            else self._config.ego_vehicle.spawn_point
        )
        try:
            spawn_point = resolve_actor_spawn(
                base, participant.spawn_point, self._config.spawn_semantics
            )
        except Exception as exc:  # noqa: BLE001  坐标非法不影响其他参与者
            logger.warning(
                f"Participant '{participant.participant_id}' spawn skipped: {exc}"
            )
            return False

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
        base = (
            self._ego_spawn
            if self._ego_spawn is not None
            else self._config.ego_vehicle.spawn_point
        )
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
        base = self._ego_spawn
        yaw = math.radians(base.yaw_deg if base is not None else spawn_point.yaw_deg)
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
        """参与者出生点到自车的水平距离（无自车基准时返回 None）。"""
        ego = self._ego_spawn
        if ego is None:
            return None
        return math.hypot(spawn_point.x - ego.x, spawn_point.y - ego.y)

    def _log_participant_spawned(
        self, participant: TrafficParticipantConfig, actor: Any, spawn_point: SpawnPoint
    ) -> None:
        """参与者生成成功日志 + 「可能拍不到」告警（可视化验收排障用）。"""
        distance = self._distance_to_ego(spawn_point)
        distance_text = "n/a" if distance is None else f"{distance:.1f}m"
        behavior = getattr(participant.behavior, "value", participant.behavior)
        logger.info(
            "Participant '%s' spawned: actor=%s blueprint=%s type=%s behavior=%s "
            "spawn=(%.1f,%.1f,%.1f,yaw=%.0f) distance_to_ego=%s",
            participant.participant_id,
            getattr(actor, "id", "n/a"),
            getattr(actor, "type_id", participant.blueprint),
            participant.actor_type,
            behavior,
            spawn_point.x,
            spawn_point.y,
            spawn_point.z,
            spawn_point.yaw_deg,
            distance_text,
        )
        if participant.behavior == ScenarioBehaviorType.STATIC:
            return
        if distance is not None and distance > _MAX_VISIBLE_DISTANCE_M:
            logger.warning(
                "Participant '%s' 距自车 %.1fm（> %.0fm），环视主视区可能拍不到；"
                "如需看到行为请收小平台下发的相对偏移",
                participant.participant_id,
                distance,
                _MAX_VISIBLE_DISTANCE_M,
            )

    # ── 运动/行为控制 ─────────────────────────────────────────────────────

    def _configure_ego_motion(self) -> None:
        """应用自车初始速度（autopilot 优先交给 TM，否则手动油门）。"""
        if self._ego is None:
            return
        speed = float(self._config.ego_vehicle.initial_speed_ms)
        if speed <= 0:
            return
        if self._config.ego_vehicle.autopilot and self._set_desired_speed(self._ego, speed):
            logger.info(f"Ego initial speed set via Traffic Manager: {speed:.1f} m/s")
            return
        self._ego_manual_speed = speed
        logger.info(f"Ego initial speed set via manual control: {speed:.1f} m/s")

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
        """非 autopilot 自车的简单速度保持（P 控制）。"""
        if self._ego is None or self._ego_manual_speed <= 0:
            return
        error = self._ego_manual_speed - self._current_speed(self._ego)
        throttle = min(1.0, max(0.0, error * 0.5))
        brake = 1.0 if error < -1.0 else 0.0
        self._apply_vehicle_control(
            self._ego,
            throttle=throttle,
            brake=brake,
            steer=self._ego_initial_steer,
        )

    def _update_traveled_distance(self, delta_seconds: float) -> None:
        """积分自车速度得到已行驶里程（供 traveled_distance 事件使用）。"""
        if self._ego is None:
            return
        step = max(0.0, min(float(delta_seconds), 1.0))
        self._traveled_distance_m += max(0.0, self._current_speed(self._ego)) * step

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
        try:
            accel = self._ego.get_acceleration()
            metrics.acceleration_ms2 = math.sqrt(
                float(accel.x) ** 2 + float(accel.y) ** 2 + float(accel.z) ** 2
            )
        except Exception:  # noqa: BLE001  Mock/无位姿 Actor
            metrics.acceleration_ms2 = 0.0
        metrics.offroad = self._is_offroad(world)

        distances, lead_gap, lead_speed = self._relative_gaps()
        metrics.relative_distance_m = distances
        if lead_gap is not None:
            metrics.time_headway_s = lead_gap / max(0.1, metrics.speed_ms)
            closing = metrics.speed_ms - (lead_speed or 0.0)
            if closing > 0.1:
                metrics.time_to_collision_s = lead_gap / closing
        return metrics

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

        lead_gap: Optional[float] = None
        lead_speed: Optional[float] = None
        for actor in self._actors:
            try:
                location = actor.get_transform().location
            except Exception:  # noqa: BLE001
                continue
            dx = float(location.x) - ex
            dy = float(location.y) - ey
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
        destination = self._walker_destination(spawn_point, carla, participant)

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
        offset = min(18.0, max(8.0, abs(side) + 6.0))
        x = spawn_point.x + direction * left_x * offset
        y = spawn_point.y + direction * left_y * offset
        z = spawn_point.z if spawn_point.z > 0 else _FALLBACK_Z_M
        return carla_module.Location(x=x, y=y, z=z)

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
        """登记脚本化行人（AI 不可用时每个 tick 直行推向目的地）。"""
        target = (
            float(getattr(destination, "x", spawn_point.x)),
            float(getattr(destination, "y", spawn_point.y)),
            float(getattr(destination, "z", spawn_point.z)),
        )
        self._scripted_walkers.append(
            _ScriptedWalker(
                actor=actor,
                destination=target,
                speed_ms=max(0.1, speed_ms),
                carla_module=carla_module,
            )
        )
        logger.info(
            "Walker '%s' scripted crossing: speed=%.2f m/s from=(%.1f,%.1f) "
            "to=(%.1f,%.1f) distance_to_ego=%s",
            participant.participant_id,
            speed_ms,
            spawn_point.x,
            spawn_point.y,
            target[0],
            target[1],
            _format_distance(self._distance_to_ego(spawn_point)),
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
                dx = walker.destination[0] - float(location.x)
                dy = walker.destination[1] - float(location.y)
                distance = math.hypot(dx, dy)
                if distance <= _WALKER_ARRIVE_EPS_M:
                    walker.done = True
                    continue
                step = min(distance, walker.speed_ms * step_budget)
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
            except Exception as exc:  # noqa: BLE001  单帧推进失败不阻断场景
                logger.debug(f"Scripted walker update failed: {exc}")
                walker.done = True

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
            "(native encode, jpeg>=%d, no downscale)",
            _OVERVIEW_MOUNT_ID,
            camera_config.fov_deg,
            camera_config.width,
            camera_config.height,
            _OVERVIEW_JPEG_QUALITY,
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
                self._warn_overview_slow(time.perf_counter() - started)

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
        """实际分发逻辑（拆分便于单测直接调用）。"""
        ts = float(getattr(data, "timestamp", 0.0)) or time.time()
        log_time_ns = int(ts * 1e9)
        frame_id = _FRAME_IDS.get(stream, stream)

        if stream == STREAM_OVERVIEW:
            # 仅可视化：推 FrameHub 供主视区取帧，不录 mcap、不发 ROS。
            # 主视区必须看到 CARLA 原生画面：不再套用数据流的 stream_max_width
            # 降采样（默认 640 会把 1920x1080 压成 640 宽，主视区明显模糊），
            # JPEG 质量下限提到 _OVERVIEW_JPEG_QUALITY。
            raw, width, height = self._raw_image(data)
            encoded = encode_rgb_frame(
                raw,
                width,
                height,
                quality=max(self._screenshot_quality, _OVERVIEW_JPEG_QUALITY),
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
                    },
                )
            if self._recorder is not None:
                # 录制保留原始 BGRA（无损，便于离线重编码）
                self._recorder.push_stream(
                    stream,
                    log_time_ns,
                    build_image_message(raw, width, height, "bgra8", frame_id, log_time_ns),
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
                        },
                    )
                if self._recorder is not None:
                    self._recorder.push_stream(
                        stream,
                        log_time_ns,
                        build_image_message(meters, width, height, "32FC1", frame_id, log_time_ns),
                    )
            self._publish_ros("depth", data)
            return

        if stream == "lidar":
            payload, points = self._lidar_xyz(points_bytes=_measurement_bytes(data))
            if self._frame_hub is not None:
                self._frame_hub.push(stream, ts, payload, {"points": points})
            if self._recorder is not None and payload:
                self._recorder.push_stream(
                    stream,
                    log_time_ns,
                    build_pointcloud2_message(payload, points, frame_id, log_time_ns),
                )
            self._publish_ros("lidar", data)
            return

        if stream == "imu":
            accel = _vec3(getattr(data, "accelerometer", None))
            gyro = _vec3(getattr(data, "gyroscope", None))
            if self._frame_hub is not None:
                self._frame_hub.push(stream, ts, repr((accel, gyro)).encode("utf-8"), {})
            if self._recorder is not None:
                self._recorder.push_stream(
                    stream,
                    log_time_ns,
                    build_imu_message(accel, gyro, frame_id, log_time_ns),
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
        """读取自车实际 transform 作为参与者偏移基准（取不到时用候选点）。"""
        getter = getattr(actor, "get_transform", None)
        if callable(getter):
            try:
                return transform_to_spawn_point(getter())
            except Exception as exc:  # noqa: BLE001  读取失败退回候选点，不阻断场景
                logger.debug(f"ego_transform_unavailable: {exc}")
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
