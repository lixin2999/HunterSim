"""端到端集成测试：以 mock 的 CARLA 面向组件驱动真实 L2~L5 管线。

真实装配 **缓冲注册表 → 采集调度器 → 转换 → 清洗 → 落盘 → 指标 → 报告 → 回放**，
仅对 CARLA 交互面（连接/车辆/场景/传感器管理）使用轻量假件：假场景在每次步进时向
对应缓冲投递合成测量，模拟传感器数据到达。验证运行结果、磁盘落盘与回放读取的闭环。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np

from hunter_sim.acquisition.buffer import BufferRegistryImpl
from hunter_sim.acquisition.writer import DiskDataWriterImpl
from hunter_sim.app.orchestrator import RunOrchestratorImpl
from hunter_sim.app.scheduler import CollectionSchedulerImpl
from hunter_sim.core.config import ScenarioConfig
from hunter_sim.evaluation.metrics import MetricsEngineImpl
from hunter_sim.evaluation.replay import ReplayerImpl
from hunter_sim.evaluation.reporter import ReporterImpl
from hunter_sim.processing.cleaner import CleanerImpl
from hunter_sim.processing.converter import ConverterImpl
from hunter_sim.simulation.protocols import ScenarioState


def make_config() -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {
            "scenario": {
                "name": "integration",
                "map": "Town03",
                "duration_seconds": 0.4,
                "tick_rate": 10.0,
            },
            "vehicle": {"blueprint": "vehicle.tesla.model3"},
            "sensors": [
                {
                    "id": "cam0",
                    "type": "camera.rgb",
                    "position": [0.0, 0.0, 1.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "tick_rate": 10.0,
                    "width": 4,
                    "height": 4,
                    "fov": 90.0,
                },
                {
                    "id": "lidar0",
                    "type": "lidar.ray_cast",
                    "position": [0.0, 0.0, 2.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "channels": 16,
                    "range": 50.0,
                    "points_per_second": 1000000,
                    "rotation_frequency": 10.0,
                },
            ],
            "output": {"base_dir": "./data/runs"},
        }
    )


def _camera_measurement(tick: int) -> SimpleNamespace:
    h, w = 4, 4
    raw = np.zeros((h, w, 4), dtype=np.uint8)
    return SimpleNamespace(
        timestamp=float(tick) / 10.0,
        width=w,
        height=h,
        fov=90.0,
        raw_data=raw.tobytes(),
    )


def _lidar_measurement(tick: int) -> SimpleNamespace:
    pts = np.array([[1.0, 2.0, 3.0, 0.5], [4.0, 5.0, 6.0, 0.7]], dtype=np.float32)
    return SimpleNamespace(timestamp=float(tick) / 10.0, raw_data=pts.tobytes())


class FakeScenario:
    """步进时向缓冲投递合成测量的假场景（模拟传感器数据到达）。"""

    def __init__(self, registry: BufferRegistryImpl) -> None:
        self._registry = registry
        self._tick = 0
        self.state = ScenarioState.RUNNING
        self._pushed: dict[str, object] = {
            "cam0": _camera_measurement,
            "lidar0": _lidar_measurement,
        }

    async def configure(self) -> None:
        for sid in ("cam0", "lidar0"):
            self._registry.get_or_create(sid)

    async def start(self) -> None:
        self.state = ScenarioState.RUNNING

    async def stop(self, *, status: str = "completed") -> None:
        self.state = ScenarioState.COMPLETED

    async def step(self) -> object:
        for sid, factory in self._pushed.items():
            buffer = self._registry.get_or_create(sid)
            await buffer.put(factory(self._tick))  # type: ignore[operator]
        self._tick += 1
        return object()


class FakeSensorManager:
    """不接触真实 CARLA、仅记录调用的假传感器管理器。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def initialize(self, configs: list[object], *, vehicle: object, world: object) -> None:
        self.calls.append("initialize")

    async def start_all(self) -> None:
        self.calls.append("start_all")

    async def stop_all(self) -> None:
        self.calls.append("stop_all")

    async def destroy_all(self) -> None:
        self.calls.append("destroy_all")


async def test_end_to_end_capture_write_and_replay(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    config = make_config()

    registry = BufferRegistryImpl(loop)
    writer = DiskDataWriterImpl(
        run_id="e2e", base_dir=tmp_path, image_format="npy", pointcloud_format="npy"
    )
    scenario = FakeScenario(registry)
    scheduler = CollectionSchedulerImpl(
        scenario=scenario,
        registry=registry,
        converter=ConverterImpl(),
        cleaner=CleanerImpl(),
        writer=writer,
        sensor_types={"cam0": "camera.rgb", "lidar0": "lidar.ray_cast"},
    )

    connection = MagicMock()
    connection.connect = AsyncMock()
    connection.disconnect = AsyncMock()
    connection.get_world.return_value = MagicMock()
    vehicle = MagicMock()
    vehicle.get_actor.return_value = MagicMock()
    sensor_manager = FakeSensorManager()
    event_bus = MagicMock()
    event_bus.subscribe.return_value = lambda: None

    orchestrator = RunOrchestratorImpl(
        run_id="e2e",
        config=config,
        connection=connection,
        scenario=scenario,  # type: ignore[arg-type]
        vehicle=vehicle,
        sensor_manager=sensor_manager,  # type: ignore[arg-type]
        registry=registry,
        scheduler=scheduler,
        writer=writer,
        event_bus=event_bus,
        metrics=MetricsEngineImpl(),
        reporter=ReporterImpl(),
    )

    result = await orchestrator.run(max_ticks=4)

    assert result.ticks == 4
    assert result.metadata.status == "completed"
    assert result.written_frames == 8  # 4 tick × 2 传感器
    assert result.sensor_counts == {"cam0": 4, "lidar0": 4}
    assert result.report is not None
    assert result.report.coverage is not None
    assert result.report.coverage.overall > 0.0
    assert sensor_manager.calls[:2] == ["initialize", "start_all"]

    # 磁盘闭环：元数据 + 各传感器 4 帧
    run_root = tmp_path / "e2e"
    assert (run_root / "metadata.json").is_file()
    assert len(list((run_root / "cam0").glob("*.npy"))) == 4
    assert len(list((run_root / "lidar0").glob("*.npy"))) == 4

    # 回放读取闭环（writer 写 → replayer 读）
    replayer = ReplayerImpl()
    assert set(replayer.list_sensors(run_root)) >= {"cam0", "lidar0"}
    meta = replayer.load_metadata(run_root)
    assert meta["run_id"] == "e2e"
    assert meta["status"] == "completed"

    cam_frames = [f async for f in replayer.iter_frames(run_root, "cam0")]
    assert len(cam_frames) == 4
    assert cam_frames[0].payload.shape == (4, 4, 3)

    lidar_frames = [f async for f in replayer.iter_frames(run_root, "lidar0")]
    assert len(lidar_frames) == 4
    assert lidar_frames[0].payload.shape[1] == 4
