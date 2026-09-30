"""VIL 状态同步服务主类（PROMPT-ENG-002-C）。

实现同步循环：接收数据 → 坐标转换 → 延迟补偿 → 设置位姿 → world.tick()。
提供 VILSyncService 类，协调数据消费者、坐标转换器、位姿外推器和车辆控制器。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from hunter_sim.common.exceptions import CarlaSimulationError, SimTimeoutError
from hunter_sim.common.models import Transform, VehicleState, VILSettings
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.coordinate_converter import CoordinateTransformer
from hunter_sim.vil_mapper.data_health_monitor import DataHealthMonitor, DataHealthStatus
from hunter_sim.vil_mapper.pose_extrapolator import PoseExtrapolator
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame

logger = get_logger(__name__)


class VehicleActorProtocol(Protocol):
    """CARLA Vehicle Actor 协议（供类型检查）。"""

    def set_transform(self, transform: Any) -> None: ...

    def set_velocity(self, vector: Any) -> None: ...

    def set_angular_velocity(self, vector: Any) -> None: ...


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
    1. 等待最新遥测帧（超时则使用外推）
    2. 坐标转换（odom → CARLA）
    3. 位姿外推（延迟补偿）
    4. 设置车辆位姿（vehicle.set_transform）
    5. world.tick()

    Args:
        settings: VIL 参数配置。
        buffer: 遥测数据缓冲区。
        coord_transformer: 坐标转换器。
        extrapolator: 位姿外推器。
        health_monitor: 数据健康监控器。
        vehicle_actor: CARLA Vehicle Actor（VIL 模式）。
        world: CARLA World 对象。
        ground_z_getter: 可选的路面高度获取函数 (x, y) -> z。
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
    ) -> None:
        self._settings = settings
        self._buffer = buffer
        self._coord = coord_transformer
        self._extrap = extrapolator
        self._health = health_monitor
        self._vehicle = vehicle_actor
        self._world = world
        self._ground_z = ground_z_getter

        self._running = threading.Event()
        self._lock = threading.Lock()
        self._stats = VILSyncStats()
        self._tick_times: list[float] = []
        self._latency_samples: list[float] = []
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
            self._stats.total_ticks += 1

            try:
                self._do_sync_cycle()
                self._stats.total_syncs += 1
            except SimTimeoutError as exc:
                logger.warning(f"Sync timeout: {exc}")
                self._stats.sync_errors += 1
            except CarlaSimulationError as exc:
                logger.error(f"CARLA sync error: {exc}")
                self._stats.sync_errors += 1
            except Exception as exc:
                logger.error(f"Unexpected sync error: {exc}", exc_info=True)
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
            self._tick_times.append(time.perf_counter())
            if len(self._tick_times) > 100:
                self._tick_times.pop(0)

        logger.info(f"VIL sync loop stopped (ticks={self._stats.total_ticks})")

    def stop(self) -> None:
        """停止同步循环。"""
        self._running.clear()
        logger.info("VIL sync loop stop requested")

    def _do_sync_cycle(self) -> None:
        """执行一次完整的同步循环步骤。"""
        # 1. 健康检查
        report = self._health.check()
        if report.status == DataHealthStatus.INTERRUPTED:
            raise SimTimeoutError(
                "vil_data_wait",
                self._settings.max_data_latency_ms / 1000.0,
                module="vil_mapper",
            )

        # 2. 获取最新帧
        frame: Optional[TelemetryFrame] = self._buffer.get_latest()
        if frame is None:
            logger.debug("No telemetry frame available, skipping sync")
            return

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

        # 5. 位姿外推（延迟补偿，DEGRADED 状态时强制外推）
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

        # 6. 设置 CARLA 车辆位姿与速度（VIL 模式，直接设置，设计文档 §4.4.1）
        self._apply_carla_transform(carla_transform)
        self._apply_carla_velocity(frame.vehicle_state, carla_transform.yaw)

        latency_ms = (time.perf_counter() - t_start) * 1000.0
        self._latency_samples.append(latency_ms)
        if len(self._latency_samples) > 100:
            self._latency_samples.pop(0)

    def _apply_carla_transform(self, transform: Transform) -> None:
        """将 Transform 应用到 CARLA Vehicle Actor。"""
        import carla  # noqa: PLC0415
        import math
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
        import carla  # noqa: PLC0415
        import math
        try:
            v = state.vehicle_speed
            self._vehicle.set_velocity(carla.Vector3D(
                x=v * math.cos(yaw_map),
                y=v * math.sin(yaw_map),
                z=0.0,
            ))
            self._vehicle.set_angular_velocity(carla.Vector3D(
                x=state.angular_velocity[0],
                y=state.angular_velocity[1],
                z=state.angular_velocity[2],
            ))
        except Exception as exc:
            logger.debug(f"apply velocity skipped: {exc}")

    def _estimate_fps(self) -> float:
        """估算当前 tick FPS。"""
        if len(self._tick_times) < 2:
            return 0.0
        span = self._tick_times[-1] - self._tick_times[0]
        return len(self._tick_times) / span if span > 0 else 0.0
