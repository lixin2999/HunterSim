"""自定义场景基类（PROMPT-ENG-003-B）。

用户继承 CustomScenarioBase 实现自定义 Python 场景逻辑。
框架在每个仿真 tick 调用 tick()，并检查 check_finish() 决定是否结束。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from hunter_sim.common.models import SceneStatus
from hunter_sim.common.utils import get_logger
from hunter_sim.scene_runner.event_detector import EventDetector
from hunter_sim.scene_runner.scene_config import SceneConfig
from hunter_sim.scene_runner.scene_state_manager import SceneStateManager

logger = get_logger(__name__)


class CustomScenarioBase(ABC):
    """自定义 Python 场景基类。

    子类需实现：
    - ``setup(world)`` - 初始化场景（生成 Actor、注册回调等）
    - ``tick(world, timestamp)`` - 每帧逻辑
    - ``check_finish(world, timestamp)`` - 返回 True 时场景结束
    - ``cleanup(world)`` - 场景结束后清理资源

    Args:
        config: 场景配置。
        state_manager: 状态机管理器。
        event_detector: 事件检测器。
    """

    def __init__(
        self,
        config: SceneConfig,
        state_manager: SceneStateManager,
        event_detector: EventDetector,
    ) -> None:
        self._config = config
        self._state_mgr = state_manager
        self._event_detector = event_detector
        self._start_time: float = 0.0
        self._elapsed: float = 0.0

    @property
    def config(self) -> SceneConfig:
        """场景配置（只读）。"""
        return self._config

    @property
    def elapsed_seconds(self) -> float:
        """场景已运行时间（秒）。"""
        return self._elapsed

    @abstractmethod
    def setup(self, world: Any) -> None:
        """初始化场景。

        在状态转为 RUNNING 之前调用。生成 Actor、设置天气、注册传感器回调等。

        Args:
            world: CARLA World 对象。
        """
        ...

    @abstractmethod
    def tick(self, world: Any, timestamp: float, delta_seconds: float) -> None:
        """每仿真帧调用一次。

        Args:
            world: CARLA World 对象。
            timestamp: 当前仿真时间戳（秒）。
            delta_seconds: 帧间隔（秒）。
        """
        ...

    @abstractmethod
    def check_finish(self, world: Any, timestamp: float) -> bool:
        """检查场景是否结束。

        Args:
            world: CARLA World 对象。
            timestamp: 当前仿真时间戳。

        Returns:
            True 表示场景已完成，框架将转为 COMPLETED 状态。
        """
        ...

    @abstractmethod
    def cleanup(self, world: Any) -> None:
        """场景结束/失败后清理资源。

        必须销毁所有已生成的 Actor，注销传感器回调。

        Args:
            world: CARLA World 对象。
        """
        ...

    def _update_elapsed(self) -> None:
        """内部方法：更新已运行时间。"""
        if self._start_time == 0.0:
            self._start_time = time.perf_counter()
        self._elapsed = time.perf_counter() - self._start_time

    def is_timeout(self) -> bool:
        """检查场景是否超时（超出配置的 duration_seconds）。"""
        return self._elapsed > self._config.duration_seconds
