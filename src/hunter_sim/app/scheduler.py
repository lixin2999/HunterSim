"""模块 5.1：采集调度器。

驱动单帧采集循环：**步进场景 → 取空各传感器缓冲 → 转换 → 清洗 → 落盘**，
并维护 tick 计数与各传感器写入计数。仅依赖各层 Protocol，全部可 mock 测试。
"""

from __future__ import annotations

from collections.abc import Mapping

from hunter_sim.acquisition.protocols import BufferRegistry, DataWriter
from hunter_sim.core.exceptions import ConversionError
from hunter_sim.core.logging import logger
from hunter_sim.processing.protocols import Cleaner, Converter
from hunter_sim.simulation.protocols import ScenarioManager, ScenarioState


class CollectionSchedulerImpl:
    """满足采集调度契约：逐步进并落盘一帧内的传感器数据。"""

    def __init__(
        self,
        *,
        scenario: ScenarioManager,
        registry: BufferRegistry,
        converter: Converter,
        cleaner: Cleaner,
        writer: DataWriter,
        sensor_types: Mapping[str, str],
    ) -> None:
        """初始化调度器。

        Args:
            scenario: 场景管理器（推进帧）。
            registry: 传感器缓冲注册表。
            converter: 原始测量 → 契约帧转换器。
            cleaner: 数据清洗器。
            writer: 落盘写入器。
            sensor_types: ``sensor_id -> sensor_type`` 映射（供转换器分派）。
        """
        self._scenario = scenario
        self._registry = registry
        self._converter = converter
        self._cleaner = cleaner
        self._writer = writer
        self._sensor_types = dict(sensor_types)
        self._ticks = 0
        self._frame_counter = 0
        self._sensor_counts: dict[str, int] = {}

    @property
    def ticks(self) -> int:
        """已完成的 tick 数。"""
        return self._ticks

    @property
    def sensor_counts(self) -> dict[str, int]:
        """各传感器累计写入帧数。"""
        return dict(self._sensor_counts)

    @property
    def written_frames(self) -> int:
        """累计写入落盘的帧总数。"""
        return sum(self._sensor_counts.values())

    async def step_once(self) -> int:
        """推进一帧并处理到达的数据，返回本帧写入的帧数。"""
        await self._scenario.step()
        written = 0
        for sensor_id in self._registry.sensor_ids():
            buffer = self._registry.get(sensor_id)
            sensor_type = self._sensor_types.get(sensor_id)
            if buffer is None or sensor_type is None:
                continue
            for raw in buffer.drain_nowait():
                if await self._process(sensor_id, sensor_type, raw):
                    written += 1
        self._ticks += 1
        return written

    async def _process(self, sensor_id: str, sensor_type: str, raw: object) -> bool:
        """转换/清洗/落盘单条原始测量；转换失败记录并跳过（返回是否成功写入）。"""
        frame_id = self._frame_counter
        self._frame_counter += 1
        try:
            frame = self._converter.convert(
                sensor_type, raw, sensor_id=sensor_id, frame_id=frame_id
            )
        except ConversionError as exc:
            logger.bind(component="scheduler", sensor_id=sensor_id).warning(
                "转换失败，跳过: {}", exc
            )
            return False
        await self._writer.write(sensor_id, self._cleaner.clean(frame))
        self._sensor_counts[sensor_id] = self._sensor_counts.get(sensor_id, 0) + 1
        return True

    async def run(self, *, max_ticks: int | None = None) -> int:
        """持续步进直至达到 ``max_ticks`` 或场景不再运行，返回总 tick 数。"""
        while max_ticks is None or self._ticks < max_ticks:
            if self._scenario.state != ScenarioState.RUNNING:
                break
            await self.step_once()
        return self._ticks


__all__ = ["CollectionSchedulerImpl"]
