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

import time
from typing import Any, Callable, Optional

from hunter_sim.common.exceptions import CarlaSimulationError, SensorSimulationError
from hunter_sim.common.models import SensorType
from hunter_sim.common.utils import get_logger
from hunter_sim.scene_runner.custom_scenario import CustomScenarioBase
from hunter_sim.scene_runner.event_detector import EventDetector
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
    SceneConfig,
    SceneEventType,
    SensorSpec,
    SpawnPoint,
    TrafficParticipantConfig,
)
from hunter_sim.scene_runner.scene_state_manager import SceneStateManager
from hunter_sim.sensor_sim.frame_hub import FrameHub, STREAM_RGB
from hunter_sim.sensor_sim.image_codec import depth_bgra_to_meters, encode_rgb_frame
from hunter_sim.sensor_sim.mcap_recorder import (
    McapRecorder,
    build_image_message,
    build_imu_message,
    build_pointcloud2_message,
)
from hunter_sim.sensor_sim.profiles import resolve_profiles
from hunter_sim.sensor_sim.sensor_factory import SensorBlueprintFactory
from hunter_sim.sensor_sim.sensor_mount import SensorMountManager

logger = get_logger(__name__)

# 生成位姿工厂签名：(world, spawn_point) -> carla.Transform
TransformFactory = Callable[[Any, SpawnPoint], Any]

# 自车/参与者默认离地高度（米）：spawn z<=0 时按地面高度 + 该值放置
_DEFAULT_CLEARANCE_M = 0.3
# 无法查询地面时的兜底高度（米）
_FALLBACK_Z_M = 0.5

_AUTOPILOT_TM_PORT = 8000

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
    ) -> None:
        super().__init__(config, state_manager, event_detector)
        self._transform_factory = transform_factory
        self._frame_hub = frame_hub
        self._recorder = recorder
        self._ros_bridge = ros_bridge
        self._screenshot_quality = screenshot_quality
        self._stream_max_width = stream_max_width
        # 由 config.sensors 在挂载时确定（任一 spec 开启 ros 即发布）
        self._ros_enabled = False
        self._ego: Any = None
        self._actors: list[Any] = []
        self._sensors: list[Any] = []
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

        library = self._blueprint_library(world)
        self._ego = self._spawn_ego(world, library)
        for participant in self._config.traffic_participants:
            self._spawn_participant(world, library, participant)
        self._attach_event_sensors(world, library)
        self._attach_sim_sensors(world, library)

        logger.info(
            f"Builtin scenario '{self._config.scene_id}' ready: "
            f"ego={getattr(self._ego, 'id', 'n/a')}, actors={len(self._actors)}, "
            f"sensors={len(self._sensors)}"
        )

    def tick(self, world: Any, timestamp: float, delta_seconds: float) -> None:
        """每帧回调：P0 阶段行为由 Traffic Manager 接管，此处无需额外推进。"""

    def check_finish(self, world: Any, timestamp: float) -> bool:
        """运行时长达到 ``duration_seconds`` 时结束场景。"""
        return self.elapsed_seconds >= self._config.duration_seconds

    def cleanup(self, world: Any) -> None:
        """销毁本场景生成的全部 Actor 与传感器（幂等）。"""
        if self._cleaned:
            return
        self._cleaned = True

        for sensor in self._sensors:
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

        for actor in ([self._ego] if self._ego is not None else []) + self._actors:
            try:
                actor.destroy()
            except Exception as exc:  # noqa: BLE001  Actor 可能已被外部销毁
                logger.debug(f"Actor destroy failed: {exc}")
        self._ego = None
        self._actors.clear()

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
            logger.warning(
                f"Ego blueprint '{ego_cfg.vehicle_blueprint}' unavailable, "
                f"fallback to '{getattr(blueprint, 'id', blueprint)}': {exc}"
            )
        transform = self._make_transform(world, ego_cfg.spawn_point)
        actor = self._spawn_actor(world, blueprint, transform)
        if actor is None:
            raise CarlaSimulationError(
                "builtin_scenario.spawn_ego",
                (
                    f"自车生成失败（位置被占用或非法）：blueprint={ego_cfg.vehicle_blueprint}, "
                    f"spawn=({ego_cfg.spawn_point.x:.1f}, {ego_cfg.spawn_point.y:.1f})"
                ),
            )
        if ego_cfg.autopilot:
            set_autopilot = getattr(actor, "set_autopilot", None)
            if callable(set_autopilot):
                try:
                    set_autopilot(True, _AUTOPILOT_TM_PORT)
                    logger.info("Ego autopilot enabled (Traffic Manager)")
                except Exception as exc:  # noqa: BLE001  TM 未就绪时降级为手动控制
                    logger.warning(f"Ego autopilot enable failed: {exc}")
        return actor

    def _spawn_participant(
        self, world: Any, library: Any, participant: TrafficParticipantConfig
    ) -> None:
        prefix = "walker." if participant.actor_type in ("walker", "cyclist") else "vehicle."
        try:
            blueprint = self._find_blueprint(library, participant.blueprint, prefix)
            transform = self._make_transform(world, participant.spawn_point)
            actor = self._spawn_actor(world, blueprint, transform)
        except CarlaSimulationError as exc:
            # 单个参与者失败不阻断整场景（平台可能下发未安装的蓝图）
            logger.warning(f"Participant '{participant.participant_id}' spawn skipped: {exc}")
            return
        if actor is None:
            logger.warning(f"Participant '{participant.participant_id}' spawn failed (位置被占用)")
            return
        self._actors.append(actor)

        if participant.actor_type == "vehicle" and participant.behavior.value != "static":
            set_autopilot = getattr(actor, "set_autopilot", None)
            if callable(set_autopilot):
                try:
                    set_autopilot(True, _AUTOPILOT_TM_PORT)
                except Exception as exc:  # noqa: BLE001  TM 未就绪时保持静止
                    logger.warning(
                        f"Participant '{participant.participant_id}' autopilot failed: {exc}"
                    )

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

        Args:
            world: CARLA World。
            library: BlueprintLibrary。
        """
        specs: list[SensorSpec] = [spec for spec in self._config.sensors if spec.enabled]
        if not specs or self._ego is None:
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

        mount_manager = SensorMountManager(self._ego)
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

    def _listen_sensor(self, sensor: Any, stream: str) -> None:
        """注册传感器回调（回调异常不得冒泡到 CARLA 线程）。"""
        listen = getattr(sensor, "listen", None)
        if not callable(listen):
            return
        listen(lambda data, _stream=stream: self._on_sensor_data(_stream, data))

    def _on_sensor_data(self, stream: str, data: Any) -> None:
        """传感器帧统一出口：编码 → FrameHub → McapRecorder → ROS2。"""
        try:
            self._dispatch_sensor_data(stream, data)
        except Exception as exc:  # noqa: BLE001  单帧异常不得中断仿真
            logger.debug(f"Sensor frame dispatch skipped ({stream}): {exc}")

    def _dispatch_sensor_data(self, stream: str, data: Any) -> None:
        """实际分发逻辑（拆分便于单测直接调用）。"""
        ts = float(getattr(data, "timestamp", 0.0)) or time.time()
        log_time_ns = int(ts * 1e9)
        frame_id = _FRAME_IDS.get(stream, stream)

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

        if stream == "depth":
            raw, width, height = self._raw_image(data)
            if self._recorder is not None:
                meters = depth_bgra_to_meters(raw, width, height).tobytes()
                self._recorder.push_stream(
                    stream,
                    log_time_ns,
                    build_image_message(meters, width, height, "32FC1", frame_id, log_time_ns),
                )
            self._publish_ros("depth", data)
            return

        if stream == "lidar":
            payload, points = self._lidar_xyz(points_bytes=bytes(getattr(data, "data", b"")))
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
            accel = tuple(float(v) for v in getattr(data, "accelerometer", (0.0, 0.0, 0.0)))
            gyro = tuple(float(v) for v in getattr(data, "gyroscope", (0.0, 0.0, 0.0)))
            if self._frame_hub is not None:
                self._frame_hub.push(stream, ts, repr((accel, gyro)).encode("utf-8"), {})
            if self._recorder is not None:
                self._recorder.push_stream(
                    stream,
                    log_time_ns,
                    build_imu_message(accel, gyro, frame_id, log_time_ns),  # type: ignore[arg-type]
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
        raw = bytes(getattr(data, "data", b"") or b"")
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


def _safe_find(library: Any, blueprint_id: str) -> Any:
    """蓝图查找：CARLA 对不存在的 ID 抛 IndexError，这里统一降级为 None。"""
    try:
        return library.find(blueprint_id)
    except Exception:  # noqa: BLE001  缺失蓝图交由候选/回退逻辑处理
        return None


def build_default_scenario(
    config: SceneConfig,
    state_manager: SceneStateManager,
    event_detector: EventDetector,
    transform_factory: Optional[TransformFactory] = None,
) -> BuiltinScenario:
    """构造内置场景执行器（供 SceneRunnerService 默认使用）。"""
    return BuiltinScenario(config, state_manager, event_detector, transform_factory)


__all__ = ["BuiltinScenario", "TransformFactory", "build_default_scenario"]
