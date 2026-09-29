"""L2 数据采集层接口契约（``typing.Protocol``）。

依赖注入：上层（L3/L5）仅依赖这些 Protocol；实现类以 ``Impl`` 结尾由容器绑定。
缓冲元素为带 ``timestamp`` 的对象（原始 CARLA 测量或 L3 转换后的 ``core`` 帧），
以最小化 L2 与具体数据格式的耦合。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class HasTimestamp(Protocol):
    """缓冲元素最小协议：至少携带仿真时间戳。"""

    @property
    def timestamp(self) -> float: ...


@runtime_checkable
class SensorBuffer(Protocol):
    """单传感器数据缓冲契约（模块 2.2）。

    多生产者（CARLA 回调线程）/ 单消费者（异步写入协程）模型；环形缓冲限制内存，
    队列提供流式消费与背压。
    """

    @property
    def sensor_id(self) -> str: ...

    async def put(self, frame: HasTimestamp) -> None:
        """在同一事件循环中入队一帧。"""
        ...

    def put_from_thread(self, frame: HasTimestamp) -> None:
        """从 CARLA 回调线程安全入队（经 ``loop.call_soon_threadsafe``）。"""
        ...

    async def get(self, timeout: float | None = None) -> HasTimestamp:
        """出队一帧，超时抛 ``TimeoutError``，缓冲关闭抛 ``BufferClosed``。"""
        ...

    def query_range(self, t_start: float, t_end: float) -> list[HasTimestamp]:
        """按时间戳闭区间查询环形缓冲内的历史帧。"""
        ...

    def drain_nowait(self, max_items: int | None = None) -> list[HasTimestamp]:
        """非阻塞取空待消费队列，供每 tick 批量处理。"""
        ...

    def size(self) -> int:
        """当前待消费（队列内）帧数。"""
        ...

    async def close(self) -> None:
        """关闭缓冲并唤醒等待中的消费者。"""
        ...


@runtime_checkable
class BufferRegistry(Protocol):
    """按 ``sensor_id`` 管理独立缓冲的注册表契约。"""

    def get_or_create(self, sensor_id: str) -> SensorBuffer: ...

    def get(self, sensor_id: str) -> SensorBuffer | None: ...

    def sensor_ids(self) -> list[str]: ...

    async def close_all(self) -> None: ...


@runtime_checkable
class SensorManager(Protocol):
    """传感器生命周期管理契约（模块 2.1）。"""

    async def initialize(self, configs: list[Any], *, vehicle: Any, world: Any) -> None: ...

    def register_callback(self, sensor_id: str, callback: Callable[[Any], None]) -> None: ...

    async def start_all(self) -> None: ...

    async def stop_all(self) -> None: ...

    async def start_sensor(self, sensor_id: str) -> None: ...

    async def stop_sensor(self, sensor_id: str) -> None: ...

    async def destroy_all(self) -> None: ...


@runtime_checkable
class DataWriter(Protocol):
    """数据持久化写入器契约（模块 2.3）。"""

    async def write(self, sensor_id: str, item: Any) -> Path: ...

    async def write_batch(self, sensor_id: str, items: list[Any]) -> list[Path]: ...

    async def write_metadata(self, payload: dict[str, Any]) -> Path: ...

    async def flush(self) -> None: ...

    async def close(self) -> None: ...


__all__ = [
    "BufferRegistry",
    "DataWriter",
    "HasTimestamp",
    "SensorBuffer",
    "SensorManager",
]
