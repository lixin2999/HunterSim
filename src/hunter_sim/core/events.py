"""事件总线核心事件类型定义（见开发提示词 §5.3）。

事件对象跨层传递，使用不可变 ``pydantic`` 模型。``Event`` 为所有事件的基类，
携带单调递增序号与仿真时间戳，便于订阅方按序处理与时间对齐。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Event(BaseModel):
    """所有事件的基类（不可变）。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    sequence: int = Field(ge=0, description="全局单调递增事件序号")
    timestamp: float = Field(description="事件发生的仿真时间戳（秒）")


class SimulationTickEvent(Event):
    """每帧仿真 tick 完成时发布。"""

    tick: int = Field(ge=0, description="累计 tick 数")
    delta_seconds: float = Field(description="本帧距上一帧的时间间隔（秒）")


class SensorDataEvent(Event):
    """某传感器产生一帧新数据时发布。"""

    sensor_id: str
    sensor_type: str = Field(description="传感器类型，如 camera.rgb / lidar.ray_cast")
    frame_id: int = Field(ge=0)


class ScenarioStartedEvent(Event):
    """场景开始运行时发布。"""

    scenario_name: str
    map_name: str
    run_id: str


class ScenarioEndedEvent(Event):
    """场景结束时发布。"""

    scenario_name: str
    run_id: str
    status: str = Field(description="终止状态: completed / failed / aborted")
    total_frames: int = Field(ge=0, default=0)


class CollisionEvent(Event):
    """发生碰撞时发布。"""

    other_actor_id: int = Field(description="碰撞对象 Actor id")
    impulse: float = Field(description="碰撞冲量大小（牛顿）")
    location_x: float
    location_y: float
    location_z: float


class LaneInvasionEvent(Event):
    """压线 / 越线时发布。"""

    lane_type: str = Field(description="被侵犯的车道线类型，如 Solid / Dashed")


__all__ = [
    "CollisionEvent",
    "Event",
    "LaneInvasionEvent",
    "ScenarioEndedEvent",
    "ScenarioStartedEvent",
    "SensorDataEvent",
    "SimulationTickEvent",
]
