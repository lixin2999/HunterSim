"""HunterSim VIL 虚实映射服务层（ENG-002）。

负责实车实时数据接入、坐标映射、状态同步、感知叠加渲染。
数据流：Kafka carla.{sim_id}.vil_state → TelemetryBuffer → VILSyncService → CARLA Vehicle。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hunter_sim.vil_mapper.calibration_manager import CalibrationManager
from hunter_sim.vil_mapper.data_health_monitor import (
    DataHealthMonitor,
    DataHealthReport,
    DataHealthStatus,
)
from hunter_sim.vil_mapper.frame_aligner import AlignedFrame, AlignerStats, FrameAligner
from hunter_sim.vil_mapper.pose_extrapolator import PoseExtrapolator
from hunter_sim.vil_mapper.state_visualizer import PerceptionOverlay, StateVisualizer
from hunter_sim.vil_mapper.telemetry_buffer import (
    TelemetryBuffer,
    TelemetryFrame,
    compute_auto_buffer_depth,
)
from hunter_sim.vil_mapper.vil_data_consumer import VILDataConsumer
from hunter_sim.vil_mapper.vil_sync_service import VILSyncService, VILSyncStats

if TYPE_CHECKING:
    from hunter_sim.cloud.vil_state_parser import parse_vil_state

__all__ = [
    "AlignedFrame", "AlignerStats", "FrameAligner",
    "CalibrationManager",
    "DataHealthMonitor", "DataHealthReport", "DataHealthStatus",
    "PoseExtrapolator",
    "PerceptionOverlay", "StateVisualizer",
    "TelemetryBuffer", "TelemetryFrame", "compute_auto_buffer_depth",
    "VILDataConsumer",
    "VILSyncService", "VILSyncStats",
    "parse_vil_state",
]

# 延迟导入避免 cloud ↔ vil_mapper 循环依赖
_MOD_PATHS: dict[str, str] = {"parse_vil_state": "hunter_sim.cloud.vil_state_parser"}


def __getattr__(name: str) -> object:  # PEP 562
    target = _MOD_PATHS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415

    mod = importlib.import_module(target)
    val = getattr(mod, name)
    globals()[name] = val  # 缓存，后续直接命中
    return val
