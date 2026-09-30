"""HunterSim 传感器仿真服务层（ENG-004）。

负责 CARLA 传感器配置创建、ROS2 话题桥接、数据格式转换和录制。
"""

from hunter_sim.sensor_sim.carla_ros_bridge import CarlaRosBridge
from hunter_sim.sensor_sim.data_converters import SensorDataConverter
from hunter_sim.sensor_sim.data_recorder import DataRecorder
from hunter_sim.sensor_sim.sensor_factory import (
    DepthCameraConfig,
    EventSensorConfig,
    GNSSConfig,
    IMUConfig,
    LidarConfig,
    RGBCameraConfig,
    SensorBlueprintFactory,
    SensorConfigBundle,
)
from hunter_sim.sensor_sim.sensor_mount import SensorMountManager, SensorMountSpec
from hunter_sim.sensor_sim.topic_mapper import TopicMapper

__all__ = [
    "CarlaRosBridge",
    "SensorDataConverter",
    "DataRecorder",
    "DepthCameraConfig", "EventSensorConfig", "GNSSConfig", "IMUConfig",
    "LidarConfig", "RGBCameraConfig", "SensorBlueprintFactory", "SensorConfigBundle",
    "SensorMountManager", "SensorMountSpec",
    "TopicMapper",
]
