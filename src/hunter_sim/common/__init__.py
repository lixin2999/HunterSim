"""HunterSim 公共层：异常定义、数据模型、工具函数。"""

from hunter_sim.common.exceptions import (
    CarlaConnectionError,
    CarlaSimulationError,
    ConfigurationError,
    ConnectionError,
    HunterSimError,
    InstanceStateError,
    KafkaConnectionError,
    ResourceError,
    ROS2ConnectionError,
    SensorSimulationError,
    SimTimeoutError,
    SimulationError,
    ValidationError,
)
from hunter_sim.common.models import (
    APISettings,
    CarlaSettings,
    DetectedObject,
    EvalGrade,
    HunterSESpec,
    HunterSimSettings,
    InstanceStatus,
    KafkaSettings,
    PerceptionResult,
    QualityLevel,
    ResourceSettings,
    RosTopicMapping,
    ROS2_TOPIC_MAP,
    SceneStatus,
    SensorType,
    SimMode,
    Transform,
    VehicleControlCommand,
    VehicleState,
    VILSettings,
)
from hunter_sim.common.utils import (
    RingBuffer,
    deg_to_rad,
    euclidean_distance_2d,
    get_logger,
    get_struct_logger,
    lerp,
    normalize_angle_rad,
    rad_to_deg,
    rmse,
    smoothstep,
)

__all__ = [
    # exceptions
    "HunterSimError", "ConfigurationError", "ConnectionError",
    "CarlaConnectionError", "KafkaConnectionError", "ROS2ConnectionError",
    "ValidationError", "SimulationError", "CarlaSimulationError",
    "SensorSimulationError", "ResourceError", "SimTimeoutError", "InstanceStateError",
    # models
    "SimMode", "QualityLevel", "InstanceStatus", "SceneStatus", "SensorType", "EvalGrade",
    "Transform", "VehicleControlCommand", "VehicleState",
    "DetectedObject", "PerceptionResult", "HunterSESpec", "RosTopicMapping", "ROS2_TOPIC_MAP",
    "CarlaSettings", "KafkaSettings", "VILSettings", "APISettings", "ResourceSettings",
    "HunterSimSettings",
    # utils
    "RingBuffer", "get_logger", "get_struct_logger",
    "deg_to_rad", "rad_to_deg", "normalize_angle_rad",
    "lerp", "smoothstep", "euclidean_distance_2d", "rmse",
]
