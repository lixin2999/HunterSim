"""VIL 运行时（适配器版）。

与 ``VILSyncService`` 的区别：**不自己 tick 世界**。适配器把 ``step()`` 挂到
``SceneRunnerService`` 的唯一 tick 循环上，只在该 tick 之前应用实车位姿，
避免双线程 ``world.tick()`` 造成帧同步错乱。

数据面沿用现有组件：

- ``VILDataConsumer``：消费 ``carla.{sim_id}.vil_state``（消费组 ``carla-vil``）；
- ``TelemetryBuffer``：latest-wins；
- ``CoordinateTransformer``：odom → map 标定；
- ``PoseExtrapolator``：``extrapolation_ms`` 内延迟补偿；
- ``DataHealthMonitor``：超时判定 → 减速/停车（不冻结位姿）。
"""

from __future__ import annotations

import math
import threading
from typing import Any, Callable, Optional

from hunter_sim.common.models import HunterSimSettings, VILSettings
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.coordinate_converter import CalibrationParams, CoordinateTransformer
from hunter_sim.platform_adapter.scene_mapper import map_calibration
from hunter_sim.vil_mapper.data_health_monitor import DataHealthMonitor
from hunter_sim.vil_mapper.pose_extrapolator import PoseExtrapolator
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer
from hunter_sim.vil_mapper.vil_data_consumer import VILDataConsumer
from hunter_sim.vil_mapper.vil_sync_service import VILSyncService

logger = get_logger(__name__)


class VILRuntime:
    """单实例 VIL 运行时。

    Args:
        settings: 全局配置（Kafka/VIL）。
        sim_id: 当前仿真实例 ID（渲染 ``carla.{sim_id}.vil_state``）。
        vehicle_id: 孪生车 ID。
        vil_config: 平台下发的 VIL 配置（含 calibration/target_hz/data_timeout_ms）。
        runner_provider: 返回当前 ``SceneRunnerService`` 的回调（延迟绑定自车）。
    """

    def __init__(
        self,
        settings: HunterSimSettings,
        sim_id: str,
        vehicle_id: str,
        vil_config: Optional[dict[str, Any]],
        runner_provider: Callable[[], Any],
    ) -> None:
        self._settings = settings
        self._sim_id = sim_id
        self._vehicle_id = vehicle_id
        self._vil_config = vil_config or {}
        self._runner_provider = runner_provider

        overrides: dict[str, Any] = {}
        extrapolation_ms = _optional_int(self._vil_config.get("extrapolation_ms"))
        if extrapolation_ms is not None:
            overrides["extrapolation_ms"] = max(0, min(1000, extrapolation_ms))
        timeout_ms = _optional_int(self._vil_config.get("data_timeout_ms"))
        if timeout_ms is not None:
            overrides["max_data_latency_ms"] = max(100, min(5000, timeout_ms))
        self._vil_settings: VILSettings = (
            settings.vil.model_copy(update=overrides) if overrides else settings.vil
        )

        calibration = map_calibration(self._vil_config) or CalibrationParams(
            x0=settings.vil.calibration_x0,
            y0=settings.vil.calibration_y0,
            yaw0=settings.vil.calibration_yaw0,
            z_source="map",
        )
        self._calibration = calibration
        self._buffer = TelemetryBuffer(vil_settings=self._vil_settings)
        self._transformer = CoordinateTransformer(calibration)
        self._extrapolator = PoseExtrapolator(self._vil_settings.extrapolation_ms)
        self._health = DataHealthMonitor(self._vil_settings, self._buffer)
        self._consumer: Optional[VILDataConsumer] = None
        self._service: Optional[VILSyncService] = None
        self._bound_vehicle: Any = None
        self._interrupted = False
        self._lock = threading.RLock()
        self._steps = 0
        self._applied = 0

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """启动 Kafka 消费线程（无 broker 时内部降级，不影响仿真）。"""
        with self._lock:
            if self._consumer is not None:
                return
            self._consumer = VILDataConsumer(
                self._settings.kafka,
                self._vil_settings,
                self._vehicle_id,
                self._buffer,
                sim_id=self._sim_id,
            )
            self._consumer.start()
        logger.info(f"VIL runtime started: sim_id={self._sim_id}, vehicle_id={self._vehicle_id}")

    def stop(self, timeout_s: float = 3.0) -> None:
        """停止消费线程并释放绑定。"""
        with self._lock:
            consumer = self._consumer
            self._consumer = None
            self._service = None
            self._bound_vehicle = None
        if consumer is not None:
            try:
                consumer.stop(timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001  停止失败不阻断实例回收
                logger.warning(f"VIL consumer stop failed: {exc}")

    # ── 每 tick 调用 ────────────────────────────────────────────────────────

    def step(self, delta_seconds: float) -> None:
        """在 ``world.tick()`` 前应用一次位姿（数据中断时减速）。"""
        with self._lock:
            self._steps += 1
            service = self._bind_locked()
            if service is None:
                return
            try:
                outcome = service.apply_once()
            except Exception as exc:  # noqa: BLE001  单帧失败不阻断场景
                logger.debug(f"VIL apply_once failed: {exc}")
                return
            self._interrupted = outcome == "interrupted"
            if outcome == "applied":
                self._applied += 1

    def status_override(self) -> Optional[tuple[str, Optional[str]]]:
        """返回状态覆盖（数据中断 → PAUSED），否则 None 由运行器自行上报。"""
        if self._interrupted:
            return ("PAUSED", "vil_data_interrupted")
        return None

    @property
    def interrupted(self) -> bool:
        """当前是否处于 VIL 数据中断状态。"""
        return self._interrupted

    @property
    def stats(self) -> dict[str, Any]:
        """运行统计（脱敏，供实例视图/排障）。"""
        return {
            "steps": self._steps,
            "applied": self._applied,
            "interrupted": self._interrupted,
            "buffer_frames": self._buffer.total_received,
            "max_frames": self._buffer.max_frames,
            "extrapolation_ms": self._vil_settings.extrapolation_ms,
            "max_data_latency_ms": self._vil_settings.max_data_latency_ms,
        }

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _bind_locked(self) -> Optional[VILSyncService]:
        """延迟绑定当前运行器/自车；场景重载（自车替换）时重建并清空缓存。"""
        runner = self._runner_provider()
        if runner is None:
            return None
        vehicle = getattr(runner, "ego_vehicle", None)
        world = getattr(runner, "world", None)
        if vehicle is None or world is None:
            return None
        if self._service is not None and vehicle is self._bound_vehicle:
            return self._service

        # 会话/场景切换：清空帧缓存（消费者持有同一 buffer 引用，不能替换对象）
        self._buffer.clear()
        consumer = self._consumer
        resetter = getattr(consumer, "reset_ordering_guard", None)
        if callable(resetter):
            resetter()
        self._health = DataHealthMonitor(self._vil_settings, self._buffer)
        self._service = VILSyncService(
            settings=self._vil_settings,
            buffer=self._buffer,
            coord_transformer=self._transformer,
            extrapolator=self._extrapolator,
            health_monitor=self._health,
            vehicle_actor=vehicle,
            world=world,
            ground_z_getter=self._ground_z_getter(world),
        )
        self._bound_vehicle = vehicle
        logger.info(f"VIL runtime bound to ego actor: sim_id={self._sim_id}")
        return self._service

    @staticmethod
    def _ground_z_getter(world: Any) -> Optional[Callable[[float, float], float]]:
        """构造地图路面高度获取器（无 CARLA SDK/接口时返回 None）。"""
        try:
            import carla  # noqa: PLC0415
            game_map = world.get_map()
        except Exception:  # noqa: BLE001
            return None

        def _get(x: float, y: float) -> float:
            try:
                waypoint = game_map.get_waypoint(carla.Location(x=x, y=y, z=100.0))
                return float(waypoint.transform.location.z) if waypoint else 0.0
            except Exception:  # noqa: BLE001
                return 0.0

        return _get

    def _estimate_speed(self) -> float:
        """当前自车速度（用于统计/降级）。"""
        try:
            vehicle = self._bound_vehicle
            velocity = vehicle.get_velocity()
            return math.sqrt(velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2)
        except Exception:  # noqa: BLE001
            return 0.0


def _optional_int(value: Any) -> Optional[int]:
    """安全转 int（None/非法返回 None）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["VILRuntime"]
