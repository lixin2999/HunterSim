"""模块 5.2：运行编排器。

编排一次端到端采集运行的完整生命周期：**建立连接 → 配置/启动场景 → 初始化并启动
传感器 → 驱动采集调度器 → 停止/收尾 → 汇总评估指标 → 产出 :class:`RunResult`**。

依赖注入：全部协作者以各层 Protocol 注入，无任何真实 CARLA 依赖，便于全量 mock 测试。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from hunter_sim.acquisition.protocols import BufferRegistry, DataWriter, SensorManager
from hunter_sim.app.models import RunResult
from hunter_sim.app.protocols import Scheduler
from hunter_sim.core.config import ScenarioConfig
from hunter_sim.core.contracts import RunMetadata
from hunter_sim.core.events import CollisionEvent, Event, LaneInvasionEvent
from hunter_sim.core.logging import logger
from hunter_sim.core.protocols import EventBus
from hunter_sim.evaluation.protocols import MetricsEngine, Reporter
from hunter_sim.simulation.protocols import (
    CarlaConnectionManager,
    ScenarioManager,
    VehicleController,
)


class RunOrchestratorImpl:
    """满足 :class:`~hunter_sim.app.protocols.Orchestrator` 契约。"""

    def __init__(
        self,
        *,
        run_id: str,
        config: ScenarioConfig,
        connection: CarlaConnectionManager,
        scenario: ScenarioManager,
        vehicle: VehicleController,
        sensor_manager: SensorManager,
        registry: BufferRegistry,
        scheduler: Scheduler,
        writer: DataWriter,
        event_bus: EventBus,
        metrics: MetricsEngine,
        reporter: Reporter,
    ) -> None:
        """初始化编排器。

        Args:
            run_id: 本次运行唯一标识（与写入器目录一致）。
            config: 场景配置根模型。
            connection: CARLA 连接管理器。
            scenario: 场景管理器。
            vehicle: 主车控制器（供传感器附着取用 actor）。
            sensor_manager: 传感器生命周期管理器。
            registry: 传感器缓冲注册表（收尾时关闭）。
            scheduler: 采集调度器。
            writer: 数据落盘写入器。
            event_bus: 事件总线（订阅碰撞/压线计数）。
            metrics: 指标引擎。
            reporter: 报告构建器。
        """
        self._run_id = run_id
        self._config = config
        self._connection = connection
        self._scenario = scenario
        self._vehicle = vehicle
        self._sensor_manager = sensor_manager
        self._registry = registry
        self._scheduler = scheduler
        self._writer = writer
        self._bus = event_bus
        self._metrics = metrics
        self._reporter = reporter

    def _build_metadata(self) -> RunMetadata:
        """按当前配置构造运行元数据（起始状态为 ``running``）。"""
        return RunMetadata(
            run_id=self._run_id,
            scenario_name=self._config.scenario.name,
            map_name=self._config.scenario.map,
            start_time=datetime.now(UTC),
            sensor_configs=[s.model_dump(mode="json") for s in self._config.sensors],
            vehicle_config=self._config.vehicle.model_dump(mode="json"),
            weather_config=self._config.weather.model_dump(mode="json"),
        )

    async def run(self, *, max_ticks: int | None = None) -> RunResult:
        """执行一次端到端采集运行并返回聚合结果。

        Args:
            max_ticks: 最大步进 tick 数；``None`` 时按 ``duration_seconds * tick_rate`` 推算。

        Returns:
            含元数据、写入计数与评估报告的运行结果。

        Raises:
            HunterSimError: 生命周期内任意阶段失败（收尾后原样重抛）。
        """
        sc = self._config.scenario
        target_ticks = (
            max_ticks if max_ticks is not None else round(sc.duration_seconds * sc.tick_rate)
        )
        metadata = self._build_metadata()
        counters: dict[str, int] = {"collision": 0, "lane_invasion": 0}
        unsubscribes = self._subscribe_counters(counters)

        status = "completed"
        try:
            await self._connection.connect()
            await self._scenario.configure()
            await self._scenario.start()
            await self._sensor_manager.initialize(
                list(self._config.sensors),
                vehicle=self._vehicle.get_actor(),
                world=self._connection.get_world(),
            )
            await self._sensor_manager.start_all()

            await self._scheduler.run(max_ticks=target_ticks)
            await self._sensor_manager.stop_all()
            await self._scenario.stop()

            metadata.total_frames = self._scheduler.written_frames
            self._finalize(metadata, status="completed")
        except BaseException:
            status = "failed"
            self._finalize(metadata, status="failed")
            raise
        finally:
            for unsubscribe in unsubscribes:
                unsubscribe()
            await self._cleanup()

        logger.bind(component="orchestrator", run_id=self._run_id).info(
            "运行结束: status={} ticks={} frames={}",
            status,
            self._scheduler.ticks,
            metadata.total_frames,
        )
        report = self._build_report(metadata, counters)
        await self._writer.write_metadata(metadata.model_dump(mode="json"))
        return RunResult(
            run_id=self._run_id,
            scenario_name=sc.name,
            ticks=self._scheduler.ticks,
            written_frames=self._scheduler.written_frames,
            sensor_counts=self._scheduler.sensor_counts,
            metadata=metadata,
            report=report,
        )

    def _subscribe_counters(self, counters: dict[str, int]) -> list[Callable[[], None]]:
        """订阅碰撞与压线事件以累计计数，返回退订函数列表。"""

        def _on_event(kind: str) -> Callable[[Event], None]:
            def _callback(_event: Event) -> None:
                counters[kind] += 1

            return _callback

        return [
            self._bus.subscribe(CollisionEvent, _on_event("collision")),
            self._bus.subscribe(LaneInvasionEvent, _on_event("lane_invasion")),
        ]

    @staticmethod
    def _finalize(metadata: RunMetadata, *, status: str) -> None:
        """幂等地终结元数据（已终结则跳过）。"""
        if metadata.status == "running":
            metadata.finalize(status=status, end_time=datetime.now(UTC))

    def _build_report(self, metadata: RunMetadata, counters: dict[str, int]) -> Any:
        """基于运行计数构建评估报告（安全 + 覆盖率维度，零内存累积）。"""
        sc = self._config.scenario
        total_ticks = self._scheduler.ticks
        safety = self._metrics.safety(
            collision_count=counters["collision"],
            lane_invasion_count=counters["lane_invasion"],
            total_ticks=total_ticks,
            tick_rate=sc.tick_rate,
        )
        coverage = self._metrics.coverage(
            total_ticks=total_ticks,
            per_sensor_counts=self._scheduler.sensor_counts,
        )
        return self._reporter.build_report(
            run_id=metadata.run_id,
            scenario_name=metadata.scenario_name,
            safety=safety,
            coverage=coverage,
        )

    async def _cleanup(self) -> None:
        """尽力收尾：销毁传感器、关闭缓冲与写入器、断开连接（逐项失败不阻断其余）。"""
        with contextlib.suppress(Exception):
            await self._sensor_manager.destroy_all()
        with contextlib.suppress(Exception):
            await self._registry.close_all()
        with contextlib.suppress(Exception):
            await self._writer.close()
        with contextlib.suppress(Exception):
            await self._connection.disconnect()


__all__ = ["RunOrchestratorImpl"]
