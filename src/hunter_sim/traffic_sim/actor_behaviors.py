"""交通参与者行为控制模块（PROMPT-ENG-005-B）。

定义行为基类和具体行为实现，供场景化参与者控制器使用。
行为统一接口：update(elapsed_time, ego_vehicle) -> Action
支持行为类型（设计文档 §7.4.1）：constant_speed / decelerate / cut_in 等。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
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

    def start(self, now: float = 0.0) -> None:
        """激活行为。

        Args:
            now: 激活时刻的仿真绝对时间（秒），记录到 `_start_time` 供需要时回查。
        """
        self._active = True
        self._start_time = now

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
        trigger_time_s: 减速触发时刻（文档 §7.4.1：elapsed_time > trigger_time 后才减速）。
    """

    def __init__(
        self,
        initial_speed_ms: float = 8.0,
        deceleration_ms2: float = 2.0,
        target_speed_ms: float = 0.0,
        trigger_time_s: float = 0.0,
        behavior_id: str = "decelerate",
    ) -> None:
        super().__init__(behavior_id)
        self._initial = initial_speed_ms
        self._decel = deceleration_ms2
        self._target = target_speed_ms
        self._trigger_time = trigger_time_s

    def update(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        if elapsed_time < self._trigger_time:
            # 触发前保持初速匀速行驶
            return ActorAction(target_speed_ms=self._initial)
        decel_elapsed = elapsed_time - self._trigger_time
        current = self._initial - self._decel * decel_elapsed
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

# ─── 行为工厂与场景化控制器（设计文档 §7.4） ───────────────────────────────


def create_behavior_from_config(config: dict[str, Any]) -> ActorBehavior:
    """从行为配置字典创建行为实例（文档 §7.4.1 type 字段）。

    Args:
        config: 行为配置，必含 'type' 字段：
            - constant_speed: {'type','speed'}
            - decelerate: {'type','trigger_time','deceleration','initial_speed'?,'target_speed'?}
            - cut_in: {'type','speed','duration'?}
            - static / pedestrian_cross 亦支持。

    Returns:
        ActorBehavior 实例。

    Raises:
        ValueError: 未知行为类型。
    """
    btype = str(config.get("type", ""))
    if btype == "constant_speed":
        return ConstantSpeedBehavior(target_speed_ms=float(config.get("speed", 5.0)))
    if btype == "decelerate":
        return DecelerateBehavior(
            initial_speed_ms=float(config.get("initial_speed", 8.0)),
            deceleration_ms2=float(config.get("deceleration", 2.0)),
            target_speed_ms=float(config.get("target_speed", 0.0)),
            trigger_time_s=float(config.get("trigger_time", 0.0)),
        )
    if btype == "cut_in":
        return CutInBehavior(
            speed_ms=float(config.get("speed", 6.0)),
            duration_s=float(config.get("duration", 4.0)),
        )
    if btype == "static":
        return StaticBehavior()
    if btype == "pedestrian_cross":
        return PedestrianCrossBehavior(
            speed_ms=float(config.get("speed", 1.2)),
            crossing_duration_s=float(config.get("duration", 15.0)),
        )
    raise ValueError(f"Unknown actor behavior type: '{btype}'")


class ScriptedActorController:
    """场景化参与者控制器：触发条件 + 行为组合执行（文档 §7.4）。

    触发条件满足前参与者保持静止（或默认行为）；
    触发后每 tick 执行绑定行为并输出指令。

    Args:
        actor: CARLA Actor 对象（Any）。
        behavior: 行为实例。
        trigger: 触发条件实例（None 表示立即激活）。
    """

    def __init__(
        self,
        actor: Any,
        behavior: ActorBehavior,
        trigger: Optional[Any] = None,
    ) -> None:
        self._actor = actor
        self._behavior = behavior
        self._trigger = trigger
        self._triggered = trigger is None
        # 触发时刻基准：行为接收到的 elapsed_time 为触发后的相对时间，
        # 避免触发发生在 t>0 时行为进度跳变（如 cut_in 首帧即 progress=1.0）
        self._trigger_elapsed: float = 0.0
        self._elapsed: float = 0.0

    @property
    def is_triggered(self) -> bool:
        """触发条件是否已满足。"""
        return self._triggered

    @property
    def behavior(self) -> ActorBehavior:
        """绑定的行为实例。"""
        return self._behavior

    def update(self, ego_vehicle: Any, delta_seconds: float) -> Optional[ActorAction]:
        """每 tick 更新：满足触发后执行行为。

        Args:
            ego_vehicle: 自车 Actor（用于触发判断与行为上下文）。
            delta_seconds: 仿真步长（秒）。

        Returns:
            本帧行为指令；未触发时返回 None。
        """
        self._elapsed += delta_seconds
        if not self._triggered and self._trigger is not None:
            if self._trigger.check(self._elapsed, self._actor, ego_vehicle):
                self._triggered = True
                self._trigger_elapsed = self._elapsed
                self._behavior.start(self._elapsed)
                logger.debug(f"ScriptedActorController triggered: {self._behavior.behavior_id}")
        if not self._triggered:
            return None
        return self._behavior.update(
            self._elapsed - self._trigger_elapsed, self._actor, ego_vehicle, delta_seconds
        )

    def reset(self) -> None:
        """重置控制器与触发器状态（场景重启时调用）。"""
        self._elapsed = 0.0
        self._trigger_elapsed = 0.0
        self._triggered = self._trigger is None
        if self._trigger is not None:
            self._trigger.reset()
        self._behavior.stop()
