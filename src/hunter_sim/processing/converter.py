"""模块 3.1：原始 CARLA 测量 → ``core`` 契约帧。

转换使用 numpy 向量化解析 ``raw_data``；坐标系可选由 CARLA 左手系（X 东/Y 北/Z 上）
翻转为右手系（``to_ros_axes=True`` 时对点云 Y 轴取反，对齐实车 odom 约定，见规则 §9）。

CARLA 测量对象以不透明句柄（``Any``）处理：运行期为真实传感器数据，单测用
``SimpleNamespace`` 提供等价属性与 ``bytes`` 缓冲区。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from hunter_sim.core.contracts import (
    CameraFrame,
    GnssFrame,
    ImuFrame,
    LidarFrame,
    RadarFrame,
    TimestampedData,
)
from hunter_sim.core.exceptions import ConversionError

#: sensor type → 处理函数签名
_Handler = Callable[[Any, str, int], TimestampedData]


class ConverterImpl:
    """满足 :class:`~hunter_sim.processing.protocols.Converter` 契约。"""

    def __init__(self, *, to_ros_axes: bool = False) -> None:
        """初始化转换器。

        Args:
            to_ros_axes: 是否将点云坐标从 CARLA 左手系翻转为右手系（Y 取反）。
        """
        self._to_ros_axes = to_ros_axes
        self._handlers: dict[str, _Handler] = {
            "camera.rgb": self._camera,
            "camera.depth": self._camera,
            "camera.semantic": self._camera,
            "lidar.ray_cast": self._lidar,
            "radar": self._radar,
            "imu": self._imu,
            "gnss": self._gnss,
        }

    def convert(
        self,
        sensor_type: str,
        measurement: Any,
        *,
        sensor_id: str,
        frame_id: int,
    ) -> TimestampedData:
        """按传感器类型分派转换，包装解析/校验异常为 :class:`ConversionError`。"""
        handler = self._handlers.get(sensor_type)
        if handler is None:
            raise ConversionError(f"不支持的传感器类型: {sensor_type}")
        try:
            return handler(measurement, sensor_id, frame_id)
        except ConversionError:
            raise
        except (ValueError, TypeError, AttributeError) as exc:
            raise ConversionError(f"转换失败 type={sensor_type} sensor={sensor_id}: {exc}") from exc

    # -- 各类型处理函数 ---------------------------------------------------

    def _camera(self, measurement: Any, sensor_id: str, frame_id: int) -> CameraFrame:
        """BGRA/RGBA 原始缓冲 → ``(H, W, 3)`` uint8 RGB 图像。"""
        width = int(measurement.width)
        height = int(measurement.height)
        buffer = np.frombuffer(measurement.raw_data, dtype=np.uint8)
        if buffer.size != width * height * 4:
            raise ConversionError(
                f"相机缓冲区尺寸不符: 期望 {width * height * 4} 得到 {buffer.size}"
            )
        rgba = buffer.reshape(height, width, 4)
        image = np.ascontiguousarray(rgba[:, :, :3])
        return CameraFrame(
            timestamp=float(measurement.timestamp),
            frame_id=frame_id,
            sensor_id=sensor_id,
            image=image,
            width=width,
            height=height,
            fov=float(measurement.fov),
        )

    def _lidar(self, measurement: Any, sensor_id: str, frame_id: int) -> LidarFrame:
        """float32 原始缓冲 → ``(N, 4)`` ``[x, y, z, intensity]`` 点云。"""
        points = np.frombuffer(measurement.raw_data, dtype=np.float32)
        if points.size % 4 != 0:
            raise ConversionError(f"点云数据长度非 4 的倍数: {points.size}")
        array = points.reshape(-1, 4).astype(np.float64)
        if self._to_ros_axes and array.size:
            array[:, 1] *= -1.0  # CARLA 左手系 → 右手系（Y 取反）
        return LidarFrame(
            timestamp=float(measurement.timestamp),
            frame_id=frame_id,
            sensor_id=sensor_id,
            points=array,
            point_count=array.shape[0],
        )

    def _radar(self, measurement: Any, sensor_id: str, frame_id: int) -> RadarFrame:
        """Radar 点迹列表 → ``(N, 4)`` ``[velocity, azimuth, altitude, rcs]``。"""
        detections = list(measurement.detections)
        if detections:
            array = np.array(
                [[d.velocity, d.azimuth, d.altitude, d.rcs] for d in detections],
                dtype=np.float64,
            )
        else:
            array = np.empty((0, 4), dtype=np.float64)
        return RadarFrame(
            timestamp=float(measurement.timestamp),
            frame_id=frame_id,
            sensor_id=sensor_id,
            detections=array,
            detection_count=array.shape[0],
        )

    def _imu(self, measurement: Any, sensor_id: str, frame_id: int) -> ImuFrame:
        """IMUMeasurement → :class:`ImuFrame`。"""
        acc = measurement.accelerometer
        gyro = measurement.gyroscope
        return ImuFrame(
            timestamp=float(measurement.timestamp),
            frame_id=frame_id,
            sensor_id=sensor_id,
            accelerometer=(float(acc.x), float(acc.y), float(acc.z)),
            gyroscope=(float(gyro.x), float(gyro.y), float(gyro.z)),
            compass=float(measurement.compass),
        )

    def _gnss(self, measurement: Any, sensor_id: str, frame_id: int) -> GnssFrame:
        """GNSSMeasurement → :class:`GnssFrame`。"""
        return GnssFrame(
            timestamp=float(measurement.timestamp),
            frame_id=frame_id,
            sensor_id=sensor_id,
            latitude=float(measurement.latitude),
            longitude=float(measurement.longitude),
            altitude=float(measurement.altitude),
        )


__all__ = ["ConverterImpl"]
