"""ROS2 话题映射器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from hunter_sim.common.models import RosTopicMapping
from hunter_sim.sensor_sim.topic_mapper import TopicMapper


class TestTopicMapper:
    def setup_method(self) -> None:
        self.mapper = TopicMapper()

    def test_sim_to_real_known(self) -> None:
        assert self.mapper.sim_to_real("/carla/lidar_points") == "/lidar_points"
        assert self.mapper.sim_to_real("/carla/imu/data") == "/imu/data"

    def test_real_to_sim_known(self) -> None:
        assert self.mapper.real_to_sim("/lidar_points") == "/carla/lidar_points"
        assert self.mapper.real_to_sim("/imu/data") == "/carla/imu/data"

    def test_unmapped_returns_original(self) -> None:
        assert self.mapper.sim_to_real("/unknown/topic") == "/unknown/topic"
        assert self.mapper.real_to_sim("/unknown/topic") == "/unknown/topic"

    def test_get_mapping_object(self) -> None:
        m = self.mapper.get_mapping("/carla/lidar_points")
        assert isinstance(m, RosTopicMapping)
        assert m.msg_type == "sensor_msgs/PointCloud2"

    def test_get_mapping_missing(self) -> None:
        assert self.mapper.get_mapping("/nope") is None

    def test_frequency(self) -> None:
        assert self.mapper.get_frequency("/carla/imu/data") == 100.0
        assert self.mapper.get_frequency("/carla/lidar_points") == 10.0
        assert self.mapper.get_frequency("/missing") == 0.0

    def test_msg_type(self) -> None:
        assert self.mapper.get_msg_type("/carla/camera/color/image_raw") == "sensor_msgs/Image"
        assert self.mapper.get_msg_type("/missing") == ""

    def test_all_topics(self) -> None:
        assert "/carla/lidar_points" in self.mapper.all_sim_topics()
        assert "/imu/data" in self.mapper.all_real_topics()
