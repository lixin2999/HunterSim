"""FrameAligner 单元测试（V3.0.0）。

覆盖计划 §6.1 的对齐器用例：
- 10Hz 降采样（同一栅格内不重复推进）；
- 双侧线性插值（含航向最短弧）；
- 单侧窗口内外推；
- 超窗/单帧退化为 nearest 并记录 skew；
- ``reset()`` 与时钟回退保护；
- 指标计数（``huntersim_vil_frames_downsampled_total``）。

时间基准使用当前时刻附近的值，避免触发缓冲区超时分支的日志噪音。
"""

from __future__ import annotations

import math
import time

import pytest

from hunter_sim.common.metrics import counter_value
from hunter_sim.common.models import Transform, VehicleState, VILSettings
from hunter_sim.vil_mapper.frame_aligner import FrameAligner
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame


def _frame(
    ts: float,
    *,
    x: float = 0.0,
    yaw: float = 0.0,
    speed: float = 0.0,
    steering: float = 0.0,
    gear: int = 1,
    lights: dict[str, bool] | None = None,
) -> TelemetryFrame:
    """构造一帧遥测数据（仅填对齐器关心的字段）。"""
    state = VehicleState(
        time_stamp=ts,
        transform=Transform(x=x, y=0.0, z=0.0, yaw=yaw),
        velocity=(speed, 0.0, 0.0),
        steering=steering,
        vehicle_speed=speed,
        gear=gear,
        lights=lights or {},
    )
    return TelemetryFrame(
        vehicle_id="veh-1",
        timestamp=ts,
        receive_time=ts,
        vehicle_state=state,
    )


def _buffer(*frames: TelemetryFrame, vil_settings: VILSettings | None = None) -> TelemetryBuffer:
    buffer = TelemetryBuffer(vil_settings=vil_settings) if vil_settings else TelemetryBuffer()
    for frame in frames:
        buffer.push(frame)
    return buffer


def _grid_base() -> float:
    """返回当前时刻所在 10Hz 栅格的起点（网格边界清晰）。"""
    return math.floor(time.time() * 10.0) / 10.0


# ── 构造参数 ──────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_rejects_non_positive_hz(self) -> None:
        with pytest.raises(ValueError, match="target_hz"):
            FrameAligner(TelemetryBuffer(), target_hz=0.0)

    def test_default_target_hz(self) -> None:
        aligner = FrameAligner(TelemetryBuffer())
        assert aligner.target_hz == 10.0


# ── 降采样 ────────────────────────────────────────────────────────────────────


class TestDownsampling:
    def test_same_grid_returns_none(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.04), _frame(base + 0.06))
        aligner = FrameAligner(buffer, target_hz=10.0)

        first = aligner.next(base + 0.05)
        assert first is not None
        # 同一 100ms 栅格内的后续 tick 不再推进（50Hz tick → 每 5 个 tick 才推进一次）
        assert aligner.next(base + 0.06) is None
        assert aligner.next(base + 0.09) is None

    def test_next_grid_advances(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.04), _frame(base + 0.06), _frame(base + 0.16))
        aligner = FrameAligner(buffer, target_hz=10.0)

        assert aligner.next(base + 0.05) is not None
        second = aligner.next(base + 0.15)
        assert second is not None
        assert second.grid_index > 0

    def test_empty_buffer_returns_none(self) -> None:
        assert FrameAligner(TelemetryBuffer(), target_hz=10.0).next(time.time()) is None

    def test_high_rate_input_downsampled_to_grid(self) -> None:
        """50Hz 输入遍历 1 秒 tick，只推进约 10 个栅格。"""
        base = _grid_base()
        buffer = TelemetryBuffer(max_frames=200)
        for i in range(50):
            buffer.push(_frame(base + i * 0.02))
        aligner = FrameAligner(buffer, target_hz=10.0)

        emitted = 0
        for i in range(50):
            if aligner.next(base + i * 0.02) is not None:
                emitted += 1
        assert 8 <= emitted <= 11
        assert aligner.stats.skipped == 50 - emitted


# ── 插值 ──────────────────────────────────────────────────────────────────────


class TestInterpolation:
    def test_linear_interpolation_between_frames(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.04, x=0.0), _frame(base + 0.06, x=10.0))
        aligner = FrameAligner(buffer, target_hz=10.0)

        aligned = aligner.next(base + 0.05)
        assert aligned is not None
        assert aligned.mode == "interpolated"
        assert aligned.frame.vehicle_state.transform.x == pytest.approx(4.0, abs=1e-6)
        # 栅格时间由浮点除法还原，允许 1e-2 ms 量级的舍入偏差
        assert aligned.skew_ms == pytest.approx(40.0, abs=0.01)

    def test_heading_uses_shortest_arc(self) -> None:
        """跨越 ±pi 的航向必须走最短弧（3.0 → -3.0 之间是 0.28rad 而非 -6rad）。"""
        base = _grid_base()
        buffer = _buffer(
            _frame(base - 0.04, yaw=3.0),
            _frame(base + 0.06, yaw=-3.0),
        )
        aligner = FrameAligner(buffer, target_hz=10.0)

        aligned = aligner.next(base + 0.05)
        assert aligned is not None
        yaw = aligned.frame.vehicle_state.transform.yaw
        # 3.0 → -3.0 的最短弧为 +0.283rad，alpha=0.4 时落在 pi 附近
        assert abs(yaw) == pytest.approx(math.pi, abs=0.05)

    def test_scalar_state_is_interpolated(self) -> None:
        base = _grid_base()
        buffer = _buffer(
            _frame(base - 0.04, speed=0.0, steering=0.0),
            _frame(base + 0.06, speed=10.0, steering=1.0),
        )
        aligner = FrameAligner(buffer, target_hz=10.0)

        aligned = aligner.next(base + 0.05)
        assert aligned is not None
        state = aligned.frame.vehicle_state
        assert state.vehicle_speed == pytest.approx(4.0, abs=1e-6)
        assert state.steering == pytest.approx(0.4, abs=1e-6)

    def test_discrete_state_takes_nearest_frame(self) -> None:
        """挡位/灯光是离散量：取时间上更近的一帧，不做数值插值。"""
        base = _grid_base()
        buffer = _buffer(
            _frame(base - 0.04, gear=1, lights={"left_turn": True}),
            _frame(base + 0.06, gear=3, lights={"right_turn": True}),
        )
        aligner = FrameAligner(buffer, target_hz=10.0)

        aligned = aligner.next(base + 0.05)
        assert aligned is not None
        # alpha=0.4 < 0.5 → 取左侧帧
        assert aligned.frame.vehicle_state.gear == 1
        assert aligned.frame.vehicle_state.lights == {"left_turn": True}

    def test_grid_timestamp_overrides_source_timestamp(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.04), _frame(base + 0.06))
        aligner = FrameAligner(buffer, target_hz=10.0)

        aligned = aligner.next(base + 0.05)
        assert aligned is not None
        assert aligned.frame.timestamp == pytest.approx(base, abs=1e-9)


# ── 外推与退化 ────────────────────────────────────────────────────────────────


class TestExtrapolation:
    def test_single_sided_within_window_extrapolates(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.05, x=0.0, speed=10.0))
        aligner = FrameAligner(buffer, target_hz=10.0, max_extrapolation_ms=150)

        aligned = aligner.next(base + 0.049)
        assert aligned is not None
        assert aligned.mode == "extrapolated"
        # 0.05s × 10m/s ≈ 0.5m
        assert aligned.frame.vehicle_state.transform.x == pytest.approx(0.5, abs=0.2)
        assert aligned.skew_ms == pytest.approx(50.0, abs=1.0)

    def test_beyond_window_falls_back_to_nearest(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.5, x=1.0))
        aligner = FrameAligner(buffer, target_hz=10.0, max_extrapolation_ms=150)

        aligned = aligner.next(base + 0.049)
        assert aligned is not None
        assert aligned.mode == "nearest"
        assert aligned.skew_ms == pytest.approx(500.0, abs=10.0)
        assert aligned.frame.vehicle_state.transform.x == pytest.approx(1.0)

    def test_single_future_frame_falls_back_to_nearest(self) -> None:
        """只有右侧帧（网格早于全部数据）时无法外推，取最近帧。"""
        base = _grid_base()
        buffer = _buffer(_frame(base + 0.3))
        aligner = FrameAligner(buffer, target_hz=10.0)

        aligned = aligner.next(base + 0.049)
        assert aligned is not None
        assert aligned.mode == "nearest"


# ── 统计 / 重置 / 时钟保护 ────────────────────────────────────────────────────


class TestStatsAndReset:
    def test_stats_track_modes(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.04), _frame(base + 0.06))
        aligner = FrameAligner(buffer, target_hz=10.0)

        assert aligner.next(base + 0.05) is not None
        stats = aligner.stats
        assert stats.emitted == 1
        assert stats.interpolated == 1
        assert stats.nearest == 0
        assert stats.last_skew_ms == pytest.approx(40.0, abs=0.01)

    def test_metrics_counter_increments(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.04), _frame(base + 0.06))
        aligner = FrameAligner(buffer, target_hz=10.0)

        before = counter_value(_mode_counter(), mode="interpolated")
        aligner.next(base + 0.05)
        assert counter_value(_mode_counter(), mode="interpolated") == before + 1

    def test_reset_allows_same_grid_again(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.04), _frame(base + 0.06))
        aligner = FrameAligner(buffer, target_hz=10.0)

        assert aligner.next(base + 0.05) is not None
        assert aligner.next(base + 0.06) is None
        aligner.reset()
        assert aligner.next(base + 0.07) is not None
        assert aligner.stats.emitted == 1

    def test_clock_backwards_does_not_freeze(self) -> None:
        base = _grid_base()
        buffer = _buffer(_frame(base - 0.04), _frame(base + 0.06))
        aligner = FrameAligner(buffer, target_hz=10.0)

        assert aligner.next(base + 0.05) is not None
        # 时钟被外部校正回退 5s：不得因为栅格序号相同而永久卡住
        assert aligner.next(base - 5.0) is not None


def _mode_counter() -> object:
    """返回 downsample 计数 Counter（避免模块级私有导入散落）。"""
    from hunter_sim.common.metrics import VIL_FRAMES_DOWNSAMPLED_TOTAL  # noqa: PLC0415

    return VIL_FRAMES_DOWNSAMPLED_TOTAL
