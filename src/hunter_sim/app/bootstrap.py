"""L5 组合根：构建依赖注入容器。

自配置出发装配全部五层的 **具体实现**，按 Protocol 键绑定至 :class:`Container`，
并组装 :class:`~hunter_sim.app.orchestrator.RunOrchestratorImpl`。这是全系统中唯一
集中导入并实例化各层 ``Impl``（含 CARLA 相关实现）的位置——其余模块仅依赖 Protocol。

必须在运行中的事件循环内调用 :func:`build_container`（缓冲注册表与传感器管理器
需捕获当前 ``loop`` 以支持 CARLA 回调线程的跨线程投递）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from hunter_sim.acquisition.buffer import BufferRegistryImpl
from hunter_sim.acquisition.protocols import BufferRegistry, DataWriter, SensorManager
from hunter_sim.acquisition.sensor_manager import SensorManagerImpl
from hunter_sim.acquisition.writer import DiskDataWriterImpl
from hunter_sim.app.orchestrator import RunOrchestratorImpl
from hunter_sim.app.protocols import Orchestrator, Scheduler
from hunter_sim.app.scheduler import CollectionSchedulerImpl
from hunter_sim.container import Container
from hunter_sim.core.config import ConnectionConfig, ScenarioConfig
from hunter_sim.core.event_bus import InMemoryEventBus
from hunter_sim.core.logging import logger
from hunter_sim.core.protocols import EventBus
from hunter_sim.evaluation.metrics import MetricsEngineImpl
from hunter_sim.evaluation.protocols import MetricsEngine, Replayer, Reporter
from hunter_sim.evaluation.replay import ReplayerImpl
from hunter_sim.evaluation.reporter import ReporterImpl
from hunter_sim.processing.cleaner import CleanerImpl
from hunter_sim.processing.converter import ConverterImpl
from hunter_sim.processing.protocols import Cleaner, Converter, Synchronizer
from hunter_sim.processing.synchronizer import SynchronizerImpl
from hunter_sim.simulation.connection import CarlaConnectionManagerImpl
from hunter_sim.simulation.protocols import (
    CarlaConnectionManager,
    ScenarioManager,
    VehicleController,
)
from hunter_sim.simulation.scenario import ScenarioManagerImpl
from hunter_sim.simulation.vehicle import VehicleControllerImpl

_IMAGE_FORMATS = frozenset({"npy", "png", "jpeg"})
_POINTCLOUD_FORMATS = frozenset({"pcd", "npy", "hdf5"})
_TELEMETRY_FORMATS = frozenset({"json", "msgpack"})


def _resolve_formats(formats: Any) -> tuple[str, str, str]:
    """映射为写入器受支持的 ``(image_format, pointcloud_format, telemetry_format)``。

    不支持的格式回退为免依赖默认值（npy / pcd / json）并记录告警。
    """
    image = getattr(formats, "camera", "npy")
    pointcloud = getattr(formats, "lidar", "pcd")
    telemetry = getattr(formats, "telemetry", "json")
    if image not in _IMAGE_FORMATS:
        logger.bind(component="bootstrap").warning("图像格式 {} 不支持，回退为 npy", image)
        image = "npy"
    if pointcloud not in _POINTCLOUD_FORMATS:
        logger.bind(component="bootstrap").warning("点云格式 {} 不支持，回退为 pcd", pointcloud)
        pointcloud = "pcd"
    if telemetry not in _TELEMETRY_FORMATS:
        logger.bind(component="bootstrap").warning("遥测格式 {} 不支持，回退为 json", telemetry)
        telemetry = "json"
    return str(image), str(pointcloud), str(telemetry)


def build_container(
    config: ScenarioConfig,
    *,
    run_id: str,
    connection_config: ConnectionConfig | None = None,
) -> Container:
    """按场景配置装配全部依赖并返回已绑定的容器。

    Args:
        config: 场景配置根模型。
        run_id: 本次运行唯一标识（决定写入器输出目录）。
        connection_config: CARLA 连接参数；``None`` 时使用默认值。

    Returns:
        已注册各层 Protocol→实现及 L5 编排器的 :class:`Container`。

    Raises:
        RuntimeError: 在未运行事件循环的上下文中调用。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:  # pragma: no cover - 误用路径
        raise RuntimeError("build_container 须在运行中的事件循环内调用") from exc

    conn_cfg = connection_config or ConnectionConfig()
    image_format, pointcloud_format, telemetry_format = _resolve_formats(config.output.formats)

    event_bus = InMemoryEventBus()
    connection = CarlaConnectionManagerImpl(conn_cfg)
    vehicle = VehicleControllerImpl(connection)
    scenario = ScenarioManagerImpl(config, connection, vehicle, event_bus)
    registry = BufferRegistryImpl(loop)
    sensor_manager = SensorManagerImpl(registry, loop)
    writer = DiskDataWriterImpl(
        run_id=run_id,
        base_dir=config.output.base_dir,
        image_format=image_format,
        pointcloud_format=pointcloud_format,
        telemetry_format=telemetry_format,
    )
    converter = ConverterImpl()
    synchronizer = SynchronizerImpl()
    cleaner = CleanerImpl()
    metrics = MetricsEngineImpl()
    reporter = ReporterImpl()
    replayer = ReplayerImpl()
    scheduler = CollectionSchedulerImpl(
        scenario=scenario,
        registry=registry,
        converter=converter,
        cleaner=cleaner,
        writer=writer,
        sensor_types={s.id: s.type for s in config.sensors},
    )
    orchestrator = RunOrchestratorImpl(
        run_id=run_id,
        config=config,
        connection=connection,
        scenario=scenario,
        vehicle=vehicle,
        sensor_manager=sensor_manager,
        registry=registry,
        scheduler=scheduler,
        writer=writer,
        event_bus=event_bus,
        metrics=metrics,
        reporter=reporter,
    )

    container = Container()
    bindings: list[tuple[type[Any], Any]] = [
        (EventBus, event_bus),
        (CarlaConnectionManager, connection),
        (VehicleController, vehicle),
        (ScenarioManager, scenario),
        (BufferRegistry, registry),
        (SensorManager, sensor_manager),
        (DataWriter, writer),
        (Converter, converter),
        (Synchronizer, synchronizer),
        (Cleaner, cleaner),
        (MetricsEngine, metrics),
        (Reporter, reporter),
        (Replayer, replayer),
        (Scheduler, scheduler),
        (Orchestrator, orchestrator),
    ]
    for interface, instance in bindings:
        container.register_instance(interface, instance)
    logger.bind(component="bootstrap", run_id=run_id).info("依赖容器装配完成")
    return container


__all__ = ["build_container"]
