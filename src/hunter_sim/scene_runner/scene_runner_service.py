"""场景运行主服务（V2.10.0：平台接入）。

SceneRunnerService 管理场景完整生命周期：
创建 → 加载 → 就绪 → 运行（支持暂停/恢复）→ 完成/失败 → 销毁。
支持两种运行模式：ScenarioRunner（OpenSCENARIO）和自定义 Python 脚本。

接入平台发布链路（可选，注入 sim_data_producer 后启用）：
- sim_status   ：状态变化 + 1Hz 定时发布
- sim_telemetry：10Hz 自车遥测发布
- sim_event    ：检测事件发布
"""

from __future__ import annotations

import threading
import time

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from hunter_sim.common.exceptions import (
    CarlaSimulationError,
    ConfigurationError,
)
from hunter_sim.common.models import SceneStatus
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.weather_manager import (
    PRESET_ENVIRONMENTS,
    WeatherManager,
    WeatherProfile,
)
from hunter_sim.scene_runner.builtin_scenario import BuiltinScenario
from hunter_sim.scene_runner.custom_scenario import CustomScenarioBase
from hunter_sim.scene_runner.event_detector import DetectedEvent, EventDetector
from hunter_sim.scene_runner.scene_config import SceneConfig
from hunter_sim.scene_runner.scene_state_manager import SceneStateSnapshot, SceneStateManager

logger = get_logger(__name__)


class SceneRunnerService:
    """场景运行服务主类。

    Args:
        world: CARLA World 对象（已加载地图）。
        on_state_change: 状态变化回调，用于 WebSocket 推送。
        on_event: 事件触发回调。
        sim_data_producer: 可选 SimDataProducer 实例（平台发布链路）。
        sim_id: 当前仿真实例 ID（平台契约用）。
        vehicle_id: 当前孪生车 ID（SIL 模式为空）。
        scene_id: 当前场景 ID（平台契约用）。
    """

    def __init__(
        self,
        world: Any,
        on_state_change: Optional[Callable[[SceneStateSnapshot], None]] = None,
        on_event: Optional[Callable[[DetectedEvent], None]] = None,
        sim_data_producer: Optional[Any] = None,
        sim_id: str = "",
        vehicle_id: str = "",
        scene_id: str = "",
    ) -> None:
        self._world = world
        self._on_state_change = on_state_change
        self._on_event = on_event
        self._sim_data_producer = sim_data_producer
        self._sim_id = sim_id
        self._vehicle_id = vehicle_id
        self._scene_id = scene_id

        # 天气管理器：API 层天气控制（scenes.set_weather_impl）与渐变推进均使用此实例
        self._weather_manager = WeatherManager(world)

        self._lock: threading.RLock = threading.RLock()
        self._state_mgr: Optional[SceneStateManager] = None
        self._event_detector: Optional[EventDetector] = None
        self._scenario: Optional[CustomScenarioBase] = None
        self._scenario_cleaned: bool = True
        self._config: Optional[SceneConfig] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        # 平台发布节流时间戳
        self._last_status_publish: float = 0.0
        self._last_telemetry_publish: float = 0.0
        self._status_interval: float = 1.0      # sim_status 1Hz
        self._telemetry_interval: float = 0.1   # sim_telemetry 10Hz

    @property
    def weather_manager(self) -> WeatherManager:
        """天气管理器（供 API 层天气控制与渐变过渡）。"""
        return self._weather_manager

    @property
    def current_scene_id(self) -> Optional[str]:
        """当前场景 ID（无场景时返回 None）。"""
        with self._lock:
            return self._config.scene_id if self._config else None

    @property
    def has_scene(self) -> bool:
        """是否已下发场景（供 API 判定 409 前置条件）。"""
        with self._lock:
            return self._state_mgr is not None

    @property
    def is_running(self) -> bool:
        """场景循环线程是否存活。"""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def get_state_snapshot(self) -> Optional[SceneStateSnapshot]:
        """获取当前场景状态快照。"""
        with self._lock:
            return self._state_mgr.get_snapshot() if self._state_mgr else None

    def get_scene_state(self) -> dict[str, Any]:
        """当前场景状态的序列化字典（供 GET /scenes/{id}/status 端点，与测试桩接口对齐）。"""
        snapshot = self.get_state_snapshot()
        if snapshot is None:
            return {
                "scene_id": None,
                "status": "none",
                "elapsed_seconds": 0.0,
                "progress": 0.0,
                "error_message": None,
            }
        return {
            "scene_id": snapshot.scene_id,
            "status": snapshot.status.value,
            "elapsed_seconds": round(snapshot.elapsed_seconds, 3),
            "progress": round(snapshot.progress, 4),
            "error_message": snapshot.error_message,
        }

    def load_scene(self, config: SceneConfig, scenario: Optional[CustomScenarioBase] = None) -> None:
        """加载场景配置，初始化状态机和事件检测器，并把场景落到 CARLA 世界。

        状态转换：CREATED → LOADING → READY。
        默认使用 ``BuiltinScenario``（按配置生成自车/参与者）；传入 ``scenario``
        时使用自定义 Python 场景。

        Args:
            config: 已验证的场景配置。
            scenario: 自定义 Python 场景实例（可选，None 表示使用内置执行器）。

        Raises:
            ConfigurationError: 配置无效。
            CarlaSimulationError: 地图或 Actor 初始化失败。
        """
        preset = config.weather.preset_name
        if preset and preset not in PRESET_ENVIRONMENTS:
            raise ConfigurationError(
                "load_scene",
                f"Unknown weather preset '{preset}'. Available: {list(PRESET_ENVIRONMENTS)}",
            )

        # 运行中重新下发：先停机（join 在锁外执行，避免与 _run_loop 互等）
        self._stop_if_running()

        with self._lock:
            logger.info(f"Loading scene: {config.scene_id} ({config.mode.value} mode)")

            # 回收上一场景 Actor，避免重复下发后旧 Actor 残影
            self._cleanup()

            self._config = config
            self._apply_scene_weather(config)
            self._state_mgr = SceneStateManager(
                scene_id=config.scene_id,
                on_state_change=self._on_state_change,
            )
            self._state_mgr.set_duration(config.duration_seconds)
            self._state_mgr.transition_to(SceneStatus.LOADING)

            # 初始化事件检测器
            self._event_detector = EventDetector(
                event_definitions=config.scene_events,
                on_event=self._on_event,
            )

            if scenario is None:
                scenario = BuiltinScenario(config, self._state_mgr, self._event_detector)
            self._scenario = scenario
            self._scenario_cleaned = False

            try:
                scenario.setup(self._world)
            except Exception as exc:
                self._state_mgr.transition_to(SceneStatus.FAILED, str(exc))
                if isinstance(exc, (ConfigurationError, CarlaSimulationError)):
                    raise
                raise CarlaSimulationError("scene_setup", str(exc)) from exc

            self._state_mgr.transition_to(SceneStatus.READY)
            logger.info(f"Scene '{config.scene_id}' ready to start")

    def start(self) -> None:
        """启动场景运行（非阻塞，在独立线程中执行仿真循环）。

        状态转换：READY → RUNNING。

        Raises:
            ConfigurationError: 未加载场景或场景已在运行。
        """
        with self._lock:
            if self._state_mgr is None:
                raise ConfigurationError("start", "No scene loaded")
            # 重入保护：双线程 tick 同一 world 会导致 CARLA 帧同步错乱
            if self._thread is not None and self._thread.is_alive():
                raise ConfigurationError("start", "Scene is already running")
            self._state_mgr.transition_to(SceneStatus.RUNNING)

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name=f"SceneRunner-{self._config.scene_id if self._config else 'unknown'}",
                daemon=True,
            )
            self._thread.start()
        logger.info(f"Scene '{self._config.scene_id if self._config else ''}' started")

    def pause(self) -> None:
        """暂停场景运行。状态转换：RUNNING → PAUSED。"""
        with self._lock:
            if self._state_mgr is None:
                raise ConfigurationError("pause", "No scene loaded")
            self._state_mgr.transition_to(SceneStatus.PAUSED)
        logger.info("Scene paused")

    def resume(self) -> None:
        """恢复场景运行。状态转换：PAUSED → RUNNING。"""
        with self._lock:
            if self._state_mgr is None:
                raise ConfigurationError("resume", "No scene loaded")
            self._state_mgr.transition_to(SceneStatus.RUNNING)
        logger.info("Scene resumed")

    def stop(self, timeout_s: float = 5.0) -> None:
        """停止场景运行并清理资源。状态 → COMPLETED。"""
        with self._lock:
            if self._state_mgr is None:
                return
            if self._state_mgr.status in (SceneStatus.RUNNING, SceneStatus.PAUSED):
                self._state_mgr.transition_to(SceneStatus.COMPLETED)

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_s)

        self._cleanup()
        logger.info("Scene stopped and cleaned up")

    def destroy(self) -> None:
        """销毁场景，释放所有资源。"""
        self.stop()
        with self._lock:
            self._config = None
            self._state_mgr = None
            self._event_detector = None
            self._scenario = None
        logger.info("Scene destroyed")

    @property
    def detected_events(self) -> list[DetectedEvent]:
        """获取已检测事件列表。"""
        with self._lock:
            return self._event_detector.detected_events if self._event_detector else []

    def _run_loop(self) -> None:
        """场景运行主循环（独立线程执行）。"""
        delta_seconds = 0.02  # 50Hz
        tick_count = 0
        timeout_time = (
            time.perf_counter() + self._config.timeout_seconds
            if self._config
            else time.perf_counter() + 7200.0
        )

        while not self._stop_event.is_set():
            t0 = time.perf_counter()

            # 超时检查
            if time.perf_counter() > timeout_time:
                logger.warning("Scene timeout, stopping")
                with self._lock:
                    if self._state_mgr and self._state_mgr.status == SceneStatus.RUNNING:
                        self._state_mgr.transition_to(SceneStatus.FAILED, "Timeout")
                break

            # 暂停等待
            if self._state_mgr and self._state_mgr.status == SceneStatus.PAUSED:
                time.sleep(0.1)
                continue

            try:
                # 执行场景 tick
                if self._scenario is not None:
                    self._scenario._update_elapsed()
                    timestamp = tick_count * delta_seconds

                    # 场景自判完成（内置场景：运行时长达到 duration_seconds）
                    if self._scenario.check_finish(self._world, timestamp):
                        with self._lock:
                            if self._state_mgr and self._state_mgr.status == SceneStatus.RUNNING:
                                self._state_mgr.transition_to(SceneStatus.COMPLETED)
                        break

                    # 兜底：自定义场景未自判完成时按 duration 超时失败
                    if self._scenario.is_timeout():
                        with self._lock:
                            if self._state_mgr and self._state_mgr.status == SceneStatus.RUNNING:
                                self._state_mgr.transition_to(SceneStatus.FAILED, "Duration timeout")
                        break

                    self._scenario.tick(self._world, timestamp, delta_seconds)

                # 推进仿真
                self._world.tick()
                # 推进天气渐变过渡（文档 §3.4.1：每 tick 推进一步）
                self._weather_manager.update_transition()
                tick_count += 1

                # V2.10.0：平台发布链路（sim_telemetry 10Hz + sim_status 1Hz）
                if self._sim_data_producer is not None:
                    now = time.time()
                    if now - self._last_telemetry_publish >= self._telemetry_interval:
                        self._last_telemetry_publish = now
                        self._publish_sim_telemetry(tick_count * delta_seconds)
                    if now - self._last_status_publish >= self._status_interval:
                        self._last_status_publish = now
                        self._publish_sim_status()

            except CarlaSimulationError as exc:
                logger.error(f"Scene tick error: {exc}")
                with self._lock:
                    if self._state_mgr and self._state_mgr.status == SceneStatus.RUNNING:
                        self._state_mgr.transition_to(SceneStatus.FAILED, str(exc))
                break
            except Exception as exc:
                logger.error(f"Unexpected scene loop error: {exc}", exc_info=True)
                with self._lock:
                    if self._state_mgr and self._state_mgr.status == SceneStatus.RUNNING:
                        self._state_mgr.transition_to(SceneStatus.FAILED, str(exc))
                break

            # 帧率控制
            elapsed = time.perf_counter() - t0
            sleep_time = max(0.0, delta_seconds - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        self._cleanup()

    def _cleanup(self) -> None:
        """清理场景资源（幂等：同一场景只清理一次）。"""
        scenario = self._scenario
        if scenario is None or self._scenario_cleaned:
            return
        self._scenario_cleaned = True
        try:
            scenario.cleanup(self._world)
        except Exception as exc:
            logger.warning(f"Scenario cleanup error: {exc}")

    def _stop_if_running(self, timeout_s: float = 5.0) -> None:
        """若有场景循环线程在跑，先停掉（不修改公开状态机，供重新下发场景使用）。"""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self._stop_event.set()
        thread.join(timeout=timeout_s)
        if thread.is_alive():
            logger.warning(f"Scene loop did not stop within {timeout_s:.0f}s during reload")

    def _apply_scene_weather(self, config: SceneConfig) -> None:
        """把场景配置中的天气下发到 world（预设名优先，否则自定义参数）。"""
        weather = config.weather
        try:
            if weather.preset_name:
                self._weather_manager.set_preset(weather.preset_name)
            else:
                profile = WeatherProfile(
                    cloudiness=weather.cloudiness,
                    precipitation=weather.precipitation,
                    precipitation_deposits=weather.precipitation_deposits,
                    wind_intensity=weather.wind_intensity,
                    sun_azimuth_angle=weather.sun_azimuth_angle,
                    sun_altitude_angle=weather.sun_altitude_angle,
                    fog_density=weather.fog_density,
                    fog_distance=weather.fog_distance,
                )
                self._weather_manager.set_weather(profile)
        except Exception as exc:  # noqa: BLE001  天气下发失败不应阻断场景加载
            logger.warning(f"Apply scene weather failed: {exc}")

    # ── V2.10.0 平台发布辅助方法 ─────────────────────────────────────────

    def _publish_sim_status(self) -> None:
        """发布 sim_status 消息（状态变化 + 1Hz 定时调用）。

        无 sim_data_producer 时安全返回。
        """
        if self._sim_data_producer is None:
            return
        try:
            from hunter_sim.cloud.sim_data_mapper import SimStatusPayload, build_sim_status  # noqa: PLC0415

            snapshot = self.get_state_snapshot()
            if snapshot is None:
                return
            payload = SimStatusPayload(
                status=snapshot.status.value.upper(),
                progress_percent=round(snapshot.progress * 100.0, 2),
                elapsed_time=round(snapshot.elapsed_seconds, 3),
                total_time=self._config.duration_seconds if self._config else 60.0,
                actors_count=self._get_actor_count(),
                collisions=self._get_collision_count(),
                lane_invasions=self._get_lane_invasion_count(),
                ego_state=self._get_ego_state(),
                message=snapshot.error_message or None,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            msg = build_sim_status(
                sim_id=self._sim_id,
                scene_id=snapshot.scene_id or self._scene_id,
                payload=payload,
            )
            self._sim_data_producer.publish_status(msg)
        except Exception as exc:  # noqa: BLE001  发布失败不阻断仿真
            logger.debug(f"_publish_sim_status failed: {exc}")

    def _publish_sim_telemetry(self, sim_time: float) -> None:
        """发布 sim_telemetry 消息（10Hz）。

        Args:
            sim_time: 当前仿真时间轴（秒）。
        """
        if self._sim_data_producer is None:
            return
        try:
            from hunter_sim.cloud.sim_data_mapper import build_sim_telemetry  # noqa: PLC0415

            ego = self._get_ego_state()
            seq = self._sim_data_producer.next_seq()
            msg = build_sim_telemetry(
                sim_id=self._sim_id,
                vehicle_id=self._vehicle_id or None,
                scene_id=self._scene_id,
                seq=seq,
                ts=time.time(),
                pose={
                    "x": ego.get("x", 0.0),
                    "y": ego.get("y", 0.0),
                    "z": 0.0,
                    "roll": 0.0,
                    "pitch": 0.0,
                    "heading": ego.get("heading", 0.0),
                },
                twist={
                    "vx": ego.get("velocity", 0.0),
                    "vy": 0.0,
                    "vz": 0.0,
                    "wx": 0.0,
                    "wy": 0.0,
                    "wz": 0.0,
                },
                velocity={
                    "m_s": ego.get("velocity", 0.0),
                    "km_h": round(ego.get("velocity", 0.0) * 3.6, 3),
                },
                steering=ego.get("steering", 0.0),
                acceleration={"ax": 0.0, "ay": 0.0, "az": 0.0},
            )
            self._sim_data_producer.publish_telemetry(msg)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"_publish_sim_telemetry failed: {exc}")

    def publish_event_to_platform(self, event: DetectedEvent) -> None:
        """发布单条事件到平台 sim_event（由 on_event 回调链调用）。

        注意：本方法设计为与现有 ``on_event`` 回调链并联，不会中断现有推送。
        """
        if self._sim_data_producer is None:
            return
        try:
            self._sim_data_producer.publish_event(
                vehicle_id=self._vehicle_id or None,
                scene_id=self._scene_id,
                event_type=event.event_type,
                event_level=event.event_level,
                sim_time=event.sim_time or event.time_stamp,
                description=event.description,
                data=event.data,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"publish_event_to_platform failed: {exc}")

    def _get_actor_count(self) -> int:
        """获取世界中当前 Actor 数量（失败时返回 0）。"""
        try:
            return len(self._world.get_actors())
        except Exception:  # noqa: BLE001
            return 0

    def _get_collision_count(self) -> int:
        """获取碰撞计数（从事件检测器读）。"""
        if self._event_detector is None:
            return 0
        return sum(
            1 for e in self._event_detector.detected_events
            if "collision" in e.event_type.lower()
        )

    def _get_lane_invasion_count(self) -> int:
        """获取车道偏离计数。"""
        if self._event_detector is None:
            return 0
        return sum(
            1 for e in self._event_detector.detected_events
            if "lane" in e.event_type.lower()
        )

    def _get_ego_state(self) -> dict[str, Any]:
        """获取自车状态字典。

        尝试从 scenario 获取车辆 actor，否则通过 world 查找 vehicle 类型 Actor。
        失败时返回零值字典。
        """
        default: dict[str, Any] = {
            "x": 0.0, "y": 0.0, "heading": 0.0, "velocity": 0.0, "steering": 0.0
        }
        try:
            vehicle = None
            scenario = self._scenario
            if scenario is not None and hasattr(scenario, "get_ego_vehicle"):
                vehicle = scenario.get_ego_vehicle()  # type: ignore[union-attr]
            if vehicle is None:
                # 内置场景 BuiltinScenario 持有 _ego_vehicle
                vehicle = getattr(scenario, "_ego_vehicle", None)
            if vehicle is None:
                return default
            loc = vehicle.get_location()
            rot = vehicle.get_rotation()
            vel = vehicle.get_velocity()
            import math  # noqa: PLC0415
            speed = math.sqrt(vel.x ** 2 + vel.y ** 2 + vel.z ** 2)
            ctrl = vehicle.get_control()
            return {
                "x": round(loc.x, 4),
                "y": round(loc.y, 4),
                "heading": round(math.radians(rot.yaw), 6),
                "velocity": round(speed, 4),
                "steering": round(float(ctrl.steer), 6),
            }
        except Exception:  # noqa: BLE001
            return default
