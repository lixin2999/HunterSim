"""ROS2 话题映射器（PROMPT-ENG-004-B）。

将 CARLA 仿真话题名称映射到实车话题名称，确保格式完全一致。
"""

from __future__ import annotations

from hunter_sim.common.models import ROS2_TOPIC_MAP, RosTopicMapping, SensorType
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# SensorType -> 实车话题快速查询字典
_SENSOR_TO_REAL: dict[SensorType, RosTopicMapping] = {}
for _m in ROS2_TOPIC_MAP:
    if "lidar" in _m.sim_topic:
        _SENSOR_TO_REAL[SensorType.LIDAR] = _m
    elif "color/image" in _m.sim_topic:
        _SENSOR_TO_REAL[SensorType.RGB_CAMERA] = _m
    elif "depth" in _m.sim_topic:
        _SENSOR_TO_REAL[SensorType.DEPTH_CAMERA] = _m
    elif "imu" in _m.sim_topic:
        _SENSOR_TO_REAL[SensorType.IMU] = _m
    elif "odometry" in _m.sim_topic:
        pass  # odom 不对应单一传感器
    elif "chassis" in _m.sim_topic or "vehicle_status" in _m.sim_topic:
        pass


class TopicMapper:
    """仿真话题名 ↔ 实车话题名双向映射。

    无外部依赖，纯映射查询。
    """

    def __init__(self) -> None:
        self._sim_to_real: dict[str, RosTopicMapping] = {m.sim_topic: m for m in ROS2_TOPIC_MAP}
        self._real_to_sim: dict[str, RosTopicMapping] = {m.real_topic: m for m in ROS2_TOPIC_MAP}

    def sim_to_real(self, sim_topic: str) -> str:
        """仿真话题 → 实车话题。

        Args:
            sim_topic: 仿真话题名称（如 /carla/lidar_points）。

        Returns:
            对应的实车话题名称，无映射时返回原名称。
        """
        mapping = self._sim_to_real.get(sim_topic)
        return mapping.real_topic if mapping else sim_topic

    def real_to_sim(self, real_topic: str) -> str:
        """实车话题 → 仿真话题。"""
        mapping = self._real_to_sim.get(real_topic)
        return mapping.sim_topic if mapping else real_topic

    def get_mapping(self, sim_topic: str) -> RosTopicMapping | None:
        """获取完整的映射记录对象。"""
        return self._sim_to_real.get(sim_topic)

    def get_frequency(self, sim_topic: str) -> float:
        """返回话题对应频率（Hz），未找到时返回 0.0。"""
        mapping = self._sim_to_real.get(sim_topic)
        return mapping.frequency_hz if mapping else 0.0

    def get_msg_type(self, sim_topic: str) -> str:
        """返回 ROS2 消息类型字符串。"""
        mapping = self._sim_to_real.get(sim_topic)
        return mapping.msg_type if mapping else ""

    def all_sim_topics(self) -> list[str]:
        """返回所有仿真话题列表。"""
        return list(self._sim_to_real.keys())

    def all_real_topics(self) -> list[str]:
        """返回所有实车话题列表。"""
        return list(self._real_to_sim.keys())
