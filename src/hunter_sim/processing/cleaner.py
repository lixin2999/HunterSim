"""模块 3.3：数组型传感器数据清洗去噪。

- LiDAR：去除含 ``NaN``/``Inf`` 的行，可选去除全零（无回波）点与超距点。
- Radar：去除含 ``NaN``/``Inf`` 的点迹。
- 其他帧类型原样返回。

清洗返回**新的不可变帧**（``model_copy(update=...)``），不修改输入，符合契约不可变约定。
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from hunter_sim.core.contracts import LidarFrame, RadarFrame, TimestampedData


class CleanerImpl:
    """满足 :class:`~hunter_sim.processing.protocols.Cleaner` 契约。"""

    def __init__(self, *, max_range: float | None = None, drop_all_zero: bool = True) -> None:
        """初始化为清洗器。

        Args:
            max_range: 仅对 LiDAR 生效，去除 ``norm(x,y,z) > max_range`` 的点；``None`` 不过滤。
            drop_all_zero: 是否去除 xyz 全为 0 的无回波点。
        """
        if max_range is not None and max_range <= 0:
            raise ValueError(f"max_range 必须为正数或 None: {max_range}")
        self._max_range = max_range
        self._drop_all_zero = drop_all_zero

    def clean(self, frame: TimestampedData) -> TimestampedData:
        """按帧类型执行清洗，返回新的不可变帧（非数组帧原样返回）。"""
        if isinstance(frame, LidarFrame):
            return self._clean_lidar(frame)
        if isinstance(frame, RadarFrame):
            return self._clean_radar(frame)
        return frame

    def _mask(self, array: NDArray[np.float64]) -> NDArray[np.bool_]:
        """构造有效行布尔掩码：全部有限，且可选去零、去超距。"""
        mask = np.isfinite(array).all(axis=1)
        if self._drop_all_zero:
            mask = mask & np.any(array[:, :3] != 0.0, axis=1)
        if self._max_range is not None and array.size:
            distance = np.linalg.norm(array[:, :3], axis=1)
            mask = mask & (distance <= self._max_range)
        return mask

    def _clean_lidar(self, frame: LidarFrame) -> LidarFrame:
        """过滤无效点并更新点计数。"""
        kept = frame.points[self._mask(frame.points)]
        return frame.model_copy(update={"points": kept, "point_count": int(kept.shape[0])})

    def _clean_radar(self, frame: RadarFrame) -> RadarFrame:
        """过滤含非有限值的点迹并更新计数。"""
        detections = frame.detections
        if detections.size == 0:
            return frame
        mask = np.isfinite(detections).all(axis=1)
        kept = detections[mask]
        return frame.model_copy(update={"detections": kept, "detection_count": int(kept.shape[0])})


__all__ = ["CleanerImpl"]
