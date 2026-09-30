"""场景事件检测器（PROMPT-ENG-003-B）。

支持内置事件（碰撞、车道偏离、障碍物）和自定义事件（超速、急刹车、闯红灯等）。
事件通过 CARLA sensor 回调或每 tick 检查产生。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from hunter_sim.common.models import VehicleState
from hunter_sim.common.utils import get_logger
from hunter_sim.scene_runner.scene_config import SceneEventDefinition, SceneEventType

logger = get_logger(__name__)


@dataclass
class DetectedEvent:
    """运行时检测到的场景事件。

    Attributes:
        event_id: 关联的事件定义 ID。
        event_type: 事件类型字符串。
        time_stamp: 事件发生仿真时间戳。
        description: 事件描述。
        data: 附加数据字典（碰撞对象 ID、速度值等）。
    """

    event_id: str
    event_type: str
    time_stamp: float
    description: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（供 WebSocket 推送）。"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "time_stamp": self.time_stamp,
            "description": self.description,
            "data": self.data,
        }


class EventDetector:
    """场景事件检测器。

    管理事件定义列表，提供 CARLA sensor 回调注册接口，
    以及每 tick 检查的自定义事件判定逻辑。

    Args:
        event_definitions: 场景配置中的事件定义列表。
        on_event: 事件触发回调函数。
    """

    def __init__(
        self,
        event_definitions: list[SceneEventDefinition],
        on_event: Optional[Callable[[DetectedEvent], None]] = None,
    ) -> None:
        self._definitions: dict[str, SceneEventDefinition] = {
            d.event_id: d for d in event_definitions
        }
        self._on_event = on_event
        self._lock: threading.Lock = threading.Lock()
        self._detected: list[DetectedEvent] = []
        self._prev_speed: float = 0.0
        self._prev_accel: float = 0.0
        self._collision_count: int = 0

    @property
    def detected_events(self) -> list[DetectedEvent]:
        """已检测事件列表（只读快照）。"""
        with self._lock:
            return list(self._detected)

    @property
    def failure_events(self) -> list[DetectedEvent]:
        """失败级别事件列表。"""
        with self._lock:
            return [
                e for e in self._detected
                if self._definitions.get(e.event_id) is not None
                and self._definitions[e.event_id].is_failure_condition
            ]

    def on_collision(self, timestamp: float, other_actor_id: int, impulse: float) -> None:
        """碰撞事件回调（由 CARLA collision sensor 触发）。

        Args:
            timestamp: 碰撞仿真时间戳。
            other_actor_id: 碰撞对象 Actor ID。
            impulse: 碰撞冲量（N·s）。
        """
        event = DetectedEvent(
            event_id="collision_detected",
            event_type=SceneEventType.COLLISION.value,
            time_stamp=timestamp,
            description=f"Collision with actor {other_actor_id}, impulse={impulse:.1f} N·s",
            data={"other_actor_id": other_actor_id, "impulse": impulse},
        )
        self._emit(event)
        self._collision_count += 1

    def on_lane_invasion(self, timestamp: float, lane_type: str) -> None:
        """车道偏离回调（由 CARLA lane_invasion sensor 触发）。

        Args:
            timestamp: 偏离仿真时间戳。
            lane_type: 越线的车道类型。
        """
        event = DetectedEvent(
            event_id="lane_invasion_detected",
            event_type=SceneEventType.LANE_INVASION.value,
            time_stamp=timestamp,
            description=f"Lane invasion: {lane_type}",
            data={"lane_type": lane_type},
        )
        self._emit(event)

    def check_speed_violation(
        self, timestamp: float, speed_ms: float, limit_ms: float
    ) -> None:
        """检查超速事件（每 tick 调用）。

        Args:
            timestamp: 当前仿真时间戳。
            speed_ms: 当前车速（m/s）。
            limit_ms: 限速值（m/s）。
        """
        if speed_ms > limit_ms * 1.1:  # 超过限速 10% 才触发
            event = DetectedEvent(
                event_id="speed_violation",
                event_type=SceneEventType.SPEED_VIOLATION.value,
                time_stamp=timestamp,
                description=f"Speed violation: {speed_ms:.1f} m/s > limit {limit_ms:.1f} m/s",
                data={"speed_ms": speed_ms, "limit_ms": limit_ms},
            )
            self._emit(event)

    def check_emergency_brake(
        self, timestamp: float, acceleration_ms2: float
    ) -> None:
        """检查紧急制动（减速度 > 3 m/s²，每 tick 调用）。

        Args:
            timestamp: 当前仿真时间戳。
            acceleration_ms2: 纵向加速度（m/s²，负值=制动）。
        """
        if acceleration_ms2 < -3.0:
            event = DetectedEvent(
                event_id="emergency_brake",
                event_type=SceneEventType.EMERGENCY_BRAKE.value,
                time_stamp=timestamp,
                description=f"Emergency brake: decel={abs(acceleration_ms2):.2f} m/s²",
                data={"deceleration": abs(acceleration_ms2)},
            )
            self._emit(event)

    def check_tick(
        self,
        timestamp: float,
        vehicle_state: VehicleState,
        speed_limit_ms: float = 10.0,
    ) -> None:
        """每 tick 统一调用，执行所有自定义事件检查。

        Args:
            timestamp: 当前仿真时间戳。
            vehicle_state: 当前车辆状态。
            speed_limit_ms: 当前路段限速（m/s）。
        """
        self.check_speed_violation(timestamp, vehicle_state.vehicle_speed, speed_limit_ms)
        accel = vehicle_state.acceleration
        forward_accel = math.sqrt(accel[0] ** 2 + accel[1] ** 2)
        # 只在制动时传入负值
        if vehicle_state.brake > 0.5:
            self.check_emergency_brake(timestamp, -forward_accel)
        self._prev_speed = vehicle_state.vehicle_speed

    def _emit(self, event: DetectedEvent) -> None:
        """记录事件并触发回调。"""
        with self._lock:
            self._detected.append(event)
        logger.info(f"Event detected: [{event.event_type}] {event.description}")
        if self._on_event:
            try:
                self._on_event(event)
            except Exception as exc:
                logger.warning(f"Event callback error: {exc}")

    def reset(self) -> None:
        """清空已检测事件列表（场景重启时调用）。"""
        with self._lock:
            self._detected.clear()
        self._collision_count = 0
        logger.debug("EventDetector reset")
