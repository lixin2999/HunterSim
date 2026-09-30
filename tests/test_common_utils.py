"""RingBuffer 和工具函数单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import threading
import time

import pytest

from hunter_sim.common.utils import RingBuffer, normalize_angle_rad, smoothstep


class TestRingBuffer:
    """RingBuffer 功能测试。"""

    def test_push_and_latest(self, ring_buffer: RingBuffer[int]) -> None:
        """测试基本 push 和 latest 操作。"""
        ring_buffer.push(1)
        ring_buffer.push(2)
        ring_buffer.push(3)
        assert ring_buffer.latest() == 3

    def test_overflow_keeps_latest(self) -> None:
        """超出容量时只保留最新 N 帧。"""
        buf: RingBuffer[int] = RingBuffer(max_size=3)
        for i in range(10):
            buf.push(i)
        frames = buf.get_range(5)
        assert len(frames) <= 3
        assert frames[-1] == 9

    def test_empty_latest_returns_none(self) -> None:
        """空缓冲区 latest() 返回 None。"""
        buf: RingBuffer[int] = RingBuffer(max_size=5)
        assert buf.latest() is None

    def test_thread_safety(self) -> None:
        """多线程并发写入时不崩溃。"""
        buf: RingBuffer[int] = RingBuffer(max_size=100)
        errors: list[Exception] = []

        def writer(start: int) -> None:
            try:
                for i in range(100):
                    buf.push(start + i)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i * 1000,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert not errors
        assert buf.latest() is not None

    def test_len(self) -> None:
        buf: RingBuffer[int] = RingBuffer(max_size=5)
        assert buf.size == 0
        buf.push(42)
        assert buf.size == 1


class TestNormalizeAngleRad:
    """角度归一化测试。"""

    @pytest.mark.parametrize(
        ("input_angle", "expected"),
        [
            (0.0, 0.0),
            (3.1416, pytest.approx(-3.14159, abs=0.01)),  # 略大於 pi -> 接近 -pi
            (-3.14159, pytest.approx(-3.14159, abs=0.01)),
            (2 * 3.14159, pytest.approx(0.0, abs=0.01)),
            (4.0, pytest.approx(4.0 - 2 * 3.14159, abs=0.01)),
        ],
    )
    def test_normalize(self, input_angle: float, expected: float) -> None:
        result = normalize_angle_rad(input_angle)
        assert result == expected
        assert -3.1416 <= result <= 3.1416


class TestSmoothstep:
    """Hermite 平滑插值测试。"""

    def test_boundaries(self) -> None:
        assert smoothstep(0.0) == pytest.approx(0.0, abs=1e-6)
        assert smoothstep(1.0) == pytest.approx(1.0, abs=1e-6)

    def test_midpoint(self) -> None:
        result = smoothstep(0.5)
        assert 0.4 < result < 0.6  # 约 0.5

    def test_monotonic(self) -> None:
        prev = -1.0
        for t in [0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0]:
            val = smoothstep(t)
            assert val >= prev
            prev = val
