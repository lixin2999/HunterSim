"""场景运行主服务（V2.10.0：平台接入）。

SceneRunnerService 管理场景完整生命周期：
创建 → 加载 → 就绪 → 运行（支持暂停/恢复）→ 完成/失败 → 销毁。
支持两种运行模式：ScenarioRunner（OpenSCENARIO）和自定义 Python 脚本。

接入平台发布链路（可选，注入 sim_data_producer 后启用）：
- sim_status   ：状态跃迁即时发布 + 周期定时发布（默认 1Hz，PAUSED 与终态均上报）
- sim_telemetry：10Hz 自车遥测发布
- sim_event     ：检测事件发布（与现有 on_event 回调链并联）
"""

from __future__ import annotations

import math
import threading
import time

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

# 场景终态：到达后不再推进，需向平台补发最终 sim_status
_TERMINAL_SCENE_STATUSES = (SceneStatus.COMPLETED, SceneStatus.FAILED)

#: 自车状态取数失败告警限频窗口（秒）：50Hz 下每 tick 告警会淹没日志
_EGO_STATE_WARN_INTERVAL_S = 30.0

#: 视为「自车」的 CARLA role_name（与 CARLA 官方 example 一致）
_EGO_ROLE_NAME = "hero"

#: 参与者状态缓存刷新周期（tick 数）：50Hz / 10 = 5Hz，与可视化面板刷新频率一致。
#: 每次刷新都要读每个 Actor 的位姿（CARLA RPC），太高会把 tick 线程拖慢。
_ACTOR_STATE_REFRESH_TICKS = 10

#: 等待 tick 线程退出的上限（秒）。V3.5.6：旧值 5s 在现场实测「销毁 4 个传感器
#: + 自车 + 3 个参与者」耗时 ~7s 的情况下会提前返回，释放线程随后对同一个 world
#: 下发 ``apply_settings``，与 tick 线程的 ``actor.destroy()`` 并发 → CARLA Client
#: 非线程安全，进程无 traceback 原生退出（0xC0000409）。多等 15s 远比崩溃便宜。
_RUNNER_JOIN_TIMEOUT_S = 20.0


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
        status_interval_s: sim_status 周期上报间隔（秒，默认 1.0）。
        telemetry_interval_s: sim_telemetry 上报间隔（秒，默认 0.1）。
        pose_driver: VIL 位姿驱动（每 tick 前调用）。
        sample_sink: 轨迹采样回调（评估引擎消费）。
        status_provider: 状态覆盖回调（VIL 数据中断→PAUSED）。
        frame_hub: 传感器帧汇聚中心（控制面 MJPEG/截图取用）。
        recorder: mcap 录制器（场景期间落盘传感器数据）。
        ros_bridge: ROS2 桥接器（可选出口）。
        screenshot_quality: 截图 JPEG 质量（Pillow 可用时生效）。
        stream_max_width: 回传画面最大宽度（0 表示不降采样）。
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
        status_interval_s: float = 1.0,
        telemetry_interval_s: float = 0.1,
        pose_driver: Optional[Callable[[float], None]] = None,
        sample_sink: Optional[Callable[[dict[str, Any], float], None]] = None,
        status_provider: Optional[Callable[[], Optional[tuple[str, Optional[str]]]]] = None,
        frame_hub: Optional[Any] = None,
        recorder: Optional[Any] = None,
        ros_bridge: Optional[Any] = None,
        screenshot_quality: int = 75,
        stream_max_width: int = 0,
        traffic_manager: Optional[Any] = None,
        traffic_manager_port: int = 8000,
    ) -> None:
        self._world = world
        self._on_state_change = on_state_change
        self._on_event = on_event
        self._sim_data_producer = sim_data_producer
        self._sim_id = sim_id
        self._vehicle_id = vehicle_id
        self._scene_id = scene_id
        # VIL 扩展点：每 tick 前驱动自车位姿；每 tick 后采样；状态覆盖（数据中断→PAUSED）
        self._pose_driver = pose_driver
        self._sample_sink = sample_sink
        self._status_provider = status_provider
        # Sensor Emulator 出口（FrameHub → MJPEG/截图；McapRecorder → sensor_file）
        self._frame_hub = frame_hub
        self._recorder = recorder
        self._ros_bridge = ros_bridge
        self._screenshot_quality = screenshot_quality
        self._stream_max_width = stream_max_width
        # Traffic Manager（同步模式下必须同步推进 autopilot 车辆）
        self._traffic_manager = traffic_manager
        self._traffic_manager_port = int(traffic_manager_port)

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
        # 周期来自 CloudSettings（status_report_interval_s / telemetry_proxy_interval_s）
        self._status_interval: float = max(0.05, float(status_interval_s))
        self._telemetry_interval: float = max(0.01, float(telemetry_interval_s))
        # 终态只补发一次（stop() 与运行循环结束都可能到达终态）
        self._terminal_status_published: bool = False
        # 自车状态缓存（由 tick 线程写入，executor 线程安全读取，避免跨线程 CARLA RPC）
        self._ego_state_cache: dict[str, Any] = {}
        # 自车状态取数失败告警限频状态
        self._ego_state_warn_at: float = 0.0
        self._ego_state_warn_count: int = 0
        # 交通参与者状态缓存（tick 线程 5Hz 写入，executor/HTTP 线程无锁读取）：
        # 可视化「参与者」面板靠它看到「谁在跑、跑多快、在自车哪个方位」
        self._actor_state_cache: list[dict[str, Any]] = []
        self._actor_state_warn_at: float = 0.0

    @property
    def weather_manager(self) -> WeatherManager:
        """天气管理器（供 API 层天气控制与渐变过渡）。"""
        return self._weather_manager

    @property
    def frame_hub(self) -> Any:
        """传感器帧汇聚中心（未注入时为 None）。"""
        return self._frame_hub

    @property
    def world(self) -> Any:
        """底层 CARLA World（VIL 运行时绑定用）。"""
        return self._world

    @property
    def ego_vehicle(self) -> Any:
        """当前场景自车 Actor（未就绪时返回 None）。"""
        return self._resolve_ego_actor()

    def _resolve_ego_actor(self) -> Any:
        """解析自车 Actor：场景对象各形态访问器 → 世界兜底。

        「自车状态全 0」的根因往往不是状态算错，而是自车引用没解析到：不同场景
        实现分别用 ``ego_actor`` / ``get_ego_vehicle()`` / ``_ego_vehicle`` /
        ``_ego`` 暴露，旧实现只取第一个非 None 的属性且不跳 None，任一形态返回
        None 就会让整个状态取数落到零值。这里逐个尝试（跳过 None），仍取不到时
        按 ``role_name=hero`` 与「世界中唯一车辆」兜底。
        """
        scenario = self._scenario
        if scenario is not None:
            actor = self._actor_from_scenario(scenario)
            if actor is not None:
                return actor
        return self._actor_from_world()

    @staticmethod
    def _actor_from_scenario(scenario: Any) -> Any:
        """从场景对象取自车 Actor（属性 / 方法两种形态，跳过 None）。"""
        for name in ("ego_actor", "_ego_vehicle", "_ego"):
            try:
                candidate = getattr(scenario, name, None)
            except Exception as exc:  # noqa: BLE001  property 求值异常不阻断解析
                logger.debug(f"ego attribute '{name}' failed: {exc}")
                continue
            if candidate is not None:
                return candidate
        getter = getattr(scenario, "get_ego_vehicle", None)
        if callable(getter):
            try:
                candidate = getter()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"get_ego_vehicle failed: {exc}")
            else:
                if candidate is not None:
                    return candidate
        return None

    def _actor_from_world(self) -> Any:
        """世界兜底寻找自车：``role_name=hero`` 优先，否则「唯一车辆」。"""
        world = self._world
        if world is None:
            return None
        try:
            actors = list(world.get_actors().filter("vehicle.*"))
        except Exception as exc:  # noqa: BLE001  接口缺失 / 跨线程读失败
            logger.debug(f"ego actor world lookup failed: {exc}")
            return None
        for actor in actors:
            try:
                if str(actor.attributes.get("role_name", "")).lower() == _EGO_ROLE_NAME:
                    return actor
            except Exception:  # noqa: BLE001  测试桩无 attributes
                continue
        return actors[0] if len(actors) == 1 else None

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
        if self._stop_if_running():
            # V3.5.6：tick 线程还在销毁上一个场景的 Actor，此时继续 setup 就是
            # 两个线程并发读写同一个 world（与崩溃同源）。宁可把本次下发判为
            # FAILED 让平台重发，也不能拿进程崩溃当代价。
            raise CarlaSimulationError(
                "scene_reload",
                "上一场景清理未完成（tick 线程仍在销毁 Actor），已拒绝重下发；请重新提交场景",
            )
        # 运行循环退出时会把世界恢复为异步（便于安全清理），重下发后需要
        # 重新切回同步，否则下一轮 world.tick() 不再驱动仿真
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._ensure_sync_mode()

        with self._lock:
            logger.info(f"Loading scene: {config.scene_id} ({config.mode.value} mode)")

            # 回收上一场景 Actor，避免重复下发后旧 Actor 残影
            self._cleanup()

            self._config = config
            self._apply_scene_weather(config)
            self._state_mgr = SceneStateManager(
                scene_id=config.scene_id,
                on_state_change=self._handle_state_change,
            )
            self._state_mgr.set_duration(config.duration_seconds)
            self._state_mgr.transition_to(SceneStatus.LOADING)

            # 初始化事件检测器
            self._event_detector = EventDetector(
                event_definitions=config.scene_events,
                on_event=self._handle_event,
            )

            if scenario is None:
                scenario = BuiltinScenario(
                    config,
                    self._state_mgr,
                    self._event_detector,
                    frame_hub=self._frame_hub,
                    recorder=self._recorder,
                    ros_bridge=self._ros_bridge,
                    screenshot_quality=self._screenshot_quality,
                    stream_max_width=self._stream_max_width,
                    traffic_manager=self._traffic_manager,
                    traffic_manager_port=self._traffic_manager_port,
                )
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

    def stop(self, timeout_s: float = _RUNNER_JOIN_TIMEOUT_S) -> bool:
        """停止场景运行并清理资源。状态 → COMPLETED。

        注意：若 tick 线程在超时内未能退出，**不会**在这里并发清理场景资源
        （CARLA 客户端非线程安全，跨线程销毁 Actor 会让 LibCarla 原生断言）；
        运行循环退出时会自行执行 ``_cleanup()``。

        Returns:
            ``False`` 表示已停稳并完成清理；``True`` 表示 tick 线程仍在持有
            world（清理由该线程收尾），调用方在此期间不得再发任何 CARLA 请求。
        """
        with self._lock:
            if self._state_mgr is None:
                return False
            if self._state_mgr.status in (SceneStatus.RUNNING, SceneStatus.PAUSED):
                self._state_mgr.transition_to(SceneStatus.COMPLETED)

        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout_s)
            if thread.is_alive():
                logger.warning(
                    f"Scene loop did not stop within {timeout_s:.0f}s; "
                    "deferring cleanup to the tick thread"
                )
                return True

        self._restore_async()
        self._cleanup()
        logger.info("Scene stopped and cleaned up")
        return False

    def destroy(self, timeout_s: float = _RUNNER_JOIN_TIMEOUT_S) -> bool:
        """销毁场景，释放所有资源。

        Returns:
            ``True`` 表示清理被 tick 线程接走（本线程未做任何 CARLA 收尾）。
        """
        deferred = self.stop(timeout_s=timeout_s)
        if deferred:
            # 运行循环仍在 world.tick()/Actor 销毁内：保留场景引用，退出时会自行清理
            logger.warning("Scene destroy deferred: tick thread still alive")
            return True
        with self._lock:
            self._config = None
            self._state_mgr = None
            self._event_detector = None
            self._scenario = None
        logger.info("Scene destroyed")
        return False

    @property
    def detected_events(self) -> list[DetectedEvent]:
        """获取已检测事件列表。"""
        with self._lock:
            return self._event_detector.detected_events if self._event_detector else []

    def actor_states(self) -> list[dict[str, Any]]:
        """获取交通参与者状态快照（最近一次 tick 线程缓存）。

        返回空列表表示：场景无参与者、或场景尚未开始 tick。字段见
        ``BuiltinScenario.participant_states``：``id/actor_type/behavior/
        blueprint/spawned/速度/世界坐标/ahead_m/left_m/distance_to_ego_m/visible``。
        """
        return [dict(state) for state in self._actor_state_cache]

    def _refresh_actor_states(self) -> None:
        """刷新参与者状态缓存（仅 tick 线程调用）。

        取数失败不阻断仿真：保留上一帧快照并限频告警，避免 50Hz 日志风暴。
        """
        scenario = self._scenario
        if scenario is None:
            return
        getter = getattr(scenario, "participant_states", None)
        if not callable(getter):
            return
        try:
            states = list(getter())
        except Exception as exc:  # noqa: BLE001  单个场景实现异常不影响仿真
            now = time.monotonic()
            if now - self._actor_state_warn_at >= _EGO_STATE_WARN_INTERVAL_S:
                self._actor_state_warn_at = now
                logger.warning(
                    f"Participant states unavailable: {type(exc).__name__}: {exc}"
                )
            return
        self._actor_state_cache = states

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

            # 暂停等待：仍按周期上报 sim_status(PAUSED)，否则平台看不到暂停态
            if self._state_mgr and self._state_mgr.status == SceneStatus.PAUSED:
                if self._sim_data_producer is not None:
                    now = time.time()
                    if now - self._last_status_publish >= self._status_interval:
                        self._last_status_publish = now
                        self._publish_sim_status()
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

                # VIL：在唯一一次 world.tick() 之前应用实车位姿（禁止并行 tick 循环）
                if self._pose_driver is not None:
                    self._pose_driver(delta_seconds)

                # 推进仿真
                self._world.tick()
                # 推进天气渐变过渡（文档 §3.4.1：每 tick 推进一步）
                self._weather_manager.update_transition()
                tick_count += 1

                # 无条件更新自车状态缓存（供 executor 线程安全读取，不依赖 sample_sink / producer）
                self._get_ego_state()
                # 参与者状态缓存（5Hz）：可视化面板与「拍不到」排查都靠它
                if tick_count % _ACTOR_STATE_REFRESH_TICKS == 0:
                    self._refresh_actor_states()

                # 评估/回传采样钩子（VIL 轨迹、指标计算用）
                if self._sample_sink is not None:
                    try:
                        self._sample_sink(dict(self._ego_state_cache), tick_count * delta_seconds)
                    except Exception as exc:  # noqa: BLE001  采样失败不阻断仿真
                        logger.debug(f"sample_sink failed: {exc}")

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

        # 终态补发：运行循环 break 后平台需收到最终状态（而非停在最后一条 RUNNING）
        # V3.4.4：收尾每一步都落 INFO——CARLA 原生 fail-fast 会让进程直接退出，
        # 只能靠最后一条日志定位卡在哪一步（现场曾出现 completed 后无声退出）
        logger.info("Scene loop exited: publishing terminal status")
        self._publish_terminal_status()
        # 先恢复异步再清理：同步模式下销毁 Actor/传感器会阻塞等待 tick
        # （没有后续 tick 时即表现为 CARLA 卡死）；异步模式下销毁不依赖 tick
        logger.info("Scene loop: restoring async world mode")
        self._restore_async()
        logger.info("Scene loop: cleaning up scenario resources")
        self._cleanup()
        logger.info("Scene loop: cleanup finished, thread exiting")

    def _restore_async(self) -> None:
        """把世界与 Traffic Manager 恢复为异步模式（幂等、失败不抛）。"""
        if self._traffic_manager is not None:
            try:
                self._traffic_manager.set_synchronous_mode(False)
            except Exception as exc:  # noqa: BLE001  TM 可能已随世界失效
                logger.debug(f"Restore Traffic Manager async failed: {exc}")
        try:
            settings = self._world.get_settings()
            if settings is not None:
                settings.synchronous_mode = False
                settings.fixed_delta_seconds = 0.0
                self._world.apply_settings(settings)
        except Exception as exc:  # noqa: BLE001  Mock/已销毁世界无设置接口
            logger.debug(f"Restore world async failed: {exc}")

    def _ensure_sync_mode(self) -> None:
        """把世界与 Traffic Manager 切回同步模式（重下发场景时幂等、失败不抛）。"""
        if self._traffic_manager is not None:
            try:
                self._traffic_manager.set_synchronous_mode(True)
            except Exception as exc:  # noqa: BLE001  TM 可能已随世界失效
                logger.debug(f"Enable Traffic Manager sync failed: {exc}")
        try:
            settings = self._world.get_settings()
            if settings is not None:
                settings.synchronous_mode = True
                settings.fixed_delta_seconds = 0.02
                self._world.apply_settings(settings)
        except Exception as exc:  # noqa: BLE001  Mock/已销毁世界无设置接口
            logger.debug(f"Enable world sync failed: {exc}")

    def _cleanup(self) -> None:
        """清理场景资源（幂等：同一场景只清理一次）。"""
        scenario = self._scenario
        if scenario is None or self._scenario_cleaned:
            return
        self._scenario_cleaned = True
        scene_id = self._config.scene_id if self._config else "unknown"
        try:
            scenario.cleanup(self._world)
        except Exception as exc:
            logger.warning(f"Scenario cleanup error: {exc}")
        else:
            logger.info(f"Scenario cleanup done: {scene_id}")
        # 场景已销毁：清掉参与者快照，避免面板继续显示已经不存在的车
        self._actor_state_cache = []

    def _stop_if_running(self, timeout_s: float = _RUNNER_JOIN_TIMEOUT_S) -> bool:
        """若有场景循环线程在跑，先停掉（不修改公开状态机，供重新下发场景使用）。

        Returns:
            ``True`` 表示超时后 tick 线程仍存活（仍在持有 world），调用方不得
            在本线程继续任何 CARLA 操作。
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            return False
        self._stop_event.set()
        thread.join(timeout=timeout_s)
        if thread.is_alive():
            logger.warning(f"Scene loop did not stop within {timeout_s:.0f}s during reload")
            return True
        return False

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

    def _handle_state_change(self, snapshot: SceneStateSnapshot) -> None:
        """状态跃迁回调：先转发外部回调，再立即发布 sim_status（不等待周期）。

        注意：``SceneStateManager`` 在持有**不可重入** ``Lock`` 时调用本回调，
        因此这里必须直接用入参 ``snapshot``，绝不能回查 ``get_state_snapshot()``
        （会自死锁）。
        """
        if self._on_state_change is not None:
            self._on_state_change(snapshot)
        if self._sim_data_producer is not None:
            self._last_status_publish = time.time()
            self._publish_sim_status(snapshot)
            # 跃迁即终态：标记已上报，避免 _publish_terminal_status 重复补发
            if snapshot.status in _TERMINAL_SCENE_STATUSES:
                self._terminal_status_published = True

    def _handle_event(self, event: DetectedEvent) -> None:
        """事件回调：先转发外部回调，再并联发布 sim_event 到平台。"""
        if self._on_event is not None:
            self._on_event(event)
        self.publish_event_to_platform(event)

    def _publish_terminal_status(self) -> None:
        """终态（COMPLETED/FAILED/CANCELED）补发一次 sim_status（幂等）。"""
        if self._sim_data_producer is None or self._terminal_status_published:
            return
        snapshot = self.get_state_snapshot()
        if snapshot is None:
            return
        if snapshot.status not in _TERMINAL_SCENE_STATUSES:
            return
        self._terminal_status_published = True
        self._publish_sim_status()

    def _publish_sim_status(self, snapshot: Optional[SceneStateSnapshot] = None) -> None:
        """发布 sim_status 消息（状态跃迁 + 周期定时调用）。

        无 sim_data_producer 时安全返回。seq 由 SimDataProducer 统一下发，
        否则平台按 ``(sim_id, seq)`` 去重会把周期性状态全部丢弃。

        Args:
            snapshot: 已持有的状态快照（回调路径传入，避免重复取锁）；None 则现取。
        """
        if self._sim_data_producer is None:
            return
        try:
            from hunter_sim.cloud.sim_data_mapper import SimStatusPayload, build_sim_status  # noqa: PLC0415

            snap = snapshot if snapshot is not None else self.get_state_snapshot()
            if snap is None:
                return
            status_value = snap.status.value.upper()
            message = snap.error_message or None
            if self._status_provider is not None:
                override = self._status_provider()
                if override is not None:
                    status_value, message = override[0], override[1]
            payload = SimStatusPayload(
                status=status_value,
                progress_percent=round(snap.progress * 100.0, 2),
                elapsed_time=round(snap.elapsed_seconds, 3),
                total_time=self._config.duration_seconds if self._config else 60.0,
                actors_count=self._get_actor_count(),
                collisions=self._get_collision_count(),
                lane_invasions=self._get_lane_invasion_count(),
                ego_state=self._get_ego_state(),
                events_triggered=self._get_event_count(),
                message=message,
                updated_at=time.time(),
            )
            msg = build_sim_status(
                sim_id=self._sim_id,
                scene_id=snap.scene_id or self._scene_id,
                payload=payload,
                seq=self._sim_data_producer.next_seq(),
                vehicle_id=self._vehicle_id or None,
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
            speed = float(ego.get("velocity", 0.0))
            yaw = float(ego.get("yaw", 0.0))
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
                    "z": ego.get("z", 0.0),
                    "roll": ego.get("roll", 0.0),
                    "pitch": ego.get("pitch", 0.0),
                    # 契约字段名为 yaw；heading 作为兼容别名保留
                    "yaw": yaw,
                    "heading": yaw,
                },
                twist={
                    "vx": speed * math.cos(yaw),
                    "vy": speed * math.sin(yaw),
                    "vz": 0.0,
                    "wx": 0.0,
                    "wy": 0.0,
                    "wz": 0.0,
                },
                velocity=speed,
                steering=ego.get("steering", 0.0),
                acceleration=float(ego.get("acceleration", 0.0)),
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

    def _get_event_count(self) -> int:
        """获取已触发事件总数（方案 §5.4 sim_status.events_triggered）。"""
        if self._event_detector is None:
            return 0
        return len(self._event_detector.detected_events)

    def get_ego_state(self) -> dict[str, Any]:
        """获取自车状态字典（平台适配器/进度视图使用）。

        返回 tick 线程缓存的最近一次快照，避免 executor 线程直接调用 CARLA
        RPC（非线程安全），从而消除因跨线程读取导致的零值问题。

        V3.5.6：缓存尚未写入时返回 **空字典**，不再退回 ``_get_ego_state()``。
        那个兜底是原生崩溃源头：首个成功 tick 之前缓存为空（现场日志的
        ``Ego state unavailable (stage=actor_missing)`` 即该窗口），executor 线程
        的 0.5s 进度轮询会直接落到 ``world.get_actors()`` / ``get_transform()``，
        与 tick 线程的 ``spawn_actor`` / ``actor.destroy()`` 并发操作同一个
        Client 连接，表现为进程无 traceback 退出（0xC00000FD / 0xC0000409）。
        进度字段允许缺席，不允许用崩溃换读数。
        """
        if self._ego_state_cache:
            return dict(self._ego_state_cache)
        return {}

    def _get_ego_state(self) -> dict[str, Any]:
        """获取自车状态字典（仅由 tick 线程调用）。

        自车引用统一走 ``ego_vehicle`` property（多形态访问器 + 世界兜底），
        避免两处解析规则漂移。

        取不到自车或读取失败时返回 **空字典**（不是零值字典）：零值会被进度
        落库当作真实位姿写入实例记录，可视化面板与上行遥测就整场显示 0.000，
        把「自车引用解析失败」掩盖成「车没动」。同时按窗口限频打 WARNING，
        避免 50Hz 下形成日志风暴。

        成功读取时同步更新 ``_ego_state_cache``，供 executor 线程无锁读取。
        """
        try:
            vehicle = self.ego_vehicle
        except Exception as exc:  # noqa: BLE001  解析异常与读取异常分开定位
            self._warn_ego_state("resolve", exc)
            return {}
        if vehicle is None:
            self._warn_ego_state("actor_missing", None)
            return {}
        try:
            # CARLA Actor 没有 get_rotation()：位姿（位置 + 姿态）统一从
            # get_transform() 读取。V3.4.3 误用 get_rotation() 使整个读取块
            # 每帧抛 AttributeError，面板整场显示「—」（旧版则写零值）。
            pose = vehicle.get_transform()
            loc = pose.location
            rot = pose.rotation
            vel = vehicle.get_velocity()
            speed = math.sqrt(vel.x ** 2 + vel.y ** 2 + vel.z ** 2)
            ctrl = vehicle.get_control()
            yaw = round(math.radians(float(getattr(rot, "yaw", 0.0))), 6)
            state = {
                "x": round(loc.x, 4),
                "y": round(loc.y, 4),
                "z": round(loc.z, 4),
                "heading": yaw,
                "yaw": yaw,
                "velocity": round(speed, 4),
                "steering": round(float(getattr(ctrl, "steer", 0.0)), 6),
                "acceleration": round(self._acceleration_magnitude(vehicle), 4),
            }
        except Exception as exc:  # noqa: BLE001  单帧读取失败不阻断场景
            self._warn_ego_state("read", exc)
            return {}
        self._ego_state_cache = state  # tick 线程写入缓存，executor 线程安全读取
        return state

    def _warn_ego_state(self, stage: str, exc: Optional[BaseException]) -> None:
        """自车状态取数失败告警（按窗口限频，串行输出被抑制条数）。"""
        now = time.monotonic()
        if self._ego_state_warn_at and now - self._ego_state_warn_at < _EGO_STATE_WARN_INTERVAL_S:
            self._ego_state_warn_count += 1
            return
        suppressed = self._ego_state_warn_count
        self._ego_state_warn_at = now
        self._ego_state_warn_count = 0
        detail = "" if exc is None else f": {type(exc).__name__}: {exc}"
        suffix = f" (+{suppressed} suppressed)" if suppressed else ""
        logger.warning(f"Ego state unavailable (stage={stage}){detail}{suffix}")

    @staticmethod
    def _acceleration_magnitude(vehicle: Any) -> float:
        """读取加速度向量模长（不支持时回退 0）。"""
        try:
            accel = vehicle.get_acceleration()
            return float(math.sqrt(accel.x ** 2 + accel.y ** 2 + accel.z ** 2))
        except Exception:  # noqa: BLE001
            return 0.0
