"""HunterSim VIL 虚实映射服务层（ENG-002）。

负责实车实时数据接入、坐标映射、状态同步、感知叠加渲染。
数据流：Kafka → TelemetryBuffer → VILSyncService → CARLA Vehicle。
"""

from hunter_sim.vil_mapper.calibration_manager import CalibrationManager
from hunter_sim.vil_mapper.data_health_monitor import (
    DataHealthMonitor,
    DataHealthReport,
    DataHealthStatus,
)
from hunter_sim.vil_mapper.pose_extrapolator import PoseExtrapolator
from hunter_sim.vil_mapper.state_visualizer import PerceptionOverlay, StateVisualizer
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame
from hunter_sim.vil_mapper.vil_data_consumer import VILDataConsumer
from hunter_sim.vil_mapper.vil_sync_service import VILSyncService, VILSyncStats

__all__ = [
    "CalibrationManager",
    "DataHealthMonitor", "DataHealthReport", "DataHealthStatus",
    "PoseExtrapolator",
    "PerceptionOverlay", "StateVisualizer",
    "TelemetryBuffer", "TelemetryFrame",
    "VILDataConsumer",
    "VILSyncService", "VILSyncStats",
]
