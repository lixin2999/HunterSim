"""模块 3.2：多传感器时间同步。

以参考传感器（通常最低频的相机或 LiDAR）的帧时刻为锚点，为每个数据流用**二分查找**
匹配容差内时间最近的样本，聚合主车状态，产出 :class:`~hunter_sim.core.contracts.SynchronizedFrame`。

采用最近邻匹配（零阶保持），锚点若无容差内的主车状态则跳过该时刻。
"""

from __future__ import annotations

from bisect import bisect_left
from math import inf

from hunter_sim.core.contracts import (
    CameraFrame,
    GnssFrame,
    ImuFrame,
    LidarFrame,
    RadarFrame,
    SynchronizedFrame,
    TimestampedData,
    VehicleState,
)

_Buckets = tuple[
    dict[str, CameraFrame],
    dict[str, LidarFrame],
    dict[str, RadarFrame],
    dict[str, ImuFrame],
    dict[str, GnssFrame],
]


class SynchronizerImpl:
    """满足 :class:`~hunter_sim.processing.protocols.Synchronizer` 契约。"""

    def __init__(self, *, tolerance_seconds: float = 0.05) -> None:
        """初始化同步器。

        Args:
            tolerance_seconds: 最近邻匹配的时间容差（秒），须为正数。
        """
        if tolerance_seconds <= 0:
            raise ValueError(f"容差必须为正数: {tolerance_seconds}")
        self._tolerance = tolerance_seconds

    def synchronize(
        self,
        streams: dict[str, list[TimestampedData]],
        *,
        reference: str,
        vehicle_states: list[VehicleState],
    ) -> list[SynchronizedFrame]:
        """按参考传感器锚点对齐各流，返回升序同步帧列表。"""
        if reference not in streams:
            raise ValueError(f"参考传感器 {reference} 不在数据流中")

        sorted_streams = {
            sid: sorted(frames, key=lambda f: f.timestamp) for sid, frames in streams.items()
        }
        keys = {sid: [f.timestamp for f in frames] for sid, frames in sorted_streams.items()}
        vs_sorted = sorted(vehicle_states, key=lambda s: s.timestamp)
        vs_keys = [s.timestamp for s in vs_sorted]

        anchors = sorted_streams[reference]
        result: list[SynchronizedFrame] = []
        for anchor in anchors:
            t = anchor.timestamp
            state_index = self._nearest_index(vs_keys, t)
            if state_index is None:
                continue  # 无容差内主车状态，跳过该锚点
            cameras: dict[str, CameraFrame] = {}
            lidars: dict[str, LidarFrame] = {}
            radars: dict[str, RadarFrame] = {}
            imus: dict[str, ImuFrame] = {}
            gnss: dict[str, GnssFrame] = {}
            buckets: _Buckets = (cameras, lidars, radars, imus, gnss)
            for sid, frames in sorted_streams.items():
                if sid == reference:
                    chosen: TimestampedData | None = anchor
                else:
                    chosen = self._nearest(frames, keys[sid], t)
                if chosen is not None:
                    self._route(chosen, buckets)
            result.append(
                SynchronizedFrame(
                    timestamp=t,
                    frame_id=anchor.frame_id,
                    vehicle_state=vs_sorted[state_index],
                    cameras=cameras,
                    lidars=lidars,
                    radars=radars,
                    imus=imus,
                    gnss=gnss,
                )
            )
        return result

    def _nearest_index(self, keys: list[float], t: float) -> int | None:
        """在升序时间戳列表中找与 ``t`` 最近且不超过容差的索引，无则 ``None``。"""
        if not keys:
            return None
        pos = bisect_left(keys, t)
        best_index: int | None = None
        best_diff = inf
        for candidate in (pos - 1, pos):
            if 0 <= candidate < len(keys):
                diff = abs(keys[candidate] - t)
                if diff < best_diff:
                    best_diff = diff
                    best_index = candidate
        if best_index is not None and best_diff <= self._tolerance:
            return best_index
        return None

    def _nearest(
        self, frames: list[TimestampedData], keys: list[float], t: float
    ) -> TimestampedData | None:
        """返回容差内时间最近的帧，无则 ``None``。"""
        index = self._nearest_index(keys, t)
        return None if index is None else frames[index]

    @staticmethod
    def _route(frame: TimestampedData, buckets: _Buckets) -> None:
        """按具体类型把帧放入对应传感器字典桶。"""
        cameras, lidars, radars, imus, gnss = buckets
        if isinstance(frame, CameraFrame):
            cameras[frame.sensor_id] = frame
        elif isinstance(frame, LidarFrame):
            lidars[frame.sensor_id] = frame
        elif isinstance(frame, RadarFrame):
            radars[frame.sensor_id] = frame
        elif isinstance(frame, ImuFrame):
            imus[frame.sensor_id] = frame
        elif isinstance(frame, GnssFrame):
            gnss[frame.sensor_id] = frame


__all__ = ["SynchronizerImpl"]
