"""场景运行主服务（PROMPT-ENG-003-B）。

SceneRunnerService 管理场景完整生命周期：
创建 → 加载 → 就绪 → 运行（支持暂停/恢复）→ 完成/失败 → 销毁。
支持两种运行模式：ScenarioRunner（OpenSCENARIO）和自定义 Python 脚本。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Optional

from hunter_sim.common.exceptions import (
    CarlaSimulationError,
    ConfigurationError,
    InstanceStateError,
    SimTimeoutError,
)
from hunter_sim.common.models import SceneStatus, SimMode
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.weather_manager import WeatherManager
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
    """

    def __init__(
        self,
        world: Any,
        on_state_change: Optional[Callable[[SceneStateSnapshot], None]] = None,
        on_event: Optional[Callable[[DetectedEvent], None]] = None,
    ) -> None:
        self._world = world
        self._on_state_change = on_state_change
        self._on_event = on_event

        # 天气管理器：API 层天气控制（scenes.set_weather_impl）与渐变推进均使用此实例
        self._weather_manager = WeatherManager(world)

        self._lock: threading.RLock = threading.RLock()
        self._state_mgr: Optional[SceneStateManager] = None
        self._event_detector: Optional[EventDetector] = None
        self._scenario: Optional[CustomScenarioBase] = None
        self._config: Optional[SceneConfig] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()

    @property
    def weather_manager(self) -> WeatherManager:
        """天气管理器（供 API 层天气控制与渐变过渡）。"""
        return self._weather_manager

    @property
    def current_scene_id(self) -> Optional[str]:
        """当前场景 ID（无场景时返回 None）。"""
        with self._lock:
            return self._config.scene_id if self._config else None

    def get_state_snapshot(self) -> Optional[SceneStateSnapshot]:
        """获取当前场景状态快照。"""
        with self._lock:
            return self._state_mgr.get_snapshot() if self._state_mgr else None

    def load_scene(self, config: SceneConfig, scenario: Optional[CustomScenarioBase] = None) -> None:
        """加载场景配置，初始化状态机和事件检测器。

        状态转换：CREATED → LOADING → READY。

        Args:
            config: 已验证的场景配置。
            scenario: 自定义 Python 场景实例（可选，None 表示使用内置执行器）。

        Raises:
            ConfigurationError: 配置无效。
            CarlaSimulationError: 地图或 Actor 初始化失败。
        """
        with self._lock:
            logger.info(f"Loading scene: {config.scene_id} ({config.mode.value} mode)")

            self._config = config
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

            self._scenario = scenario

            if scenario is not None:
                try:
                    scenario.setup(self._world)
                except Exception as exc:
                    self._state_mgr.transition_to(SceneStatus.FAILED, str(exc))
                    raise CarlaSimulationError("scene_setup", str(exc)) from exc

            self._state_mgr.transition_to(SceneStatus.READY)
            logger.info(f"Scene '{config.scene_id}' ready to start")

    def start(self) -> None:
        """启动场景运行（非阻塞，在独立线程中执行仿真循环）。

        状态转换：READY → RUNNING。

        Raises:
            InstanceStateError: 当前状态不允许启动。
        """
        with self._lock:
            if self._state_mgr is None:
                raise ConfigurationError("start", "No scene loaded")
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
                    if self._scenario.is_timeout():
                        with self._lock:
                            if self._state_mgr and self._state_mgr.status == SceneStatus.RUNNING:
                                self._state_mgr.transition_to(SceneStatus.FAILED, "Duration timeout")
                        break

                    timestamp = tick_count * delta_seconds
                    self._scenario.tick(self._world, timestamp, delta_seconds)

                    # 检查场景完成
                    if self._scenario.check_finish(self._world, timestamp):
                        with self._lock:
                            if self._state_mgr and self._state_mgr.status == SceneStatus.RUNNING:
                                self._state_mgr.transition_to(SceneStatus.COMPLETED)
                        break

                # 推进仿真
                self._world.tick()
                # 推进天气渐变过渡（文档 §3.4.1：每 tick 推进一步）
                self._weather_manager.update_transition()
                tick_count += 1

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
        """清理场景资源。"""
        try:
            if self._scenario is not None:
                self._scenario.cleanup(self._world)
        except Exception as exc:
            logger.warning(f"Scenario cleanup error: {exc}")
