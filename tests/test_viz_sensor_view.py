"""深度预览渲染单测（V3.5.5：抽样先行 + 并发闸 + 上一帧缓存）。

回归背景（现场「主视区卡顿」归因）：1280x720 的 32FC1 深度一帧有 92 万个
float32，旧实现**先对全量做 ``nan_to_num`` + 归一化、再跨步抽样**，单次请求的
numpy 运算量与输出尺寸无关；这些运算与环视编码跑在同一个核上（GIL + 单线程
编码），深度预览一勾选，环视画面就跟不上下集 —— 表现为「面板越用越卡」。

本文件锁住三件事：抽样必须发生在逐像素运算之前、同一帧不重复编码、
编码不排队（后来者拿上一帧顶住，绝不阻塞等待）。
"""

from __future__ import annotations

import struct
from typing import Any, Callable, Iterator, Optional

import numpy as np
import pytest

from hunter_sim.sensor_sim.frame_hub import STREAM_DEPTH, Frame
from hunter_sim.visualization import sensor_view

_RAW_W = 1280
_RAW_H = 720
_TARGET_W = 480
# 跨步抽样后的期望尺寸：step=ceil(1280/480)=3 → 720/3=240 行、ceil(1280/3)=427 列
_SAMPLED = (240, 427)


def _depth_frame(width: int = _RAW_W, height: int = _RAW_H, seq: int = 1) -> Frame:
    """构造米制深度帧（0~60 m 线性分布，含超远值用于验证截断路径）。"""
    values = np.linspace(0.0, 60.0, width * height, dtype=np.float32).reshape(height, width)
    return Frame(
        stream=STREAM_DEPTH,
        ts=1.0,
        seq=seq,
        payload=values.astype("<f4").tobytes(),
        meta={"width": width, "height": height, "encoding": "32FC1"},
    )


def _png_size(png: bytes) -> tuple[int, int]:
    """从 IHDR 读 PNG 宽高（不依赖图像库）。"""
    return tuple(struct.unpack(">II", png[16:24]))  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def clean_depth_cache() -> Iterator[Callable[[], None]]:
    """深度预览缓存是模块级状态：用例间必须复位，否则命中/让路判断互相污染。"""

    def _reset() -> None:
        with sensor_view._DEPTH_CACHE_LOCK:  # noqa: SLF001
            sensor_view._DEPTH_CACHE.update({"key": "", "seq": -1, "png": b""})  # noqa: SLF001

    _reset()
    yield _reset
    _reset()


class TestSamplingBeforeNormalize:
    def test_only_sampled_grid_reaches_pixel_math(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """逐像素运算（nan_to_num）的入参必须是抽样后的栅格，而不是原始大图。"""
        shapes: list[tuple[int, ...]] = []
        original = np.nan_to_num

        def spy(array: Any, *args: Any, **kwargs: Any) -> Any:
            shapes.append(tuple(np.shape(array)))
            return original(array, *args, **kwargs)

        monkeypatch.setattr(np, "nan_to_num", spy)

        png = sensor_view.depth_png(_depth_frame(), max_width=_TARGET_W)

        assert png
        assert shapes == [_SAMPLED], "抽样未前置：全量 92 万点仍参与逐像素运算"

    def test_output_is_sampled_resolution(self) -> None:
        """输出尺寸即跨步抽样结果（主视区预览不需要原始大图精度）。"""
        png = sensor_view.depth_png(_depth_frame(), max_width=_TARGET_W)

        assert png is not None
        assert _png_size(png) == (_SAMPLED[1], _SAMPLED[0])

    def test_no_target_width_keeps_full_grid(self) -> None:
        """max_width=0 时不抽样（调用方要原尺寸，不能悄悄降质）。"""
        png = sensor_view.depth_png(_depth_frame(width=8, height=4), max_width=0)

        assert png is not None
        assert _png_size(png) == (8, 4)

    def test_undersized_payload_returns_none(self) -> None:
        frame = Frame(
            stream=STREAM_DEPTH,
            ts=1.0,
            seq=1,
            payload=b"\x00" * 16,
            meta={"width": 64, "height": 32},
        )

        assert sensor_view.depth_png(frame, max_width=0) is None

    def test_missing_dimensions_returns_none(self) -> None:
        frame = Frame(stream=STREAM_DEPTH, ts=1.0, seq=1, payload=b"\x00" * 16, meta={})

        assert sensor_view.depth_png(frame, max_width=0) is None


class TestCacheAndGate:
    def _spy_encode(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """统计真实编码次数（保留原实现，只观察不改行为）。"""
        calls: list[str] = []
        original: Callable[..., Optional[bytes]] = sensor_view._encode_depth  # noqa: SLF001

        def spy(np_module: Any, frame: Frame, width: int, height: int,
                max_width: int, key: str) -> Optional[bytes]:
            calls.append(key)
            return original(np_module, frame, width, height, max_width, key)

        monkeypatch.setattr(sensor_view, "_encode_depth", spy)  # noqa: SLF001
        return calls

    def test_same_frame_is_served_from_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """前端 1.5s 轮询快于深度出帧：同一 seq 重复请求不得重复编码。"""
        calls = self._spy_encode(monkeypatch)
        frame = _depth_frame(seq=7)

        first = sensor_view.depth_png(frame, max_width=_TARGET_W)
        second = sensor_view.depth_png(frame, max_width=_TARGET_W)

        assert first == second
        assert calls == [f"{_RAW_W}x{_RAW_H}@{_TARGET_W}"]

    def test_new_frame_recomputes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._spy_encode(monkeypatch)

        sensor_view.depth_png(_depth_frame(width=64, height=32, seq=1), max_width=32)
        sensor_view.depth_png(_depth_frame(width=64, height=32, seq=2), max_width=32)

        assert calls == ["64x32@32", "64x32@32"]

    def test_gate_busy_serves_previous_png(self) -> None:
        """已有编码在跑：后来者拿上一帧顶住，不排队、不阻塞（编码线程互不饿死）。"""
        cached = sensor_view.depth_png(_depth_frame(width=64, height=32, seq=1), max_width=0)
        assert cached

        gate = sensor_view._DEPTH_COMPUTE_GATE  # noqa: SLF001
        assert gate.acquire(blocking=False)
        try:
            assert sensor_view.depth_png(
                _depth_frame(width=64, height=32, seq=3), max_width=0
            ) == cached
        finally:
            gate.release()

    def test_gate_busy_without_cache_returns_none(self) -> None:
        """首张还在算：无上一帧可给，只能返回 None（由路由转 503，不拖慢请求线程）。"""
        gate = sensor_view._DEPTH_COMPUTE_GATE  # noqa: SLF001
        assert gate.acquire(blocking=False)
        try:
            assert sensor_view.depth_png(_depth_frame(width=64, height=32), max_width=0) is None
        finally:
            gate.release()

    def test_cache_is_keyed_by_output_size(self) -> None:
        """尺寸不同的请求不得复用上一帧（拉伸显示会误导画面判读）。"""
        assert sensor_view.depth_png(_depth_frame(width=64, height=32, seq=1), max_width=0)
        gate = sensor_view._DEPTH_COMPUTE_GATE  # noqa: SLF001
        assert gate.acquire(blocking=False)
        try:
            assert sensor_view.depth_png(_depth_frame(width=64, height=32, seq=2), max_width=16) is None
        finally:
            gate.release()


class TestRenderStreamDispatch:
    def test_depth_goes_through_preview_renderer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``render_stream`` 的深度分支必须走同一套抽样/缓存实现（单一口径）。"""
        monkeypatch.setattr(
            sensor_view, "depth_png", lambda frame, max_width=480: b"\x89PNG-cached"
        )

        body, media_type = sensor_view.render_stream(_depth_frame(width=8, height=4))

        assert body == b"\x89PNG-cached"
        assert media_type == "image/png"

    def test_unrenderable_depth_yields_empty_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sensor_view, "depth_png", lambda frame, max_width=480: None)

        body, media_type = sensor_view.render_stream(_depth_frame(width=8, height=4))

        assert body == b""
        assert media_type == "image/png"
