"""模块 3.3 数据清洗器的单元测试。"""

from __future__ import annotations

import numpy as np
import pytest

from hunter_sim.core.contracts import ImuFrame, LidarFrame, RadarFrame
from hunter_sim.processing.cleaner import CleanerImpl


def make_lidar(points: np.ndarray) -> LidarFrame:
    return LidarFrame(
        timestamp=1.0,
        frame_id=0,
        sensor_id="lidar",
        points=points,
        point_count=int(points.shape[0]),
    )


def make_radar(detections: np.ndarray) -> RadarFrame:
    return RadarFrame(
        timestamp=1.0,
        frame_id=0,
        sensor_id="radar",
        detections=detections,
        detection_count=int(detections.shape[0]),
    )


def test_clean_lidar_drops_nan_and_zero() -> None:
    points = np.array(
        [
            [1.0, 1.0, 1.0, 0.5],
            [0.0, 0.0, 0.0, 0.0],  # 无回波
            [np.nan, 1.0, 1.0, 0.0],  # 非有限
            [2.0, 2.0, 2.0, 0.1],
        ],
        dtype=np.float64,
    )
    frame = make_lidar(points)
    cleaned = CleanerImpl().clean(frame)
    assert isinstance(cleaned, LidarFrame)
    assert cleaned.point_count == 2
    assert np.all(np.isfinite(cleaned.points)).all()
    # 输入不可变，原帧保持不变
    assert frame.point_count == 4


def test_clean_lidar_max_range() -> None:
    points = np.array(
        [[1.0, 1.0, 1.0, 0.5], [100.0, 100.0, 100.0, 0.2]],
        dtype=np.float64,
    )
    cleaned = CleanerImpl(max_range=50.0).clean(make_lidar(points))
    assert cleaned.point_count == 1


def test_clean_lidar_empty() -> None:
    cleaned = CleanerImpl().clean(make_lidar(np.empty((0, 4), dtype=np.float64)))
    assert cleaned.point_count == 0
    assert cleaned.points.shape == (0, 4)


def test_clean_radar_drops_nan() -> None:
    detections = np.array(
        [[1.0, 2.0, 3.0, 4.0], [np.nan, 0.0, 0.0, 0.0], [5.0, 6.0, 7.0, 8.0]],
        dtype=np.float64,
    )
    cleaned = CleanerImpl().clean(make_radar(detections))
    assert isinstance(cleaned, RadarFrame)
    assert cleaned.detection_count == 2


def test_clean_radar_empty_passthrough() -> None:
    frame = make_radar(np.empty((0, 4), dtype=np.float64))
    cleaned = CleanerImpl().clean(frame)
    assert cleaned is frame


def test_clean_non_array_frame_passthrough() -> None:
    imu = ImuFrame(
        timestamp=1.0,
        frame_id=0,
        sensor_id="imu",
        accelerometer=(1.0, 2.0, 3.0),
        gyroscope=(0.0, 0.0, 0.0),
        compass=0.0,
    )
    assert CleanerImpl().clean(imu) is imu


def test_cleaner_invalid_max_range() -> None:
    with pytest.raises(ValueError):
        CleanerImpl(max_range=0.0)
