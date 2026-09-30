"""传感器数据 ROS2 格式转换器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import math
import struct
import sys
import types
from typing import Any

import numpy as np
import pytest

from hunter_sim.sensor_sim.data_converters import SensorDataConverter


class _Auto:
    """自动补全嵌套属性的桩消息基类。"""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        node = _Auto()
        self.__dict__[name] = node
        return node


class _PointField(_Auto):
    FLOAT32 = 7


def _mod(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


@pytest.fixture(autouse=True)
def ros_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    mods = {
        "rclpy": _mod("rclpy"),
        "rclpy.qos": _mod("rclpy.qos", QoSProfile=_Auto),
        "rclpy.time": _mod("rclpy.time", Time=_Auto),
        "std_msgs": _mod("std_msgs"),
        "std_msgs.msg": _mod("std_msgs.msg", Header=_Auto),
        "sensor_msgs": _mod("sensor_msgs"),
        "sensor_msgs.msg": _mod("sensor_msgs.msg", PointCloud2=_Auto, PointField=_PointField, Image=_Auto, Imu=_Auto),
        "geometry_msgs": _mod("geometry_msgs"),
        "geometry_msgs.msg": _mod("geometry_msgs.msg", Vector3=_Auto, Quaternion=_Auto, Twist=_Auto, PoseWithCovariance=_Auto, TwistWithCovariance=_Auto),
        "nav_msgs": _mod("nav_msgs"),
        "nav_msgs.msg": _mod("nav_msgs.msg", Odometry=_Auto),
    }
    for name, m in mods.items():
        monkeypatch.setitem(sys.modules, name, m)


class _Meas:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class TestLidar:
    def test_convert_to_pointcloud2(self) -> None:
        pts = [(1.0, 2.0, 3.0, 0.5), (4.0, 5.0, 6.0, 0.5)]
        raw = b"".join(struct.pack("ffff", *p) for p in pts)
        meas = _Meas(data=raw, timestamp=12.5)
        msg = SensorDataConverter.lidar_to_pointcloud2(meas, frame_id="lidar_top")
        assert msg.width == 2
        assert msg.height == 1
        assert msg.point_step == 16
        assert msg.row_step == 32
        assert len(msg.fields) == 4
        assert msg.fields[0].name == "x"
        assert msg.header.frame_id == "lidar_top"
        # 解包第一个点应为 (1,2,3,0)
        x, y, z, inten = struct.unpack_from("ffff", msg.data, 0)
        assert (x, y, z) == (1.0, 2.0, 3.0)
        assert inten == 0.0


class TestCamera:
    def test_rgb_encoding_step(self) -> None:
        img = _Meas(data=b"\x00" * 12, width=2, height=2, timestamp=1.0)
        msg = SensorDataConverter.camera_image_to_ros_image(img, encoding="rgb8")
        assert msg.height == 2
        assert msg.width == 2
        assert msg.step == 6  # 2 * 3
        assert msg.encoding == "rgb8"

    def test_float_encoding_step(self) -> None:
        img = _Meas(data=b"\x00" * 8, width=2, height=2, timestamp=0.0)
        msg = SensorDataConverter.camera_image_to_ros_image(img, encoding="32FC1")
        assert msg.step == 2  # 2 * 1


class TestImu:
    def test_convert(self) -> None:
        imu = _Meas(accelerometer=(1.0, 2.0, 9.8), gyroscope=(0.1, 0.2, 0.3), timestamp=3.0)
        msg = SensorDataConverter.imu_to_ros_imu(imu)
        assert msg.header.frame_id == "imu_link"
        assert msg.linear_acceleration.z == 9.8
        assert msg.angular_velocity.x == 0.1

    def test_defaults_when_missing_fields(self) -> None:
        msg = SensorDataConverter.imu_to_ros_imu(_Meas(timestamp=0.0))
        assert msg.linear_acceleration.x == 0.0


class TestOdometry:
    def test_yaw_to_quaternion(self) -> None:
        msg = SensorDataConverter.vehicle_state_to_odometry(
            1.0, 2.0, 0.5, yaw_rad=math.pi / 2, vx=3.0, vy=0.5, wz=0.1,
        )
        assert msg.header.frame_id == "odom"
        assert msg.child_frame_id == "base_link"
        assert msg.pose.pose.position.x == 1.0
        # yaw=pi/2 → qz=sin(pi/4)
        assert msg.pose.pose.orientation.z == pytest.approx(math.sin(math.pi / 4))
        assert msg.twist.twist.linear.x == 3.0
        assert msg.twist.twist.angular.z == 0.1


class TestRawImageNumpy:
    def test_reshape(self) -> None:
        arr = np.zeros((2, 3, 4), dtype=np.uint8)
        img = _Meas(data=arr.tobytes(), width=3, height=2)
        out = SensorDataConverter.raw_image_to_numpy(img)
        assert out.shape == (2, 3, 4)
        assert out.dtype == np.uint8


class TestRosTime:
    def test_time_none_when_no_rclpy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 移除 rclpy.time 桩，触发 ImportError 分支返回 None
        monkeypatch.delitem(sys.modules, "rclpy.time")
        monkeypatch.setitem(sys.modules, "rclpy", None)  # 阻断真实导入
        from hunter_sim.sensor_sim.data_converters import _get_ros_time

        assert _get_ros_time(_Meas(timestamp=5.0)) is None
