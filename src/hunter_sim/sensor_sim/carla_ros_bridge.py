"""CARLA ROS2 Bridge 主服务类（PROMPT-ENG-004-B）。

将 CARLA 传感器回调数据发布为 ROS2 话题，话题名称与实车完全一致。
通过 carla-ros-bridge（ROS2 Humble 兼容）或直接使用 rclpy 发布。

使用方式：在 CARLA 传感器回调中调用 bridge.publish_*() 方法。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from hunter_sim.common.exceptions import ROS2ConnectionError, SensorSimulationError
from hunter_sim.common.models import SensorType
from hunter_sim.common.utils import get_logger
from hunter_sim.sensor_sim.data_converters import SensorDataConverter
from hunter_sim.sensor_sim.topic_mapper import TopicMapper

logger = get_logger(__name__)


class CarlaRosBridge:
    """CARLA → ROS2 话题桥接服务。

    封装 rclpy 节点，为每种传感器创建对应 Publisher。
    CARLA 传感器回调通过本类的 publish 方法转发到 ROS2 话题。

    本类需在 ROS2 环境（Ubuntu 22.04 + Humble）下运行。
    CARLA 端（Windows）可通过网络发送或共享内存获取数据。

    Args:
        node_name: ROS2 节点名称。
        topic_mapper: 话题映射器实例。
    """

    def __init__(
        self,
        node_name: str = "hunter_sim_bridge",
        topic_mapper: Optional[TopicMapper] = None,
    ) -> None:
        self._node_name = node_name
        self._mapper = topic_mapper or TopicMapper()
        self._converter = SensorDataConverter()
        self._node: Any = None
        self._publishers: dict[SensorType, Any] = {}
        self._initialized: bool = False
        self._lock: threading.Lock = threading.Lock()
        self._msg_count: dict[str, int] = {}
        logger.info(f"CarlaRosBridge created (node={node_name})")

    def initialize(self) -> None:
        """初始化 ROS2 节点和所有 Publisher。

        应在 rclpy.init() 之后调用。

        Raises:
            ROS2ConnectionError: rclpy 不可用或节点创建失败。
        """
        try:
            import rclpy  # noqa: PLC0415
            from rclpy.node import Node  # noqa: PLC0415
            from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy  # noqa: PLC0415
            from sensor_msgs.msg import Image, Imu, PointCloud2  # noqa: PLC0415
            from nav_msgs.msg import Odometry  # noqa: PLC0415
        except ImportError as exc:
            raise ROS2ConnectionError(
                self._node_name,
                f"rclpy or message packages not available: {exc}. "
                "Run in ROS2 Humble environment.",
            ) from exc

        try:
            if not rclpy.ok():
                rclpy.init()
            self._node = Node(self._node_name)

            # 传感器话题 QoS（实时数据，允许少量丢包）
            sensor_qos = QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=5,
                reliability=ReliabilityPolicy.BEST_EFFORT,
            )

            # 创建各传感器 Publisher
            topic_configs: list[tuple[SensorType, str, Any]] = [
                (SensorType.LIDAR, "/lidar_points", PointCloud2),
                (SensorType.RGB_CAMERA, "/camera/color/image_raw", Image),
                (SensorType.DEPTH_CAMERA, "/camera/depth/image_rect_raw", Image),
                (SensorType.IMU, "/imu/data", Imu),
            ]
            for stype, real_topic, msg_cls in topic_configs:
                pub = self._node.create_publisher(msg_cls, real_topic, sensor_qos)
                self._publishers[stype] = pub
                self._msg_count[real_topic] = 0
                logger.info(f"Publisher created: {real_topic} ({msg_cls.__name__})")

            self._initialized = True
            logger.info(f"CarlaRosBridge initialized, {len(self._publishers)} publishers")

        except Exception as exc:
            raise ROS2ConnectionError(self._node_name, str(exc)) from exc

    def publish_lidar(self, lidar_measurement: Any) -> None:
        """发布 LiDAR 点云数据到 /lidar_points。"""
        self._ensure_ready()
        try:
            msg = self._converter.lidar_to_pointcloud2(lidar_measurement)
            pub = self._publishers.get(SensorType.LIDAR)
            if pub:
                pub.publish(msg)
                self._msg_count["/lidar_points"] = self._msg_count.get("/lidar_points", 0) + 1
        except Exception as exc:
            raise SensorSimulationError("lidar", "publish", str(exc)) from exc

    def publish_rgb_image(self, image_data: Any) -> None:
        """发布 RGB 图像到 /camera/color/image_raw。"""
        self._ensure_ready()
        try:
            msg = self._converter.camera_image_to_ros_image(image_data, encoding="rgb8")
            pub = self._publishers.get(SensorType.RGB_CAMERA)
            if pub:
                pub.publish(msg)
                self._msg_count["/camera/color/image_raw"] = (
                    self._msg_count.get("/camera/color/image_raw", 0) + 1
                )
        except Exception as exc:
            raise SensorSimulationError("rgb_camera", "publish", str(exc)) from exc

    def publish_depth_image(self, image_data: Any) -> None:
        """发布深度图像到 /camera/depth/image_rect_raw。"""
        self._ensure_ready()
        try:
            msg = self._converter.camera_image_to_ros_image(image_data, encoding="16UC1")
            pub = self._publishers.get(SensorType.DEPTH_CAMERA)
            if pub:
                pub.publish(msg)
                self._msg_count["/camera/depth/image_rect_raw"] = (
                    self._msg_count.get("/camera/depth/image_rect_raw", 0) + 1
                )
        except Exception as exc:
            raise SensorSimulationError("depth_camera", "publish", str(exc)) from exc

    def publish_imu(self, imu_measurement: Any) -> None:
        """发布 IMU 数据到 /imu/data。"""
        self._ensure_ready()
        try:
            msg = self._converter.imu_to_ros_imu(imu_measurement)
            pub = self._publishers.get(SensorType.IMU)
            if pub:
                pub.publish(msg)
                self._msg_count["/imu/data"] = self._msg_count.get("/imu/data", 0) + 1
        except Exception as exc:
            raise SensorSimulationError("imu", "publish", str(exc)) from exc

    def spin_once(self, timeout_sec: float = 0.01) -> None:
        """处理一次 ROS2 事件循环（非阻塞模式）。"""
        if self._node is not None:
            try:
                import rclpy  # noqa: PLC0415
                rclpy.spin_once(self._node, timeout_sec=timeout_sec)
            except Exception:
                pass

    def shutdown(self) -> None:
        """关闭 ROS2 节点和所有 Publisher。"""
        if self._node is not None:
            try:
                self._node.destroy_node()
                import rclpy  # noqa: PLC0415
                if rclpy.ok():
                    rclpy.shutdown()
            except Exception as exc:
                logger.warning(f"ROS2 shutdown error: {exc}")
        self._initialized = False
        self._node = None
        self._publishers.clear()
        logger.info(
            "CarlaRosBridge shutdown. Message counts: "
            + ", ".join(f"{t}={c}" for t, c in self._msg_count.items())
        )

    @property
    def is_initialized(self) -> bool:
        """Bridge 是否已初始化。"""
        return self._initialized

    @property
    def message_counts(self) -> dict[str, int]:
        """各话题已发布消息数量。"""
        return dict(self._msg_count)

    def _ensure_ready(self) -> None:
        if not self._initialized:
            raise SensorSimulationError("bridge", "check", "CarlaRosBridge not initialized")
