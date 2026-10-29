"""VIL 帧对齐器（V3.0.0）。

职责：把 10–50Hz 到达的实车帧规约到固定仿真时间栅格（默认 10Hz），
并在栅格时刻上做时间对齐插值，消除「数据到达抖动 → 位姿跳变」。

处理流程（``next()`` 每次 tick 调用一次）：

1. ``grid_index = floor(now * target_hz)``；与上次相同 → 返回 ``None``
   （本 tick 不推进，实现**降采样**，50Hz 输入下只有 1/5 的 tick 会改车辆位姿）；
2. 缓冲区中同时存在左右两帧（``t_i <= grid_time <= t_{i+1}``）→ 位置/速度/舵角
   线性插值、航向按最短弧插值，``mode="interpolated"``；
3. 只有左侧帧且偏差 <= ``max_extrapolation_ms`` → 交给 ``PoseExtrapolator``
   按精确 dt 做匀速圆周外推，``mode="extrapolated"``；
4. 其余情况（首帧、超窗外、缓冲区无左帧、时钟回退）→ 取最近帧，
   ``mode="nearest"``，并把偏差记入 ``skew_ms``。

时钟域：``now`` 是仿真端系统时间，``TelemetryFrame.timestamp`` 是实车上报时刻，
两者可能不同步。开启 ``clock_offset_auto`` 时，用最近窗口内
``receive_time - timestamp`` 的最小值作为偏置估计，把系统时间映射到实车时钟域
（默认关闭，保持既有对齐行为）。

指标：每次推进都会对 ``huntersim_vil_frames_downsampled_total{mode}`` 计数，
并把 ``skew_ms`` 写入 ``huntersim_vil_align_skew_ms`` 直方图（P95 用于判质量）。
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Literal, Optional

from hunter_sim.common.metrics import observe_vil_alignment
from hunter_sim.common.models import Transform, VehicleState
from hunter_sim.common.utils import get_logger, normalize_angle_rad
from hunter_sim.vil_mapper.pose_extrapolator import PoseExtrapolator
from hunter_sim.vil_mapper.telemetry_buffer import TelemetryBuffer, TelemetryFrame

logger = get_logger(__name__)

AlignMode = Literal["interpolated", "extrapolated", "nearest"]

# 时间比较容差（秒）：小于该值视为同一时刻，避免除零与浮点抖动
_TIME_EPS = 1e-6
# 时钟偏置估计：样本窗口、最小样本数与可信范围（超过 1 小时的差值视为未校准）
_OFFSET_WINDOW = 30
_MIN_OFFSET_SAMPLES = 3
_MAX_CLOCK_OFFSET_S = 3600.0


@dataclass(frozen=True)
class AlignedFrame:
    """对齐到仿真栅格的一帧数据。

    Attributes:
        frame: 对齐后的遥测帧（``timestamp`` 已被改写为栅格时刻）。
        mode: 对齐模式。
        skew_ms: 栅格时刻与所用最近原始帧时刻的偏差（毫秒）。
        grid_index: 10Hz 栅格序号（``floor(grid_time * target_hz)``）。
    """

    frame: TelemetryFrame
    mode: AlignMode
    skew_ms: float
    grid_index: int


@dataclass
class AlignerStats:
    """对齐器运行统计（只读快照用）。

    Attributes:
        emitted: 已推进的栅格总数。
        skipped: 因同一栅格被跳过的 tick 数（降采样收益）。
        nearest: 退化为取最近帧的次数。
        extrapolated: 外推推进次数。
        interpolated: 插值推进次数。
        last_skew_ms: 最近一次对齐偏差（毫秒）。
        clock_offset_ms: 当前时钟偏置估计（毫秒，未开启自动补偿时为 0）。
    """

    emitted: int = 0
    skipped: int = 0
    nearest: int = 0
    extrapolated: int = 0
    interpolated: int = 0
    last_skew_ms: float = 0.0
    clock_offset_ms: float = 0.0


class FrameAligner:
    """把实车帧对齐到固定仿真栅格（线程安全）。

    Args:
        buffer: 遥测数据缓冲区（提供最近 N 帧）。
        target_hz: 栅格频率（Hz），默认 10，需 > 0。
        max_extrapolation_ms: 单侧外推的最大偏差（毫秒）。
        extrapolator: 位姿外推器；None 时按 ``max_extrapolation_ms`` 自建。
        clock_offset_auto: 是否自动估计并补偿实车/仿真端时钟偏置（默认关闭）。

    Raises:
        ValueError: ``target_hz`` 非正时抛出。
    """

    def __init__(
        self,
        buffer: TelemetryBuffer,
        target_hz: float = 10.0,
        max_extrapolation_ms: int = 150,
        extrapolator: Optional[PoseExtrapolator] = None,
        clock_offset_auto: bool = False,
    ) -> None:
        if target_hz <= 0:
            raise ValueError("target_hz must be positive")
        self._buffer = buffer
        self._target_hz = float(target_hz)
        self._max_extrapolation_s = max(0, max_extrapolation_ms) / 1000.0
        self._extrapolator = extrapolator or PoseExtrapolator(max_extrapolation_ms)
        # 内建自己的外推器时，其外推窗口仅作兜底；实际外推按精确 dt 传入。
        self._lock = threading.Lock()
        self._last_grid_index: Optional[int] = None
        self._last_now: Optional[float] = None
        self._stats = AlignerStats()
        # 时钟偏置估计（仅 clock_offset_auto 时采样）
        self._clock_offset_auto = bool(clock_offset_auto)
        self._offset_samples: deque[float] = deque(maxlen=_OFFSET_WINDOW)
        self._clock_offset_s = 0.0
        self._last_offset_sample: Optional[tuple[float, float]] = None
        logger.info(
            f"FrameAligner initialized (target_hz={target_hz}, "
            f"max_extrapolation_ms={max_extrapolation_ms}, "
            f"clock_offset_auto={self._clock_offset_auto})"
        )

    @property
    def target_hz(self) -> float:
        """栅格频率（Hz）。"""
        return self._target_hz

    @property
    def stats(self) -> AlignerStats:
        """当前统计快照（只读副本）。"""
        with self._lock:
            return AlignerStats(
                emitted=self._stats.emitted,
                skipped=self._stats.skipped,
                nearest=self._stats.nearest,
                extrapolated=self._stats.extrapolated,
                interpolated=self._stats.interpolated,
                last_skew_ms=self._stats.last_skew_ms,
                clock_offset_ms=self._stats.clock_offset_ms,
            )

    def reset(self) -> None:
        """重置栅格进度与统计（场景重启/断流恢复时调用）。"""
        with self._lock:
            self._last_grid_index = None
            self._last_now = None
            self._stats = AlignerStats()
            self._offset_samples.clear()
            self._clock_offset_s = 0.0
            self._last_offset_sample = None
        logger.info("FrameAligner reset")

    def next(self, now: float) -> Optional[AlignedFrame]:
        """推进到当前栅格，返回对齐帧（同一栅格内返回 ``None``）。

        Args:
            now: 当前时刻（Unix 秒）。开启 ``clock_offset_auto`` 时由对齐器
                自动换算到 ``TelemetryFrame.timestamp`` 的时钟域。

        Returns:
            对齐帧；本 tick 无需推进或缓冲区为空时返回 ``None``。
        """
        frames = self._buffer.get_last_n(self._buffer.max_frames)
        clock_now = self._compensate_clock(now, frames) if self._clock_offset_auto else now
        with self._lock:
            # 时钟回退保护：进程时钟被外部校正时不冻结栅格，直接重开一轮
            if self._last_now is not None and clock_now < self._last_now - 1.0:
                logger.warning(
                    f"FrameAligner clock moved backwards ({clock_now:.3f} < "
                    f"{self._last_now:.3f}), reset grid"
                )
                self._last_grid_index = None
            self._last_now = clock_now

            grid_index = int(math.floor(clock_now * self._target_hz))
            if self._last_grid_index == grid_index:
                self._stats.skipped += 1
                return None
            # 先占位：即使本次取帧失败也不在同一栅格上反复重试
            self._last_grid_index = grid_index
            self._stats.clock_offset_ms = self._clock_offset_s * 1000.0

        grid_time = grid_index / self._target_hz
        if not frames:
            return None

        aligned = self._align(frames, grid_time, grid_index)
        if aligned is None:
            return None

        observe_vil_alignment(aligned.mode, aligned.skew_ms)
        with self._lock:
            self._stats.emitted += 1
            self._stats.last_skew_ms = aligned.skew_ms
            if aligned.mode == "interpolated":
                self._stats.interpolated += 1
            elif aligned.mode == "extrapolated":
                self._stats.extrapolated += 1
            else:
                self._stats.nearest += 1
        return aligned

    # ── 内部实现 ─────────────────────────────────────────────────────────────

    def _compensate_clock(self, now: float, frames: list[TelemetryFrame]) -> float:
        """估计并补偿实车/仿真端时钟偏置，把系统时间映射到实车时钟域。

        实现：每帧采样 ``receive_time - timestamp``（传输+处理延迟），取最近
        ``_OFFSET_WINDOW`` 个样本的**最小值**作为偏置估计（最小延迟样本最能
        代表两时钟的真实差值）；样本不足 ``_MIN_OFFSET_SAMPLES`` 或差值超过
        ``_MAX_CLOCK_OFFSET_S``（未校准的时间戳）时不做补偿。
        """
        if not frames:
            return now
        latest = frames[-1]
        if latest.timestamp <= 0 or latest.receive_time <= 0:
            return now
        key = (latest.timestamp, latest.receive_time)
        delta = latest.receive_time - latest.timestamp
        if not math.isfinite(delta) or abs(delta) > _MAX_CLOCK_OFFSET_S:
            return now
        with self._lock:
            if self._last_offset_sample != key:
                self._last_offset_sample = key
                self._offset_samples.append(delta)
                # 样本不足时不写偏置：首帧抖动不能悄悄搬动栅格
                if len(self._offset_samples) >= _MIN_OFFSET_SAMPLES:
                    self._clock_offset_s = min(self._offset_samples)
            offset = self._clock_offset_s
            enough = len(self._offset_samples) >= _MIN_OFFSET_SAMPLES
        if not enough:
            return now
        return now - offset

    def _align(
        self, frames: list[TelemetryFrame], grid_time: float, grid_index: int
    ) -> Optional[AlignedFrame]:
        """在给定帧序列上求栅格时刻的对齐结果。"""
        left: Optional[TelemetryFrame] = None
        right: Optional[TelemetryFrame] = None
        for frame in frames:
            if frame.timestamp <= grid_time:
                if left is None or frame.timestamp > left.timestamp:
                    left = frame
            elif right is None or frame.timestamp < right.timestamp:
                right = frame

        nearest = self._nearest(frames, grid_time)
        if nearest is None:  # pragma: no cover  frames 非空时不可能命中
            return None

        # 双侧插值
        if left is not None and right is not None:
            span = right.timestamp - left.timestamp
            if span > _TIME_EPS:
                alpha = (grid_time - left.timestamp) / span
                alpha = min(1.0, max(0.0, alpha))
                state = self._interpolate(left.vehicle_state, right.vehicle_state, alpha)
                source = left if alpha < 0.5 else right
                return AlignedFrame(
                    frame=self._compose(source, state, grid_time),
                    mode="interpolated",
                    skew_ms=self._skew_ms(grid_time, left.timestamp, right.timestamp),
                    grid_index=grid_index,
                )

        # 单侧外推
        if left is not None:
            dt = grid_time - left.timestamp
            if 0.0 < dt <= self._max_extrapolation_s:
                state = left.vehicle_state
                transform = self._extrapolator.extrapolate_transform(
                    state.transform,
                    state.vehicle_speed,
                    state.angular_velocity[2],
                    dt=dt,
                )
                extrapolated = state.model_copy(update={"transform": transform})
                return AlignedFrame(
                    frame=self._compose(left, extrapolated, grid_time),
                    mode="extrapolated",
                    skew_ms=dt * 1000.0,
                    grid_index=grid_index,
                )

        # 退化：取最近帧
        skew = abs(nearest.timestamp - grid_time) * 1000.0
        return AlignedFrame(
            frame=self._compose(nearest, nearest.vehicle_state, grid_time),
            mode="nearest",
            skew_ms=skew,
            grid_index=grid_index,
        )

    @staticmethod
    def _nearest(
        frames: list[TelemetryFrame], grid_time: float
    ) -> Optional[TelemetryFrame]:
        """取时间上最接近栅格时刻的帧。"""
        if not frames:
            return None
        return min(frames, key=lambda f: abs(f.timestamp - grid_time))

    @staticmethod
    def _skew_ms(grid_time: float, left_ts: float, right_ts: float) -> float:
        """插值模式的对齐误差：栅格时刻到最近原始帧的偏差。"""
        return min(grid_time - left_ts, right_ts - grid_time) * 1000.0

    @staticmethod
    def _compose(
        source: TelemetryFrame, state: VehicleState, grid_time: float
    ) -> TelemetryFrame:
        """以 ``source`` 为模板生成栅格时刻的新帧（不改原帧）。"""
        return TelemetryFrame(
            vehicle_id=source.vehicle_id,
            timestamp=grid_time,
            receive_time=source.receive_time,
            vehicle_state=state,
            perception=source.perception,
            control_cmd=source.control_cmd,
            is_fresh=source.is_fresh,
            calibration=source.calibration,
            source_seq=source.source_seq,
            forwarded_at=source.forwarded_at,
        )

    @classmethod
    def _interpolate(
        cls, a: VehicleState, b: VehicleState, alpha: float
    ) -> VehicleState:
        """两帧车辆状态线性插值（航向按最短弧）。"""
        return a.model_copy(
            update={
                "time_stamp": a.time_stamp + (b.time_stamp - a.time_stamp) * alpha,
                "transform": cls._interpolate_transform(a.transform, b.transform, alpha),
                "velocity": cls._lerp_tuple(a.velocity, b.velocity, alpha),
                "acceleration": cls._lerp_tuple(a.acceleration, b.acceleration, alpha),
                "angular_velocity": cls._lerp_tuple(
                    a.angular_velocity, b.angular_velocity, alpha
                ),
                "steering": cls._lerp(a.steering, b.steering, alpha),
                "throttle": cls._lerp(a.throttle, b.throttle, alpha),
                "brake": cls._lerp(a.brake, b.brake, alpha),
                "vehicle_speed": cls._lerp(a.vehicle_speed, b.vehicle_speed, alpha),
                # 挡位/灯光是离散量：取时间上更近的一帧，不做数值插值
                "gear": a.gear if alpha < 0.5 else b.gear,
                "lights": dict(a.lights if alpha < 0.5 else b.lights),
            }
        )

    @classmethod
    def _interpolate_transform(
        cls, a: Transform, b: Transform, alpha: float
    ) -> Transform:
        """位姿插值：位置线性，航向最短弧，俯仰/横滚线性。"""
        return Transform(
            x=cls._lerp(a.x, b.x, alpha),
            y=cls._lerp(a.y, b.y, alpha),
            z=cls._lerp(a.z, b.z, alpha),
            pitch=cls._lerp(a.pitch, b.pitch, alpha),
            yaw=normalize_angle_rad(
                a.yaw + normalize_angle_rad(b.yaw - a.yaw) * alpha
            ),
            roll=cls._lerp(a.roll, b.roll, alpha),
        )

    @staticmethod
    def _lerp(a: float, b: float, alpha: float) -> float:
        """标量线性插值。"""
        return a + (b - a) * alpha

    @classmethod
    def _lerp_tuple(
        cls, a: tuple[float, float, float], b: tuple[float, float, float], alpha: float
    ) -> tuple[float, float, float]:
        """三元组线性插值。"""
        return (
            cls._lerp(a[0], b[0], alpha),
            cls._lerp(a[1], b[1], alpha),
            cls._lerp(a[2], b[2], alpha),
        )


__all__ = ["AlignedFrame", "AlignerStats", "FrameAligner"]
