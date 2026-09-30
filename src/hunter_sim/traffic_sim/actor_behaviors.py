"""交通参与者行为控制模块（PROMPT-ENG-005-B）。

定义行为基类和具体行为实现，供场景化参与者控制器使用。
行为统一接口：update(elapsed_time, ego_vehicle) -> Action
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


@dataclass
class ActorAction:
    """单帧行为输出指令。

    Attributes:
        target_speed_ms: 目标速度（m/s）。
        steering: CARLA steer 值 (-1~1)。
        brake: CARLA brake 值 (0~1)。
        throttle: CARLA throttle 值 (0~1)。
        is_stop: 是否完全停止。
    """

    target_speed_ms: float = 0.0
    steering: float = 0.0
    brake: float = 0.0
    throttle: float = 0.0
    is_stop: bool = False


class ActorBehavior(ABC):
    """交通参与者行为抽象基类。

    Args:
        behavior_id: 行为唯一标识。
        priority: 行为优先级（高优先级覆盖低优先级）。
    """

    def __init__(self, behavior_id: str = "", priority: int = 0) -> None:
        self.behavior_id = behavior_id
        self.priority = priority
        self._active: bool = False
        self._start_time: float = 0.0

    @abstractmethod
    def update(
        self,
        elapsed_time: float,
        actor: Any,
        ego_vehicle: Any,
        delta_seconds: float,
    ) -> ActorAction:
        """每 tick 调用，返回本帧行为指令。"""
        ...

    def start(self) -> None:
        """激活行为。"""
        self._active = True
        self._start_time = 0.0

    def stop(self) -> None:
        """停用行为。"""
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active


class ConstantSpeedBehavior(ActorBehavior):
    """匀速直行行为。

    Args:
        target_speed_ms: 维持的目标速度（m/s）。
    """

    def __init__(self, target_speed_ms: float = 5.0, behavior_id: str = "const_speed") -> None:
        super().__init__(behavior_id)
        self._speed = target_speed_ms

    def update(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        speed = self._get_current_speed(actor)
        throttle = min(1.0, max(0.0, (self._speed - speed) * 0.5))
        return ActorAction(target_speed_ms=self._speed, throttle=throttle)

    @staticmethod
    def _get_current_speed(actor: Any) -> float:
        try:
            vel = actor.get_velocity()
            return math.sqrt(vel.x ** 2 + vel.y ** 2 + vel.z ** 2)
        except Exception:
            return 0.0


class DecelerateBehavior(ActorBehavior):
    """减速行为（匀减速直到停止或达到目标速度）。

    Args:
        initial_speed_ms: 初始速度（m/s）。
        deceleration_ms2: 减速度大小（m/s²，正值）。
        target_speed_ms: 最终目标速度（停止时为 0）。
    """

    def __init__(
        self,
        initial_speed_ms: float = 8.0,
        deceleration_ms2: float = 2.0,
        target_speed_ms: float = 0.0,
        behavior_id: str = "decelerate",
    ) -> None:
        super().__init__(behavior_id)
        self._initial = initial_speed_ms
        self._decel = deceleration_ms2
        self._target = target_speed_ms

    def update(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        current = self._initial - self._decel * elapsed_time
        speed = max(self._target, current)
        brake = 0.0 if current <= self._target else min(1.0, self._decel / 5.0)
        return ActorAction(
            target_speed_ms=speed,
            brake=brake,
            is_stop=(speed <= 0.01),
        )


class CutInBehavior(ActorBehavior):
    """切入行为（相邻车道车辆向自车方向变道）。

    Args:
        speed_ms: 切入时的目标速度。
        lateral_offset_m: 横向切入位移量（米）。
        duration_s: 切入动作持续时长（秒）。
    """

    def __init__(
        self,
        speed_ms: float = 6.0,
        lateral_offset_m: float = 3.0,
        duration_s: float = 4.0,
        behavior_id: str = "cut_in",
    ) -> None:
        super().__init__(behavior_id)
        self._speed = speed_ms
        self._lateral = lateral_offset_m
        self._duration = duration_s

    def update(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        progress = min(1.0, elapsed_time / self._duration)
        # S-curve 平滑横向偏移
        steer = math.sin(progress * math.pi) * 0.6
        throttle = min(1.0, (self._speed / 10.0))
        return ActorAction(
            target_speed_ms=self._speed,
            steering=steer,
            throttle=throttle,
        )


class StaticBehavior(ActorBehavior):
    """静止障碍物行为。"""

    def update(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        return ActorAction(brake=1.0, is_stop=True)


class PedestrianCrossBehavior(ActorBehavior):
    """行人横穿行为。

    Args:
        speed_ms: 行人步速（实车规格最大 1.4 m/s）。
        crossing_duration_s: 横穿时间（秒）。
    """

    def __init__(
        self,
        speed_ms: float = 1.2,
        crossing_duration_s: float = 15.0,
        behavior_id: str = "ped_cross",
    ) -> None:
        super().__init__(behavior_id)
        self._speed = min(1.4, speed_ms)  # 行人最大速度 1.4 m/s
        self._duration = crossing_duration_s

    def update(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        done = elapsed_time >= self._duration
        return ActorAction(
            target_speed_ms=0.0 if done else self._speed,
            is_stop=done,
        )
