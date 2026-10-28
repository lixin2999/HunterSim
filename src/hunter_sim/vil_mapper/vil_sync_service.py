"""VIL 状态同步服务主类（PROMPT-ENG-002-C，V3.0.0 栅格对齐版）。

实现同步循环：取帧 → 栅格对齐 → 坐标转换 → 延迟补偿 → 设置位姿/速度/转向/灯光
→ 感知叠加。提供 VILSyncService 类，协调数据消费者、帧对齐器、坐标转换器、
位姿外推器、状态可视化器与车辆控制器。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from hunter_sim.common.exceptions import CarlaSimulationError, SimTimeoutError
from hunter_sim.common.models import Transform, VehicleState, VILSettings
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.coordinate_converter import CoordinateTransformer
from hunter_sim.vil_mapper.data_health_monitor import DataHealthMonitor, DataHealthStatus
from hunter_sim.vil_mapper.frame_aligner import AlignedFrame, FrameAligner
from hunter_sim.vil_mapper.pose_extrapolator import PoseExtrapolator
from hunter_sim.vil_mapper.state_visualizer import PerceptionOverlay, StateVisualizer
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer

logger = get_logger(__name__)

# 断流减速斜率（m/s²）：3.0 接近常规舒适制动，避免瞬时停车
_DECELERATION_MS2 = 3.0

# 感知叠加绘制节流：每 N 个栅格画一次（10Hz 栅格下约 3.3Hz，降低 debug 绘制开销）
_OVERLAY_GRID_INTERVAL = 3


class VehicleActorProtocol(Protocol):
    """CARLA Vehicle Actor 协议（供类型检查）。"""

    def set_transform(self, transform: Any) -> None: ...

    def set_velocity(self, vector: Any) -> None: ...

    def set_angular_velocity(self, vector: Any) -> None: ...

    def set_wheel_steer_angle(self, wheel_location: Any, steer_angle: float) -> None: ...

    def set_light_state(self, light_state: Any) -> None: ...

    def get_velocity(self) -> Any: ...

    def get_transform(self) -> Any: ...


class WorldProtocol(Protocol):
    """CARLA World 协议（供类型检查）。"""

    def tick(self) -> int: ...


@dataclass
class VILSyncStats:
    """VIL 同步状态统计。

    Attributes:
        total_ticks: 总 tick 次数。
        total_syncs: 成功同步次数。
        sync_errors: 同步失败次数。
        avg_latency_ms: 平均同步延迟（毫秒）。
        current_fps: 当前 tick 频率（Hz）。
        health_status: 最新数据健康状态。
    """

    total_ticks: int = 0
    total_syncs: int = 0
    sync_errors: int = 0
    avg_latency_ms: float = 0.0
    current_fps: float = 0.0
    health_status: DataHealthStatus = DataHealthStatus.OK


class VILSyncService:
    """VIL 主同步服务（同步模式 50Hz）。

    封装完整的同步循环，应在 CARLA 同步模式（synchronous_mode=True）下运行。
    调用方需在单独线程中运行 start_sync_loop()，并在停止时调用 stop()。

    同步流程：
    1. 栅格对齐（10Hz 降采样 + 插值/外推，``FrameAligner``）
    2. 坐标转换（odom → CARLA）
    3. 位姿外推（延迟补偿）
    4. 设置车辆位姿/速度/舵角/灯光（vehicle.set_transform 等）
    5. 感知叠加绘制（每 3 个栅格一次）
    6. world.tick()

    Args:
        settings: VIL 参数配置。
        buffer: 遥测数据缓冲区。
        coord_transformer: 坐标转换器。
        extrapolator: 位姿外推器。
        health_monitor: 数据健康监控器。
        vehicle_actor: CARLA Vehicle Actor（VIL 模式）。
        world: CARLA World 对象。
        ground_z_getter: 可选的路面高度获取函数 (x, y) -> z。
        aligner: 帧对齐器；None 时按 ``settings`` 自建（V3.0.0）。
        overlay: 感知叠加渲染器；None 表示不绘制感知框。
        visualizer: 状态可视化器（灯光同步）；None 时灯光跳过。
    """

    def __init__(
        self,
        settings: VILSettings,
        buffer: TelemetryBuffer,
        coord_transformer: CoordinateTransformer,
        extrapolator: PoseExtrapolator,
        health_monitor: DataHealthMonitor,
        vehicle_actor: VehicleActorProtocol,
        world: WorldProtocol,
        ground_z_getter: Optional[Any] = None,
        aligner: Optional[FrameAligner] = None,
        overlay: Optional[PerceptionOverlay] = None,
        visualizer: Optional[StateVisualizer] = None,
    ) -> None:
        self._settings = settings
        self._buffer = buffer
        self._coord = coord_transformer
        self._extrap = extrapolator
        self._health = health_monitor
        self._vehicle = vehicle_actor
        self._world = world
        self._ground_z = ground_z_getter
        self._aligner = aligner or FrameAligner(
            buffer,
            target_hz=settings.target_hz,
            max_extrapolation_ms=settings.extrapolation_ms,
        )
        self._overlay = overlay
        self._visualizer = visualizer

        self._running = threading.Event()
        self._lock = threading.Lock()
        self._stats = VILSyncStats()
        self._tick_times: list[float] = []
        self._latency_samples: list[float] = []
        # 断流告警限频：50Hz tick 下逐帧 warn 会刷屏（每 5s 最多一条）
        self._last_timeout_log: float = 0.0
        # 断流减速步长计算（外部 tick 调用 apply_once 时使用）
        self._last_step_time: float = 0.0
        logger.info("VILSyncService initialized")

    @property
    def stats(self) -> VILSyncStats:
        """当前同步统计（只读副本）。"""
        with self._lock:
            return VILSyncStats(
                total_ticks=self._stats.total_ticks,
                total_syncs=self._stats.total_syncs,
                sync_errors=self._stats.sync_errors,
                avg_latency_ms=(
                    sum(self._latency_samples) / len(self._latency_samples)
                    if self._latency_samples
                    else 0.0
                ),
                current_fps=self._estimate_fps(),
                health_status=self._health.current_status,
            )

    def start_sync_loop(self) -> None:
        """启动同步主循环（阻塞调用，应在独立线程中执行）。"""
        self._running.set()
        logger.info("VIL sync loop started (50Hz target)")
        delta_seconds = 0.02  # 50Hz

        while self._running.is_set():
            t0 = time.perf_counter()
            with self._lock:
                self._stats.total_ticks += 1

            try:
                self._do_sync_cycle()
                with self._lock:
                    self._stats.total_syncs += 1
            except SimTimeoutError as exc:
                # 数据中断：限制日志频率，避免 50Hz 刷屏
                now = time.perf_counter()
                with self._lock:
                    self._stats.sync_errors += 1
                    should_log = now - self._last_timeout_log >= 5.0
                    if should_log:
                        self._last_timeout_log = now
                if should_log:
                    logger.warning(f"Sync timeout: {exc}")
            except CarlaSimulationError as exc:
                logger.error(f"CARLA sync error: {exc}")
                with self._lock:
                    self._stats.sync_errors += 1
            except Exception as exc:
                logger.error(f"Unexpected sync error: {exc}", exc_info=True)
                with self._lock:
                    self._stats.sync_errors += 1

            # 执行 tick
            try:
                self._world.tick()
            except Exception as exc:
                logger.error(f"world.tick() failed: {exc}")

            # 控制帧率
            elapsed = time.perf_counter() - t0
            sleep_time = max(0.0, delta_seconds - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

            # FPS 统计
            with self._lock:
                self._tick_times.append(time.perf_counter())
                if len(self._tick_times) > 100:
                    self._tick_times.pop(0)

        with self._lock:
            total_ticks = self._stats.total_ticks
        logger.info(f"VIL sync loop stopped (ticks={total_ticks})")

    def stop(self) -> None:
        """停止同步循环。"""
        self._running.clear()
        logger.info("VIL sync loop stop requested")

    def _do_sync_cycle(self) -> None:
        """执行一次完整的同步循环步骤（兼容旧入口，中断时抛 SimTimeoutError）。"""
        outcome = self.apply_once()
        if outcome == "interrupted":
            raise SimTimeoutError(
                "vil_data_wait",
                self._settings.max_data_latency_ms / 1000.0,
                module="vil_mapper",
            )

    def apply_once(self) -> str:
        """在外部 tick 循环中应用一次实车位姿（**不**调用 ``world.tick()``）。

        适配器把本方法挂到 ``SceneRunnerService`` 的每 tick 钩子上，避免出现
        第二个 ``world.tick()`` 导致帧同步错乱。

        V3.0.0：取帧改为 ``FrameAligner.next()``，同一 10Hz 栅格内的后续 tick
        返回 ``"skipped"``（不重复写位姿，实现降采样）；只有栅格推进的 tick
        才真正改车辆状态。

        数据中断（超过 ``max_data_latency_ms``）或尚无首帧时执行减速/停车，
        不保持最后一帧位姿（方案要求"减速/停车"而非"冻结"）。

        Returns:
            ``"applied"`` / ``"skipped"`` / ``"no_data"`` / ``"interrupted"``。
        """
        # 1. 健康检查
        report = self._health.check()
        if report.status == DataHealthStatus.INTERRUPTED:
            self._decelerate()
            return "interrupted"

        # 2. 栅格对齐（10Hz 降采样 + 插值/外推）
        aligned: Optional[AlignedFrame] = self._aligner.next(time.time())
        if aligned is None:
            if self._buffer.is_empty:
                self._decelerate()
                return "no_data"
            # 本 tick 未推进到新栅格（降采样），保持现有位姿不动
            return "skipped"
        frame = aligned.frame

        t_start = time.perf_counter()

        # 3. 坐标转换（odom → CARLA）
        x_c, y_c, z_c, pitch_c, yaw_c, roll_c = self._coord.odom_to_carla(
            x_odom=frame.vehicle_state.transform.x,
            y_odom=frame.vehicle_state.transform.y,
            yaw_odom=frame.vehicle_state.transform.yaw,
            pitch_odom=frame.vehicle_state.transform.pitch,
            roll_odom=frame.vehicle_state.transform.roll,
        )

        # 4. 获取地图路面高度（实车 2D 定位，Z 由地图决定）
        if self._ground_z is not None:
            z_c = self._ground_z(x_c, y_c)

        # 5. 位姿外推（延迟补偿，DEGRADED 状态或非插值帧时强制外推）
        carla_transform = Transform(
            x=x_c, y=y_c, z=z_c,
            pitch=pitch_c, yaw=yaw_c, roll=roll_c,
        )
        if report.status == DataHealthStatus.DEGRADED or not frame.is_fresh:
            speed = frame.vehicle_state.vehicle_speed
            yaw_rate = frame.vehicle_state.angular_velocity[2]
            carla_transform = self._extrap.extrapolate_transform(
                carla_transform, speed, yaw_rate
            )

        # 6. 设置 CARLA 车辆位姿/速度/舵角/灯光（VIL 模式，设计文档 §4.4.1）
        self._apply_carla_transform(carla_transform)
        self._apply_carla_velocity(frame.vehicle_state, carla_transform.yaw)
        self._apply_steering(frame.vehicle_state)
        self._apply_lights(frame.vehicle_state)

        # 7. 感知叠加（节流绘制，异常不影响 tick）
        self._draw_overlay(aligned)

        latency_ms = (time.perf_counter() - t_start) * 1000.0
        with self._lock:
            self._latency_samples.append(latency_ms)
            if len(self._latency_samples) > 100:
                self._latency_samples.pop(0)
        return "applied"

    def _apply_steering(self, state: VehicleState) -> None:
        """同步实车方向盘角度到虚拟车前轮转角（设计文档 §4.4.1）。

        内部统一弧度制（``chassis.steering_angle``），CARLA
        ``set_wheel_steer_angle`` 边界为度，因此此处换算。
        无 carla SDK / 后端不支持时降级 debug，不中断同步循环。

        Args:
            state: 实车状态帧。
        """
        try:
            import carla  # noqa: PLC0415

            degrees = math.degrees(state.steering)
            self._vehicle.set_wheel_steer_angle(carla.WheelLocation.FRONT_LEFT, degrees)
            self._vehicle.set_wheel_steer_angle(carla.WheelLocation.FRONT_RIGHT, degrees)
        except Exception as exc:  # noqa: BLE001  含无 carla 环境的 ImportError
            logger.debug(f"apply steering skipped: {exc}")

    def _apply_lights(self, state: VehicleState) -> None:
        """同步实车灯光（刹车灯/转向灯/近光）到虚拟车（设计文档 §4.4.2）。

        ``state.lights`` 为空（实车未上报）时不做任何变更，避免把已开灯光意外关掉。

        Args:
            state: 实车状态帧。
        """
        if self._visualizer is None or not self._settings.lights_enabled:
            return
        lights = state.lights
        if not lights:
            return
        self._visualizer.apply_vehicle_lights(
            self._vehicle,
            brake=state.brake,
            left_turn=bool(lights.get("left_turn", False)),
            right_turn=bool(lights.get("right_turn", False)),
            low_beam=bool(lights.get("low_beam", False)),
        )

    def _draw_overlay(self, aligned: AlignedFrame) -> None:
        """绘制实车感知叠加框（每 ``_OVERLAY_GRID_INTERVAL`` 个栅格一次）。

        Args:
            aligned: 本栅格的对齐帧。
        """
        if self._overlay is None or not self._settings.overlay_enabled:
            return
        if aligned.grid_index % _OVERLAY_GRID_INTERVAL != 0:
            return
        perception = aligned.frame.perception
        if perception is None:
            return
        try:
            self._overlay.draw_perception_result(perception)
        except Exception as exc:  # noqa: BLE001  绘制失败不得影响同步 tick
            logger.debug(f"perception overlay skipped: {exc}")

    def _decelerate(self) -> None:
        """断流时按固定减速度逐步减速至停车（不保持最后位姿）。"""
        try:
            velocity = self._vehicle.get_velocity()
            transform = self._vehicle.get_transform()
        except Exception:  # noqa: BLE001  桩/受限 Actor 不支持读取时跳过
            return
        try:
            speed = math.sqrt(velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2)
        except Exception:  # noqa: BLE001
            return
        now = time.perf_counter()
        dt = min(0.5, max(0.0, now - self._last_step_time)) if self._last_step_time else 0.02
        self._last_step_time = now
        new_speed = max(0.0, speed - _DECELERATION_MS2 * dt)
        if speed <= 0.05:
            new_speed = 0.0
        try:
            yaw = math.radians(getattr(getattr(transform, "rotation", None), "yaw", 0.0))
        except Exception:  # noqa: BLE001
            yaw = 0.0
        try:
            import carla  # noqa: PLC0415  无 SDK 时保持静默降级

            self._vehicle.set_velocity(
                carla.Vector3D(
                    x=new_speed * math.cos(yaw),
                    y=new_speed * math.sin(yaw),
                    z=0.0,
                )
            )
            self._vehicle.set_angular_velocity(carla.Vector3D(x=0.0, y=0.0, z=0.0))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"decelerate skipped: {exc}")

    def _apply_carla_transform(self, transform: Transform) -> None:
        """将 Transform 应用到 CARLA Vehicle Actor。

        carla 模块惰性导入降级：无 carla 环境时记日志跳过，不每 tick 抛
        ModuleNotFoundError 累加 sync_errors（可选依赖惰性导入与降级规范）。
        """
        try:
            import carla  # noqa: PLC0415
        except ImportError:
            logger.debug("carla not available, transform application skipped (degraded mode)")
            return
        carla_tf = carla.Transform(
            carla.Location(x=transform.x, y=transform.y, z=transform.z),
            carla.Rotation(
                pitch=math.degrees(transform.pitch),
                yaw=math.degrees(transform.yaw),
                roll=math.degrees(transform.roll),
            ),
        )
        self._vehicle.set_transform(carla_tf)

    def _apply_carla_velocity(self, state: VehicleState, yaw_map: float) -> None:
        """同步车辆速度/角速度（设计文档 §4.4.1/§3.3.2）。

        vx = v * cos(yaw_map)，vy = v * sin(yaw_map)，vz = 0。
        失败仅记日志，不中断同步循环（部分 Mock/后端不支持速度设置）。

        Args:
            state: 实车状态帧。
            yaw_map: 换算后的 CARLA 地图航向角（弧度）。
        """
        try:
            import carla  # noqa: PLC0415
            v = state.vehicle_speed
            self._vehicle.set_velocity(carla.Vector3D(
                x=v * math.cos(yaw_map),
                y=v * math.sin(yaw_map),
                z=0.0,
            ))
            # 内部 rad/s → CARLA 边界 deg/s（set_angular_velocity 期望 deg/s）
            self._vehicle.set_angular_velocity(carla.Vector3D(
                x=math.degrees(state.angular_velocity[0]),
                y=math.degrees(state.angular_velocity[1]),
                z=math.degrees(state.angular_velocity[2]),
            ))
        except Exception as exc:  # 含无 carla 环境的 ImportError，降级不中断同步循环
            logger.debug(f"apply velocity skipped: {exc}")

    def _estimate_fps(self) -> float:
        """估算当前 tick FPS。"""
        if len(self._tick_times) < 2:
            return 0.0
        span = self._tick_times[-1] - self._tick_times[0]
        return len(self._tick_times) / span if span > 0 else 0.0
