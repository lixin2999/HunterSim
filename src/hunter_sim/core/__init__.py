"""核心层：跨模块契约、事件、配置、日志与异常。

本包是五层架构的公共基础，所有上层依赖此处的不可变数据对象与接口契约。
"""

from __future__ import annotations

from hunter_sim.core.config import (
    ConnectionConfig,
    ScenarioConfig,
    SensorConfig,
    load_scenario_config,
)
from hunter_sim.core.contracts import (
    CameraFrame,
    GnssFrame,
    ImuFrame,
    LidarFrame,
    RadarFrame,
    RunMetadata,
    SynchronizedFrame,
    TimestampedData,
    VehicleState,
)
from hunter_sim.core.event_bus import InMemoryEventBus
from hunter_sim.core.events import (
    CollisionEvent,
    Event,
    LaneInvasionEvent,
    ScenarioEndedEvent,
    ScenarioStartedEvent,
    SensorDataEvent,
    SimulationTickEvent,
)
from hunter_sim.core.exceptions import (
    BufferClosedError,
    CarlaConnectionError,
    ConfigurationError,
    DataWriteError,
    HunterSimError,
    ResourceError,
    SensorSimulationError,
    SimulationError,
    ValidationError,
)
from hunter_sim.core.logging import configure_logging, logger, run_context
from hunter_sim.core.protocols import EventBus

__all__ = [
    "BufferClosedError",
    "CameraFrame",
    "CarlaConnectionError",
    "CollisionEvent",
    "ConfigurationError",
    "ConnectionConfig",
    "DataWriteError",
    "Event",
    "EventBus",
    "GnssFrame",
    "HunterSimError",
    "ImuFrame",
    "InMemoryEventBus",
    "LaneInvasionEvent",
    "LidarFrame",
    "RadarFrame",
    "ResourceError",
    "RunMetadata",
    "ScenarioConfig",
    "ScenarioEndedEvent",
    "ScenarioStartedEvent",
    "SensorConfig",
    "SensorDataEvent",
    "SensorSimulationError",
    "SimulationError",
    "SimulationTickEvent",
    "SynchronizedFrame",
    "TimestampedData",
    "ValidationError",
    "VehicleState",
    "configure_logging",
    "load_scenario_config",
    "logger",
    "run_context",
]
