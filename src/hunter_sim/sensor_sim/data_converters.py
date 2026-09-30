"""CARLA 传感器数据到 ROS2 消息的格式转换器（PROMPT-ENG-004-B）。

将 CARLA 原始传感器数据（RawImage, LidarMeasurement 等）
转换为 ROS2 标准消息类型（sensor_msgs/PointCloud2, sensor_msgs/Image, sensor_msgs/Imu 等）。
"""

from __future__ import annotations

import math
import struct
from typing import Any, Optional

import numpy as np

from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


class SensorDataConverter:
    """无状态传感器数据格式转换工具类。

    所有方法为静态方法，输入 CARLA 原始数据类型，输出对应 ROS2 消息。
    ROS2 消息构造通过 rclpy/ROS2 库延迟导入，避免非 ROS 环境下的导入错误。
    """

    @staticmethod
    def lidar_to_pointcloud2(
        lidar_measurement: Any,
        frame_id: str = "lidar_top",
    ) -> Any:
        """将 CARLA LidarMeasurement 转换为 sensor_msgs/PointCloud2。

        Args:
            lidar_measurement: CARLA sensor_data.LidarMeasurement 对象。
            frame_id: ROS2 坐标系 ID。

        Returns:
            ROS2 PointCloud2 消息对象。
        """
        from rclpy.qos import QoSProfile  # noqa: PLC0415
        from sensor_msgs.msg import PointCloud2, PointField  # noqa: PLC0415
        from std_msgs.msg import Header  # noqa: PLC0415

        # 解析 CARLA LiDAR 数据（float32 数组，每点 3 个分量）
        data = np.frombuffer(lidar_measurement.data, dtype=np.float32)
        points = data.reshape(-1, 4)[:, :3]  # 取 x, y, z（CARLA 第4维为 intensity）

        # CARLA LiDAR 右手系转 ROS 前左上
        # CARLA: x=forward, y=right, z=up → 保持，CARLA LiDAR 与 ROS LiDAR 轴向一致
        n = len(points)
        # 序列化：x(FLOAT32), y(FLOAT32), z(FLOAT32), intensity(FLOAT32)
        buf = bytearray(n * 16)
        for i, (x, y, z) in enumerate(points):
            offset = i * 16
            struct.pack_into("ffff", buf, offset, float(x), float(y), float(z), 0.0)

        msg = PointCloud2()
        msg.header = Header()
        msg.header.stamp = _get_ros_time(lidar_measurement)
        msg.header.frame_id = frame_id
        msg.height = 1
        msg.width = n
        msg.fields = [
            PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
            PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
            PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
            PointField(name="intensity", offset=12, datatype=PointField.FLOAT32, count=1),
        ]
        msg.is_bigendian = False
        msg.point_step = 16
        msg.row_step = n * 16
        msg.data = bytes(buf)
        msg.is_dense = True
        return msg

    @staticmethod
    def camera_image_to_ros_image(
        image_data: Any,
        encoding: str = "rgb8",
        frame_id: str = "camera_link",
    ) -> Any:
        """将 CARLA RawImage 转换为 sensor_msgs/Image。

        Args:
            image_data: CARLA sensor_data.Image 对象（含 data/width/height）。
            encoding: ROS 图像编码（rgb8 / 32FC1 等）。
            frame_id: 图像坐标系 ID。

        Returns:
            ROS2 sensor_msgs/Image 消息对象。
        """
        from sensor_msgs.msg import Image  # noqa: PLC0415
        from std_msgs.msg import Header  # noqa: PLC0415

        msg = Image()
        msg.header = Header()
        msg.header.stamp = _get_ros_time(image_data)
        msg.header.frame_id = frame_id
        msg.height = image_data.height
        msg.width = image_data.width
        msg.encoding = encoding
        msg.is_bigendian = 0
        msg.step = image_data.width * (3 if encoding == "rgb8" else 1)
        msg.data = image_data.data
        return msg

    @staticmethod
    def imu_to_ros_imu(imu_measurement: Any) -> Any:
        """将 CARLA IMU 数据转换为 sensor_msgs/Imu。

        Args:
            imu_measurement: 包含 (acceleration, gyro) 向量的 CARLA 数据。

        Returns:
            ROS2 sensor_msgs/Imu 消息对象。
        """
        from geometry_msgs.msg import Vector3  # noqa: PLC0415
        from sensor_msgs.msg import Imu  # noqa: PLC0415
        from std_msgs.msg import Header  # noqa: PLC0415

        msg = Imu()
        msg.header = Header()
        msg.header.stamp = _get_ros_time(imu_measurement)
        msg.header.frame_id = "imu_link"

        acc = getattr(imu_measurement, "accelerometer", (0.0, 0.0, 0.0))
        gyro = getattr(imu_measurement, "gyroscope", (0.0, 0.0, 0.0))

        msg.linear_acceleration = Vector3(x=float(acc[0]), y=float(acc[1]), z=float(acc[2]))
        msg.angular_velocity = Vector3(x=float(gyro[0]), y=float(gyro[1]), z=float(gyro[2]))
        return msg

    @staticmethod
    def vehicle_state_to_odometry(
        x: float,
        y: float,
        z: float,
        yaw_rad: float,
        vx: float,
        vy: float,
        wz: float,
        timestamp: Any = None,
    ) -> Any:
        """构建 nav_msgs/Odometry 消息。

        Args:
            x, y, z: 位置（米）。
            yaw_rad: 航向角（弧度）。
            vx, vy: 线速度（m/s）。
            wz: 角速度（rad/s）。
            timestamp: ROS2 时间戳对象（可选）。

        Returns:
            ROS2 Odometry 消息对象。
        """
        from geometry_msgs.msg import PoseWithCovariance, Quaternion, Twist, TwistWithCovariance  # noqa: PLC0415
        from nav_msgs.msg import Odometry  # noqa: PLC0415
        from std_msgs.msg import Header  # noqa: PLC0415

        # yaw → quaternion (z 轴旋转)
        qz = math.sin(yaw_rad / 2.0)
        qw = math.cos(yaw_rad / 2.0)

        msg = Odometry()
        msg.header = Header()
        msg.header.frame_id = "odom"
        msg.child_frame_id = "base_link"

        msg.pose.pose.position.x = x
        msg.pose.pose.position.y = y
        msg.pose.pose.position.z = z
        msg.pose.pose.orientation = Quaternion(x=0.0, y=0.0, z=qz, w=qw)
        msg.twist.twist.linear.x = vx
        msg.twist.twist.linear.y = vy
        msg.twist.twist.angular.z = wz
        return msg

    @staticmethod
    def raw_image_to_numpy(image_data: Any) -> np.ndarray:
        """将 CARLA RawImage 转为 numpy 数组（H x W x 4, BGRA uint8）。

        Args:
            image_data: CARLA sensor_data.Image。

        Returns:
            numpy uint8 数组。
        """
        array = np.frombuffer(image_data.data, dtype=np.uint8)
        return array.reshape(image_data.height, image_data.width, 4)


def _get_ros_time(carla_measurement: Any) -> Any:
    """从 CARLA 测量数据提取时间戳并转为 ROS2 Time 对象。"""
    try:
        from rclpy.time import Time  # noqa: PLC0415
        ts = getattr(carla_measurement, "timestamp", 0.0)
        sec = int(ts)
        nanosec = int((ts - sec) * 1e9)
        return Time(seconds=sec, nanoseconds=nanosec)
    except ImportError:
        return None
