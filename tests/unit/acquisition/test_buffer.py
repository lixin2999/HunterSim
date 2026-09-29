"""模块 2.2 数据缓冲的单元测试。"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from hunter_sim.acquisition.buffer import BufferRegistryImpl, RingSensorBuffer
from hunter_sim.core.exceptions import BufferClosedError


def frame(t: float) -> SimpleNamespace:
    """构造仅含 timestamp 的轻量缓冲元素。"""
    return SimpleNamespace(timestamp=float(t), frame_id=int(t))


async def test_put_get_roundtrip() -> None:
    buf = RingSensorBuffer("cam", loop=asyncio.get_running_loop())
    f = frame(1.0)
    await buf.put(f)
    assert buf.size() == 1
    got = await buf.get(timeout=1.0)
    assert got is f
    assert buf.size() == 0


async def test_get_timeout() -> None:
    buf = RingSensorBuffer("cam", loop=asyncio.get_running_loop())
    with pytest.raises(asyncio.TimeoutError):
        await buf.get(timeout=0.01)


async def test_query_range_and_ring_eviction() -> None:
    buf = RingSensorBuffer("lidar", loop=asyncio.get_running_loop(), max_frames=3)
    for t in range(5):
        await buf.put(frame(t))
    assert buf.history_size() == 3  # 环形缓冲仅保留最近 3 帧
    result = buf.query_range(2, 4)
    assert [int(r.timestamp) for r in result] == [2, 3, 4]


async def test_query_range_invalid() -> None:
    buf = RingSensorBuffer("lidar", loop=asyncio.get_running_loop())
    with pytest.raises(ValueError):
        buf.query_range(5, 1)


async def test_backpressure_drop_newest() -> None:
    buf = RingSensorBuffer("cam", loop=asyncio.get_running_loop(), max_queue=2, max_frames=10)
    for t in range(5):
        await buf.put(frame(t))
    assert buf.size() == 2
    assert buf.dropped_count == 3
    assert buf.history_size() == 5  # 环形缓冲仍完整保留


async def test_backpressure_drop_oldest() -> None:
    buf = RingSensorBuffer(
        "cam", loop=asyncio.get_running_loop(), max_queue=2, on_full="drop_oldest"
    )
    for t in range(3):
        await buf.put(frame(t))
    assert buf.size() == 2
    first = await buf.get(timeout=1.0)
    second = await buf.get(timeout=1.0)
    assert (int(first.timestamp), int(second.timestamp)) == (1, 2)


async def test_invalid_on_full() -> None:
    with pytest.raises(ValueError):
        RingSensorBuffer("cam", loop=asyncio.get_running_loop(), on_full="bogus")


async def test_put_from_thread() -> None:
    loop = asyncio.get_running_loop()
    buf = RingSensorBuffer("cam", loop=loop)
    f = frame(7.0)
    worker = threading.Thread(target=lambda: buf.put_from_thread(f))
    worker.start()
    worker.join()
    got = await buf.get(timeout=1.0)
    assert got is f


async def test_close_raises_on_get_and_put() -> None:
    buf = RingSensorBuffer("cam", loop=asyncio.get_running_loop())
    await buf.close()
    with pytest.raises(BufferClosedError):
        await buf.get(timeout=1.0)
    with pytest.raises(BufferClosedError):
        await buf.put(frame(1.0))


async def test_close_wakes_pending_consumer() -> None:
    buf = RingSensorBuffer("cam", loop=asyncio.get_running_loop())
    consumer = asyncio.create_task(buf.get())
    await asyncio.sleep(0)  # 让 consumer 进入等待
    await buf.close()
    with pytest.raises(BufferClosedError):
        await consumer


async def test_put_from_thread_after_close_is_noop() -> None:
    buf = RingSensorBuffer("cam", loop=asyncio.get_running_loop())
    await buf.close()
    buf.put_from_thread(frame(1.0))  # 关闭后不应再接收数据
    await asyncio.sleep(0)
    assert buf.history_size() == 0  # 环形缓冲未被写入
    with pytest.raises(BufferClosedError):
        await buf.get(timeout=1.0)  # 仅剩关闭哨兵


async def test_registry_lifecycle() -> None:
    reg = BufferRegistryImpl(asyncio.get_running_loop())
    b1 = reg.get_or_create("a")
    b2 = reg.get_or_create("a")
    assert b1 is b2
    assert reg.get("missing") is None
    reg.get_or_create("b")
    assert set(reg.sensor_ids()) == {"a", "b"}
    await reg.close_all()
    assert b1.is_closed
