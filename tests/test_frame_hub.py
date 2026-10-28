"""FrameHub 单测（V3.0.0）。

覆盖：push/latest 语义、未知流与空帧忽略、seq 单调、fps 滑动窗口、
时钟回退保护、并发推取、clear 重置、Prometheus 打点。
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from hunter_sim.common.metrics import SENSOR_FRAMES_TOTAL
from hunter_sim.sensor_sim.frame_hub import (
    KNOWN_STREAMS,
    STREAM_DEPTH,
    STREAM_IMU,
    STREAM_LIDAR,
    STREAM_RGB,
    Frame,
    FrameHub,
)


def _payload(text: str = "x") -> bytes:
    return text.encode("utf-8")


class TestPushAndLatest:
    def test_push_returns_frame_with_incrementing_seq(self) -> None:
        hub = FrameHub()

        first = hub.push(STREAM_RGB, 100.0, _payload("a"))
        second = hub.push(STREAM_RGB, 100.1, _payload("b"))

        assert first is not None and second is not None
        assert (first.seq, second.seq) == (1, 2)
        assert first.stream == STREAM_RGB
        assert first.ts == pytest.approx(100.0)
        assert first.payload == b"a"
        assert first.size == 1

    def test_latest_keeps_only_newest_frame(self) -> None:
        hub = FrameHub()
        hub.push(STREAM_RGB, 1.0, _payload("old"))
        hub.push(STREAM_RGB, 2.0, _payload("new"))

        latest = hub.latest(STREAM_RGB)

        assert latest is not None
        assert latest.payload == b"new"
        assert hub.count(STREAM_RGB) == 2  # 计数保留全量，帧只留最新

    def test_latest_unknown_or_empty_stream_returns_none(self) -> None:
        hub = FrameHub()

        assert hub.latest(STREAM_LIDAR) is None
        assert hub.latest("not_a_stream") is None
        assert hub.count("not_a_stream") == 0

    def test_unknown_stream_is_ignored(self) -> None:
        hub = FrameHub()

        assert hub.push("thermal", 1.0, _payload()) is None
        assert hub.push("", 1.0, _payload()) is None
        assert hub.stats()["total"] == 0

    def test_empty_payload_is_ignored(self) -> None:
        hub = FrameHub()

        assert hub.push(STREAM_RGB, 1.0, b"") is None
        assert hub.latest(STREAM_RGB) is None

    def test_stream_name_is_normalized(self) -> None:
        hub = FrameHub()

        assert hub.push("  RGB ", 1.0, _payload()) is not None
        assert hub.latest("rgb") is not None
        assert hub.count("RgB") == 1

    def test_meta_is_copied(self) -> None:
        hub = FrameHub()
        meta: dict[str, Any] = {"width": 1280}

        frame = hub.push(STREAM_RGB, 1.0, _payload(), meta)
        meta["width"] = 999

        assert frame is not None
        assert frame.meta == {"width": 1280}

    def test_frame_is_immutable(self) -> None:
        frame = Frame(stream=STREAM_RGB, ts=0.0, seq=1, payload=b"x")

        with pytest.raises(Exception):
            frame.seq = 2  # type: ignore[misc]

    def test_streams_are_isolated(self) -> None:
        hub = FrameHub()
        for name in KNOWN_STREAMS:
            hub.push(name, 1.0, _payload(name))

        for name in KNOWN_STREAMS:
            frame = hub.latest(name)
            assert frame is not None
            assert frame.payload == _payload(name)
            assert frame.seq == 1


class TestStats:
    def test_stats_reports_per_stream_and_total(self) -> None:
        hub = FrameHub()
        hub.push(STREAM_RGB, 1.0, _payload())
        hub.push(STREAM_RGB, 2.0, _payload())
        hub.push(STREAM_LIDAR, 1.0, _payload())

        stats = hub.stats()

        assert stats[STREAM_RGB] == 2
        assert stats[STREAM_LIDAR] == 1
        assert stats[STREAM_DEPTH] == 0
        assert stats[STREAM_IMU] == 0
        assert stats["total"] == 3

    def test_clear_resets_frames_and_counters(self) -> None:
        hub = FrameHub()
        hub.push(STREAM_RGB, 1.0, _payload())
        hub.push(STREAM_RGB, 1.1, _payload())

        hub.clear()

        assert hub.latest(STREAM_RGB) is None
        assert hub.count(STREAM_RGB) == 0
        assert hub.stats()["total"] == 0
        # 清空后 seq 从 1 重新开始
        frame = hub.push(STREAM_RGB, 2.0, _payload())
        assert frame is not None and frame.seq == 1

    def test_metric_counter_increments(self) -> None:
        hub = FrameHub()
        before = SENSOR_FRAMES_TOTAL.labels(stream=STREAM_DEPTH)._value.get()

        hub.push(STREAM_DEPTH, 1.0, _payload())
        hub.push(STREAM_DEPTH, 2.0, _payload())

        after = SENSOR_FRAMES_TOTAL.labels(stream=STREAM_DEPTH)._value.get()
        assert after - before == 2


class TestFps:
    def test_fps_matches_push_rate(self) -> None:
        hub = FrameHub()
        # 10Hz × 期间 2s（21 帧）
        for index in range(21):
            hub.push(STREAM_RGB, 100.0 + index * 0.1, _payload())

        assert hub.fps(STREAM_RGB) == pytest.approx(10.0, rel=1e-3)

    def test_fps_requires_two_samples(self) -> None:
        hub = FrameHub()

        assert hub.fps(STREAM_RGB) == 0.0
        hub.push(STREAM_RGB, 1.0, _payload())
        assert hub.fps(STREAM_RGB) == 0.0

    def test_fps_unknown_stream_is_zero(self) -> None:
        assert FrameHub().fps("thermal") == 0.0

    def test_fps_window_drops_old_samples(self) -> None:
        hub = FrameHub(fps_window_s=1.0)
        # 早期一组 100Hz 样本，窗口外应被剔除
        for index in range(50):
            hub.push(STREAM_IMU, 10.0 + index * 0.01, _payload())
        # 之后一组 2Hz 样本
        for index in range(5):
            hub.push(STREAM_IMU, 20.0 + index * 0.5, _payload())

        assert hub.fps(STREAM_IMU) == pytest.approx(2.0, rel=1e-3)

    def test_clock_rollback_is_not_sampled(self) -> None:
        hub = FrameHub()
        hub.push(STREAM_RGB, 10.0, _payload())
        hub.push(STREAM_RGB, 10.1, _payload())
        # 时间戳回退：不入窗，且不产生负 fps
        hub.push(STREAM_RGB, 5.0, _payload())

        fps = hub.fps(STREAM_RGB)

        assert fps == pytest.approx(10.0, rel=1e-3)
        assert fps > 0.0


class TestConcurrency:
    def test_concurrent_push_and_latest(self) -> None:
        hub = FrameHub()
        writers, readers = 4, 2
        per_writer = 200
        errors: list[BaseException] = []

        def write(worker: int) -> None:
            try:
                for index in range(per_writer):
                    hub.push(STREAM_RGB, float(worker * 1000 + index), _payload())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def read() -> None:
            try:
                for _ in range(per_writer):
                    frame = hub.latest(STREAM_RGB)
                    if frame is not None:
                        assert isinstance(frame.payload, bytes)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
        threads += [threading.Thread(target=read) for _ in range(readers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        assert hub.count(STREAM_RGB) == writers * per_writer
        latest = hub.latest(STREAM_RGB)
        assert latest is not None
        assert latest.seq == writers * per_writer
