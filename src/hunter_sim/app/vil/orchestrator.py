"""模块 4：VIL 引擎主循环编排。

设计（§4.5.1）：

- **同步模式驱动**：``ScenarioManager`` 已在 ``configure`` 阶段将 CARLA world 设为
  同步模式（``synchronous_mode=True`` + ``fixed_delta_seconds``），本编排器负责
  **每步一 tick** 的循环；
- **数据等待策略**：若当前 tick 无新数据或延迟超过 ``data_timeout_ms``，则**暂停**
  场景（通过 :class:`ScenarioManager.pause` / ``resume``），并在恢复后继续；
- **降级**：无数据（``telemetry is None``）时**仍执行 tick**（避免物理引擎停滞），
  仅在延迟超阈值时才暂停；
- **可视化**：每步在同步 vehicle 后调用 ``CARLAVisualizerImpl.adraw_all`` 叠加
  速度文字 / 行为标签 / 感知框 / 规划轨迹。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from hunter_sim.app.vil.models import VehicleTelemetry, VILConfig
from hunter_sim.app.vil.protocols import (
    StateSynchronizer,
    SyncController,
    TelemetrySource,
)
from hunter_sim.core.logging import logger
from hunter_sim.simulation.models import Transform
from hunter_sim.simulation.protocols import ScenarioManager


class _VisualizerProtocol:
    """本地最小契约：只声明编排器需要的 ``adraw_all``。"""

    async def adraw_all(  # pragma: no cover - 协议占位
        self, telemetry: VehicleTelemetry, map_pose: Transform
    ) -> None: ...


class VILEngineImpl:
    """满足 :class:`~hunter_sim.app.vil.protocols.VILEngine` 契约。"""

    def __init__(
        self,
        *,
        scenario: ScenarioManager,
        telemetry_source: TelemetrySource,
        synchronizer: StateSynchronizer,
        visualizer: _VisualizerProtocol,
        sync_ctrl: SyncController,
        config: VILConfig,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化 VIL 引擎。

        Args:
            scenario: 场景管理器（tick / pause / resume 驱动）。
            telemetry_source: 遥测数据源（Kafka 或 Mock）。
            synchronizer: 状态同步器。
            visualizer: 可视化叠加（需实现 ``adraw_all``）。
            sync_ctrl: 延迟判断控制器。
            config: VIL 运行参数（tick_interval / 阈值）。
            clock: 墙钟（Unix 秒），默认 :func:`time.time`（单测可注入）。
        """
        self._scenario = scenario
        self._telemetry = telemetry_source
        self._synchronizer = synchronizer
        self._visualizer = visualizer
        self._sync_ctrl = sync_ctrl
        self._config = config
        self._clock = clock or time.time
        self._tick = 0
        self._paused = False
        self._running = False

    @property
    def tick_count(self) -> int:
        """已完成 tick 数。"""
        return self._tick

    @property
    def is_paused(self) -> bool:
        """当前是否处于数据中断导致的暂停态。"""
        return self._paused

    async def start(self) -> None:
        """启动遥测消费；场景已由 :class:`ScenarioManager` 完成 configure/start。"""
        await self._telemetry.start()
        self._running = True
        logger.bind(component="vil.engine").info(
            "VIL 引擎启动: target_vehicle_id={}", self._config.target_vehicle_id
        )

    async def step(self) -> None:
        """单次 tick 循环：拉数据 → 判断 → 同步 → 场景步进 → 可视化。"""
        telemetry = await self._telemetry.poll_latest()

        if telemetry is not None:
            delay_ms = max(0.0, (self._clock() - telemetry.timestamp) * 1000.0)
            if self._sync_ctrl.should_pause(delay_ms):
                await self._enter_pause()
            else:
                await self._exit_pause_if_any()
                map_pose = await self._synchronizer.sync(telemetry)
                # 可视化在 tick 之后（世界坐标已生效）。
                await self._scenario.step()
                await self._visualizer.adraw_all(telemetry, map_pose)
                self._tick += 1
                return
        else:
            # 无数据 → 保持步进，物理不失真；不主动暂停（等待首帧或恢复由数据到达触发）。
            await self._scenario.step()
            self._tick += 1
            return

        # 触发暂停路径：仅步进，不做同步与可视化。
        await self._scenario.step()
        self._tick += 1

    async def _enter_pause(self) -> None:
        if self._paused:
            return
        self._paused = True
        await self._scenario.pause()
        logger.bind(component="vil.engine").warning(
            "实车数据中断（>{}ms），场景暂停", self._config.data_timeout_ms
        )

    async def _exit_pause_if_any(self) -> None:
        if not self._paused:
            return
        self._paused = False
        await self._scenario.resume()
        logger.bind(component="vil.engine").info("实车数据恢复，场景继续")

    async def run(self, *, max_ticks: int | None = None) -> int:
        """持续步进直至达到 ``max_ticks``。

        Returns:
            实际完成的 tick 数。
        """
        while self._running and (max_ticks is None or self._tick < max_ticks):
            await self.step()
        return self._tick

    async def stop(self) -> None:
        """停止引擎：关闭数据源（幂等）。"""
        if not self._running:
            return
        self._running = False
        await self._telemetry.stop()
        logger.bind(component="vil.engine").info("VIL 引擎已停止: ticks={}", self._tick)


__all__ = ["VILEngineImpl"]
