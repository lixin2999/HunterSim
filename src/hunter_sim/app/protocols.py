"""L5 应用调度层 Protocol 契约。

依赖方向：L5 位于架构顶层，可向下调用 L1~L4 的 Protocol；本层契约仅面向同层
（编排器调用调度器）与对外（CLI 调用编排器）暴露。跨层数据以不可变对象传递。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hunter_sim.app.models import RunResult


@runtime_checkable
class Scheduler(Protocol):
    """采集调度器契约（模块 5.1）。

    驱动单帧采集循环：**步进场景 → 取空各传感器缓冲 → 转换 → 清洗 → 落盘**，
    并累计 tick 与各传感器写入计数。
    """

    @property
    def ticks(self) -> int:
        """已完成的 tick 数。"""
        ...

    @property
    def sensor_counts(self) -> dict[str, int]:
        """各传感器累计写入帧数（返回副本）。"""
        ...

    @property
    def written_frames(self) -> int:
        """累计落盘帧总数。"""
        ...

    async def step_once(self) -> int:
        """推进一帧并处理本帧到达的数据，返回本帧写入的帧数。"""
        ...

    async def run(self, *, max_ticks: int | None = None) -> int:
        """持续步进直至达到 ``max_ticks`` 或场景不再运行，返回总 tick 数。"""
        ...


@runtime_checkable
class Orchestrator(Protocol):
    """运行编排器契约（模块 5.2）。

    编排一次完整采集运行的生命周期：建立连接 → 配置/启动场景 → 初始化并启动
    传感器 → 驱动调度器 → 收尾并汇总评估指标，最终产出聚合 :class:`RunResult`。
    """

    async def run(self, *, max_ticks: int | None = None) -> RunResult:
        """执行一次端到端采集运行并返回聚合结果。

        Args:
            max_ticks: 最大步进 tick 数；``None`` 时按场景配置时长推算。

        Returns:
            含元数据、写入计数与评估报告的运行结果。
        """
        ...


__all__ = ["Orchestrator", "Scheduler"]
