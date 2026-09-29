"""模块 3.1 转换器的单元测试（CARLA 测量以 SimpleNamespace + bytes 缓冲区模拟）。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from hunter_sim.core.exceptions import ConversionError
from hunter_sim.processing.converter import ConverterImpl


def _camera_measurement(h: int, w: int, *, fov: float = 90.0) -> SimpleNamespace:
    buffer = np.arange(h * w * 4, dtype=np.uint8).reshape(h, w, 4)
    return SimpleNamespace(width=w, height=h, fov=fov, timestamp=1.5, raw_data=buffer.tobytes())


def test_convert_camera_rgb() -> None:
    measurement = _camera_measurement(2, 3)
    frame = ConverterImpl().convert("camera.rgb", measurement, sensor_id="cam", frame_id=7)
    assert frame.image.shape == (2, 3, 3)
    assert frame.image.dtype == np.uint8
    assert (
        frame.image == np.frombuffer(measurement.raw_data, np.uint8).reshape(2, 3, 4)[:, :, :3]
    ).all()
    assert frame.frame_id == 7
    assert frame.timestamp == 1.5
    assert frame.width == 3 and frame.height == 2


def test_convert_lidar_points() -> None:
    points = np.array([[1, 2, 3, 0.5], [4, 5, 6, 0.1]], dtype=np.float32)
    measurement = SimpleNamespace(timestamp=0.2, raw_data=points.tobytes())
    frame = ConverterImpl().convert("lidar.ray_cast", measurement, sensor_id="lidar", frame_id=0)
    assert frame.points.shape == (2, 4)
    assert frame.points.dtype == np.float64
    assert np.allclose(frame.points, points.astype(np.float64))
    assert frame.point_count == 2


def test_convert_lidar_to_ros_axes_flips_y() -> None:
    points = np.array([[1, 2, 3, 0.5], [4, 5, 6, 0.1]], dtype=np.float32)
    measurement = SimpleNamespace(timestamp=0.2, raw_data=points.tobytes())
    frame = ConverterImpl(to_ros_axes=True).convert(
        "lidar.ray_cast", measurement, sensor_id="lidar", frame_id=0
    )
    assert np.allclose(frame.points[:, 1], -points[:, 1].astype(np.float64))
    assert np.allclose(frame.points[:, 0], points[:, 0].astype(np.float64))


def test_convert_radar_detections() -> None:
    detections = [
        SimpleNamespace(velocity=1.0, azimuth=2.0, altitude=3.0, rcs=4.0),
        SimpleNamespace(velocity=5.0, azimuth=6.0, altitude=7.0, rcs=8.0),
    ]
    measurement = SimpleNamespace(timestamp=0.5, detections=detections)
    frame = ConverterImpl().convert("radar", measurement, sensor_id="radar", frame_id=1)
    assert frame.detections.shape == (2, 4)
    assert frame.detection_count == 2
    assert np.allclose(frame.detections[1], [5.0, 6.0, 7.0, 8.0])


def test_convert_radar_empty() -> None:
    measurement = SimpleNamespace(timestamp=0.5, detections=[])
    frame = ConverterImpl().convert("radar", measurement, sensor_id="radar", frame_id=1)
    assert frame.detections.shape == (0, 4)
    assert frame.detection_count == 0


def test_convert_imu() -> None:
    measurement = SimpleNamespace(
        timestamp=0.1,
        accelerometer=SimpleNamespace(x=1.0, y=2.0, z=3.0),
        gyroscope=SimpleNamespace(x=0.0, y=0.0, z=0.0),
        compass=1.57,
    )
    frame = ConverterImpl().convert("imu", measurement, sensor_id="imu", frame_id=2)
    assert frame.accelerometer == (1.0, 2.0, 3.0)
    assert frame.gyroscope == (0.0, 0.0, 0.0)
    assert frame.compass == 1.57


def test_convert_gnss() -> None:
    measurement = SimpleNamespace(timestamp=0.1, latitude=10.0, longitude=20.0, altitude=30.0)
    frame = ConverterImpl().convert("gnss", measurement, sensor_id="gnss", frame_id=3)
    assert (frame.latitude, frame.longitude, frame.altitude) == (10.0, 20.0, 30.0)


def test_convert_unsupported_type() -> None:
    with pytest.raises(ConversionError):
        ConverterImpl().convert("bogus.type", object(), sensor_id="x", frame_id=0)


def test_convert_camera_size_mismatch() -> None:
    measurement = SimpleNamespace(
        width=3, height=2, fov=90.0, timestamp=0.0, raw_data=np.zeros(10, np.uint8).tobytes()
    )
    with pytest.raises(ConversionError):
        ConverterImpl().convert("camera.rgb", measurement, sensor_id="cam", frame_id=0)


def test_convert_lidar_bad_length() -> None:
    measurement = SimpleNamespace(timestamp=0.0, raw_data=np.zeros(6, np.float32).tobytes())
    with pytest.raises(ConversionError):
        ConverterImpl().convert("lidar.ray_cast", measurement, sensor_id="lidar", frame_id=0)


def test_convert_wraps_missing_attribute() -> None:
    measurement = SimpleNamespace(  # 缺少 fov
        width=3,
        height=2,
        timestamp=0.0,
        raw_data=np.zeros(2 * 3 * 4, np.uint8).tobytes(),
    )
    with pytest.raises(ConversionError):
        ConverterImpl().convert("camera.rgb", measurement, sensor_id="cam", frame_id=0)
