"""HunterSim 传感器仿真服务层（ENG-004 / V3.0.0）。

负责 CARLA 传感器创建、安装、数据转换、帧汇聚与 mcap 录制。
ROS2 出口（``CarlaRosBridge``）可选：``rclpy`` 缺失时不影响场景运行。
"""

from hunter_sim.sensor_sim.carla_ros_bridge import CarlaRosBridge
from hunter_sim.sensor_sim.data_converters import SensorDataConverter
from hunter_sim.sensor_sim.frame_hub import Frame, FrameHub
from hunter_sim.sensor_sim.image_codec import (
    EncodedImage,
    encode_rgb_frame,
    depth_bgra_to_meters,
)
from hunter_sim.sensor_sim.mcap_recorder import McapRecorder, RecordStats
from hunter_sim.sensor_sim.profiles import (
    DEFAULT_PROFILE,
    HUNTER_SE_PROFILES,
    get_profile,
    resolve_profiles,
)
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
    "Frame", "FrameHub",
    "EncodedImage", "encode_rgb_frame", "depth_bgra_to_meters",
    "McapRecorder", "RecordStats",
    "DEFAULT_PROFILE", "HUNTER_SE_PROFILES", "get_profile", "resolve_profiles",
    "DepthCameraConfig", "EventSensorConfig", "GNSSConfig", "IMUConfig",
    "LidarConfig", "RGBCameraConfig", "SensorBlueprintFactory", "SensorConfigBundle",
    "SensorMountManager", "SensorMountSpec",
    "TopicMapper",
]
