"""L5 组合根装配的单元测试（CARLA 依赖由 conftest 全局 mock）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hunter_sim.acquisition.protocols import BufferRegistry, DataWriter, SensorManager
from hunter_sim.app.bootstrap import _resolve_formats, build_container
from hunter_sim.app.orchestrator import RunOrchestratorImpl
from hunter_sim.app.protocols import Orchestrator, Scheduler
from hunter_sim.app.scheduler import CollectionSchedulerImpl
from hunter_sim.container import Container
from hunter_sim.core.config import ConnectionConfig, ScenarioConfig
from hunter_sim.core.protocols import EventBus
from hunter_sim.evaluation.protocols import MetricsEngine, Replayer, Reporter
from hunter_sim.processing.protocols import Cleaner, Converter, Synchronizer
from hunter_sim.simulation.protocols import (
    CarlaConnectionManager,
    ScenarioManager,
    VehicleController,
)


def make_config() -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "scenario": {
                "name": "demo",
                "map": "Town01",
                "duration_seconds": 1.0,
                "tick_rate": 10.0,
            },
            "vehicle": {"blueprint": "vehicle.tesla.model3"},
            "sensors": [
                {
                    "id": "cam_front",
                    "type": "camera.rgb",
                    "position": [0.0, 0.0, 1.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "tick_rate": 10.0,
                    "width": 8,
                    "height": 8,
                    "fov": 90.0,
                },
                {
                    "id": "lidar_top",
                    "type": "lidar.ray_cast",
                    "position": [0.0, 0.0, 2.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "channels": 16,
                    "range": 50.0,
                    "points_per_second": 1000000,
                    "rotation_frequency": 10.0,
                },
            ],
            "output": {"base_dir": "./data/runs", "formats": {"camera": "png", "lidar": "hdf5"}},
        }
    )


async def test_build_container_registers_all_protocols() -> None:
    container = await _build_async()

    for interface in (
        EventBus,
        CarlaConnectionManager,
        VehicleController,
        ScenarioManager,
        BufferRegistry,
        SensorManager,
        DataWriter,
        Converter,
        Synchronizer,
        Cleaner,
        MetricsEngine,
        Reporter,
        Replayer,
        Scheduler,
        Orchestrator,
    ):
        assert interface in container, f"{interface!r} 未注册"
        assert container.resolve(interface) is not None  # type: ignore[arg-type]


async def test_resolved_orchestrator_is_singleton_impl() -> None:
    container = await _build_async()

    first = container.resolve(Orchestrator)
    second = container.resolve(Orchestrator)

    assert isinstance(first, RunOrchestratorImpl)
    assert first is second
    assert isinstance(container.resolve(Scheduler), CollectionSchedulerImpl)


def test_resolve_formats_maps_unsupported_to_fallback() -> None:
    # 全部受支持：原样返回（hdf5/msgpack 现已放行）
    assert _resolve_formats(SimpleNamespace(camera="png", lidar="hdf5", telemetry="msgpack")) == (
        "png",
        "hdf5",
        "msgpack",
    )
    # 不支持的格式均回退为免依赖默认值
    assert _resolve_formats(SimpleNamespace(camera="tiff", lidar="xyz", telemetry="csv")) == (
        "npy",
        "pcd",
        "json",
    )


def test_build_container_requires_running_loop() -> None:
    # 在无运行事件循环的同步上下文中调用应抛出 RuntimeError
    with pytest.raises(RuntimeError, match="事件循环"):
        build_container(make_config(), run_id="x", connection_config=ConnectionConfig())


async def _build_async() -> Container:
    """在运行中的事件循环内构造容器（供上述异步测试复用）。"""
    return build_container(make_config(), run_id="run-xyz", connection_config=ConnectionConfig())
