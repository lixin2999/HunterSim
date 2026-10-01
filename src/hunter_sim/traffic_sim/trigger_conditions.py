"""触发条件定义（PROMPT-ENG-005-B）。

5 种触发条件：时间触发、距离触发、位置触发、速度触发、事件触发。
统一接口：check() -> bool
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any

from hunter_sim.common.utils import euclidean_distance_2d, get_logger

logger = get_logger(__name__)


class TriggerCondition(ABC):
    """触发条件抽象基类。"""

    @abstractmethod
    def check(self, elapsed_time: float, actor: Any, ego_vehicle: Any) -> bool:
        """检查触发条件是否满足。"""
        ...

    def reset(self) -> None:
        """重置触发器状态。"""
        pass


class TimeTrigger(TriggerCondition):
    """时间触发：场景运行到指定时间（秒）后激活行为。"""

    def __init__(self, trigger_time_s: float) -> None:
        self._time = trigger_time_s
        self._fired = False

    def check(self, elapsed_time: float, actor: Any, ego_vehicle: Any) -> bool:
        if not self._fired and elapsed_time >= self._time:
            self._fired = True
            logger.debug(f"TimeTrigger fired at t={elapsed_time:.2f}s")
        return self._fired

    def reset(self) -> None:
        self._fired = False


class DistanceTrigger(TriggerCondition):
    """距离触发：与自车距离小于阈值时激活。"""

    def __init__(self, distance_m: float) -> None:
        self._dist = distance_m

    def check(self, elapsed_time: float, actor: Any, ego_vehicle: Any) -> bool:
        if ego_vehicle is None:
            return False
        try:
            ax, ay = actor.get_location().x, actor.get_location().y
            ex, ey = ego_vehicle.get_location().x, ego_vehicle.get_location().y
            dist = euclidean_distance_2d(ax, ay, ex, ey)
            return dist <= self._dist
        except Exception:
            return False


class PositionTrigger(TriggerCondition):
    """位置触发：进入指定地图坐标范围时激活。"""

    def __init__(self, x: float, y: float, radius_m: float = 5.0) -> None:
        self._x = x
        self._y = y
        self._r = radius_m

    def check(self, elapsed_time: float, actor: Any, ego_vehicle: Any) -> bool:
        try:
            loc = actor.get_location()
            return euclidean_distance_2d(loc.x, loc.y, self._x, self._y) <= self._r
        except Exception:
            return False


class VelocityTrigger(TriggerCondition):
    """速度触发：自车或目标速度超过阈值时激活。"""

    def __init__(self, speed_threshold_ms: float, use_ego: bool = True) -> None:
        self._threshold = speed_threshold_ms
        self._use_ego = use_ego

    def check(self, elapsed_time: float, actor: Any, ego_vehicle: Any) -> bool:
        target = ego_vehicle if self._use_ego else actor
        try:
            vel = target.get_velocity()
            speed = math.sqrt(vel.x ** 2 + vel.y ** 2)
            return speed >= self._threshold
        except Exception:
            return False


class EventTrigger(TriggerCondition):
    """事件触发：由外部事件系统设置标志位激活（上一事件完成后触发）。"""

    def __init__(self, event_name: str) -> None:
        self._event_name = event_name
        self._triggered = False

    def fire(self) -> None:
        """外部调用此方法激活触发器。"""
        self._triggered = True
        logger.debug(f"EventTrigger fired: {self._event_name}")

    def check(self, elapsed_time: float, actor: Any, ego_vehicle: Any) -> bool:
        return self._triggered

    def reset(self) -> None:
        self._triggered = False


def create_trigger_from_config(config: dict[str, Any]) -> TriggerCondition:
    """从触发配置字典创建触发条件实例（设计文档 §7.4.2 五种触发条件）。

    Args:
        config: 触发配置，必含 'type' 字段：
            - time: {'type','value'} 场景运行到指定时间（秒）
            - distance: {'type','value'} 与自车距离小于阈值（米）
            - position: {'type','x','y','radius'?} 到达指定坐标范围
            - velocity: {'type','value','use_ego'?} 速度达到阈值（m/s）
            - event: {'type','event_name'} 上一事件完成后触发

    Returns:
        TriggerCondition 实例。

    Raises:
        ValueError: 未知触发类型。
    """
    ttype = str(config.get("type", ""))
    value = float(config.get("value", 0.0))
    if ttype == "time":
        return TimeTrigger(trigger_time_s=value)
    if ttype == "distance":
        return DistanceTrigger(distance_m=value)
    if ttype == "position":
        return PositionTrigger(
            x=float(config.get("x", 0.0)),
            y=float(config.get("y", 0.0)),
            radius_m=float(config.get("radius", 5.0)),
        )
    if ttype == "velocity":
        return VelocityTrigger(
            speed_threshold_ms=value,
            use_ego=bool(config.get("use_ego", True)),
        )
    if ttype == "event":
        return EventTrigger(event_name=str(config.get("event_name", "")))
    raise ValueError(f"Unknown trigger type: '{ttype}'")
