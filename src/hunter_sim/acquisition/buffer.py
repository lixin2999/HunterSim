"""模块 2.2：线程安全数据缓冲。

- 每传感器独立缓冲：``deque`` 环形缓冲限制内存并支持时间区间查询，``asyncio.Queue``
  提供流式消费与背压控制。
- 多生产者（CARLA 回调线程）经 :meth:`RingSensorBuffer.put_from_thread` 借助
  ``loop.call_soon_threadsafe`` 安全入队；单消费者（异步写入协程）:meth:`get` 出队。
- 采集热路径不阻塞：队列满时按 ``on_full`` 策略丢弃（默认丢最新），并计数。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from typing import Any, cast

from hunter_sim.acquisition.protocols import HasTimestamp
from hunter_sim.core.exceptions import BufferClosedError
from hunter_sim.core.logging import logger

_CLOSE_SENTINEL: Any = object()


class RingSensorBuffer:
    """满足 :class:`~hunter_sim.acquisition.protocols.SensorBuffer` 契约。"""

    def __init__(
        self,
        sensor_id: str,
        *,
        loop: asyncio.AbstractEventLoop,
        max_frames: int = 300,
        max_queue: int = 600,
        on_full: str = "drop_newest",
    ) -> None:
        """初始化单传感器缓冲。

        Args:
            sensor_id: 传感器标识。
            loop: 目标事件循环（消费者运行、跨线程投递至此）。
            max_frames: 环形缓冲保留的最大帧数（用于时间区间查询）。
            max_queue: 待消费队列上限（背压阈值）。
            on_full: 队列满策略，``drop_newest`` 或 ``drop_oldest``。
        """
        if on_full not in ("drop_newest", "drop_oldest"):
            raise ValueError(f"未知 on_full 策略: {on_full}")
        self._sensor_id = sensor_id
        self._loop = loop
        self._ring: deque[HasTimestamp] = deque(maxlen=max_frames)
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_queue)
        self._on_full = on_full
        self._closed = False
        self._dropped = 0

    @property
    def sensor_id(self) -> str:
        """传感器标识。"""
        return self._sensor_id

    @property
    def dropped_count(self) -> int:
        """因背压被丢弃的帧数。"""
        return self._dropped

    @property
    def is_closed(self) -> bool:
        """缓冲是否已关闭。"""
        return self._closed

    def _deliver(self, frame: HasTimestamp) -> None:
        """在事件循环线程内执行的实际入队（环形缓冲 + 队列，含背压处理）。"""
        self._ring.append(frame)
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._on_full == "drop_oldest":
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()  # 丢弃最旧
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait(frame)
            elif self._dropped % 100 == 1:
                logger.bind(component="buffer", sensor_id=self._sensor_id).warning(
                    "缓冲队列已满，丢弃最新帧 (累计 {})", self._dropped
                )

    async def put(self, frame: HasTimestamp) -> None:
        """同一事件循环内入队。"""
        if self._closed:
            raise BufferClosedError(f"缓冲已关闭: {self._sensor_id}")
        self._deliver(frame)

    def put_from_thread(self, frame: HasTimestamp) -> None:
        """从任意线程安全入队。"""
        if self._closed:
            return
        # 环形缓冲 append 在 CPython 下受 GIL 保护，可即时反映；队列投递调度回事件循环。
        self._ring.append(frame)
        try:
            self._loop.call_soon_threadsafe(self._queue_put, frame)
        except RuntimeError:
            logger.bind(component="buffer", sensor_id=self._sensor_id).warning(
                "事件循环已停止，丢弃帧"
            )

    def _queue_put(self, frame: HasTimestamp) -> None:
        """由 ``call_soon_threadsafe`` 在循环线程调度的队列写入。"""
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._dropped += 1

    async def get(self, timeout: float | None = None) -> HasTimestamp:
        """出队一帧。

        Raises:
            BufferClosedError: 缓冲关闭且无残留数据，或读到关闭哨兵。
            asyncio.TimeoutError: 等待超时。
        """
        if self._closed and self._queue.empty():
            raise BufferClosedError(f"缓冲已关闭: {self._sensor_id}")
        item = await asyncio.wait_for(self._queue.get(), timeout)
        if item is _CLOSE_SENTINEL:
            raise BufferClosedError(f"缓冲已关闭: {self._sensor_id}")
        return cast(HasTimestamp, item)

    def query_range(self, t_start: float, t_end: float) -> list[HasTimestamp]:
        """按时间戳闭区间查询环形缓冲历史帧。"""
        if t_end < t_start:
            raise ValueError(f"非法区间: t_end({t_end}) < t_start({t_start})")
        return [f for f in self._ring if t_start <= float(f.timestamp) <= t_end]

    def drain_nowait(self, max_items: int | None = None) -> list[HasTimestamp]:
        """非阻塞取空待消费队列（供每 tick 批量处理）；遇关闭哨兵则停止并标记关闭。"""
        items: list[HasTimestamp] = []
        while max_items is None or len(items) < max_items:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _CLOSE_SENTINEL:
                self._closed = True
                break
            items.append(item)
        return items

    def size(self) -> int:
        """当前待消费（队列内）帧数。"""
        return self._queue.qsize()

    def history_size(self) -> int:
        """环形缓冲内保留的历史帧数。"""
        return len(self._ring)

    async def close(self) -> None:
        """关闭缓冲并唤醒等待中的消费者。"""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_CLOSE_SENTINEL)


class BufferRegistryImpl:
    """满足 :class:`~hunter_sim.acquisition.protocols.BufferRegistry` 契约。"""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        max_frames: int = 300,
        max_queue: int = 600,
        on_full: str = "drop_newest",
    ) -> None:
        """初始化注册表。

        Args:
            loop: 消费者事件循环。
            max_frames: 每传感器环形缓冲最大帧数。
            max_queue: 每传感器队列上限。
            on_full: 每传感器队列满策略。
        """
        self._loop = loop
        self._max_frames = max_frames
        self._max_queue = max_queue
        self._on_full = on_full
        self._buffers: dict[str, RingSensorBuffer] = {}

    def get_or_create(self, sensor_id: str) -> RingSensorBuffer:
        """获取或创建指定传感器的缓冲。"""
        buf = self._buffers.get(sensor_id)
        if buf is None:
            buf = RingSensorBuffer(
                sensor_id,
                loop=self._loop,
                max_frames=self._max_frames,
                max_queue=self._max_queue,
                on_full=self._on_full,
            )
            self._buffers[sensor_id] = buf
        return buf

    def get(self, sensor_id: str) -> RingSensorBuffer | None:
        """返回已存在的缓冲，不存在则 ``None``。"""
        return self._buffers.get(sensor_id)

    def sensor_ids(self) -> list[str]:
        """返回当前所有传感器 id。"""
        return list(self._buffers)

    async def close_all(self) -> None:
        """关闭全部缓冲。"""
        for buf in self._buffers.values():
            await buf.close()


__all__ = ["BufferRegistryImpl", "RingSensorBuffer"]
