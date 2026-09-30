"""行人控制器封装（PROMPT-ENG-005-B）。

封装 CARLA controller.ai.walker 蓝图，提供行人目的地、速度和行为控制接口。
最大步速 1.4 m/s（符合行人行为规格）。
"""

from __future__ import annotations

import math
from typing import Any, Optional

from hunter_sim.common.exceptions import CarlaSimulationError
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

_WALKER_MAX_SPEED_MS = 1.4  # 行人最大步速


class WalkerControllerWrapper:
    """CARLA 行人 AI 控制器封装。

    Args:
        walker_actor: CARLA walker Actor。
        controller_actor: CARLA controller.ai.walker Actor（attach_to walker）。
    """

    def __init__(self, walker_actor: Any, controller_actor: Any) -> None:
        self._walker = walker_actor
        self._controller = controller_actor
        self._max_speed = _WALKER_MAX_SPEED_MS

    def start_navigation(self, destination: Any, speed_ms: float = 1.2) -> None:
        """启动行人向目标点行走（文档 §7.5：start → go_to_location → set_max_speed）。

        Args:
            destination: carla.Location 目标位置。
            speed_ms: 行走速度，不超过 1.4 m/s。
        """
        speed = min(self._max_speed, max(0.1, speed_ms))
        try:
            self._controller.start()
            self._controller.set_max_speed(speed)
            self._controller.go_to_location(destination)
            logger.debug(f"Walker navigation started, speed={speed:.2f} m/s")
        except Exception as exc:
            raise CarlaSimulationError("walker_nav_start", str(exc)) from exc

    def stop_navigation(self) -> None:
        """停止行人移动。"""
        try:
            self._controller.stop()
        except Exception as exc:
            logger.warning(f"Walker stop error: {exc}")

    def set_destination(self, destination: Any, speed_ms: float = 1.2) -> None:
        """更新行人目的地（不重启导航）。"""
        speed = min(self._max_speed, max(0.1, speed_ms))
        try:
            self._controller.set_max_speed(speed)
            self._controller.go_to_location(destination)
        except Exception as exc:
            logger.warning(f"Walker set destination error: {exc}")

    def get_current_location(self) -> Optional[Any]:
        """返回行人当前位置（carla.Location）。"""
        try:
            return self._walker.get_location()
        except Exception:
            return None

    def destroy(self) -> None:
        """销毁行人和控制器 Actor。"""
        for actor in (self._controller, self._walker):
            try:
                actor.destroy()
            except Exception:
                pass
        logger.debug("Walker controller destroyed")
