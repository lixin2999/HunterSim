"""实车传感器型号剖面（V3.0.0）。

把 HUNTER SE 上实际安装的三型传感器（RS-Helios-16P LiDAR、Intel RealSense
D435 双目/RGB-D、CH10X IMU）的公开规格固化为 ``SensorConfigBundle``，
平台下发场景时只需指定 ``profile`` 名称，避免每次重复传参。

与 ``configs/sensor_configs.json`` 的 ``profiles`` 段双向对应：
单测 ``test_sensor_profiles.py`` 逐字段断言两处一致，改数值需同时改两处。

剖面命名：
- ``helios_16p``：仅 LiDAR；
- ``d435``：RGB + Depth；
- ``ch10x``：仅 IMU；
- ``hunter_se_full``：三件套全挂（默认剖面，由上述三剖面组合而成）。
"""

from __future__ import annotations

from typing import Final

from hunter_sim.sensor_sim.sensor_factory import (
    DepthCameraConfig,
    EventSensorConfig,
    IMUConfig,
    LidarConfig,
    RGBCameraConfig,
    SensorConfigBundle,
)

# 默认剖面（平台未指定 sensors 时使用）
DEFAULT_PROFILE: Final[str] = "hunter_se_full"

# 单型号剖面名（供 hunter_se_full 组合，也允许单独挂载）
LIDAR_PROFILE: Final[str] = "helios_16p"
CAMERA_PROFILE: Final[str] = "d435"
IMU_PROFILE: Final[str] = "ch10x"

# RS-Helios-16P：16 线 / 150m / 10Hz / 垂直 FOV ±15° / 距离噪声 σ=0.02m / 131072 pts·s⁻¹
_HELIOS_16P_LIDAR: Final[LidarConfig] = LidarConfig(
    channels=16,
    range_m=150.0,
    points_per_second=131072,
    rotation_frequency_hz=10.0,
    upper_fov_deg=15.0,
    lower_fov_deg=-15.0,
    horizontal_fov_deg=360.0,
    noise_stddev_m=0.02,
    dropoff_rate=0.0,
)

# Intel RealSense D435：RGB 1280×720 / HFOV 69° / 30fps
_D435_RGB: Final[RGBCameraConfig] = RGBCameraConfig(
    width=1280,
    height=720,
    fov_deg=69.0,
    fps=30.0,
    noise_intensity=0.0,
    motion_blur=False,
)

# Intel RealSense D435：Depth 1280×720 / HFOV 87° / 0.2–10m
_D435_DEPTH: Final[DepthCameraConfig] = DepthCameraConfig(
    width=1280,
    height=720,
    fov_deg=87.0,
    fps=30.0,
    min_depth_m=0.2,
    max_depth_m=10.0,
)

# CH10X IMU：100Hz / accel σ=0.01 m/s² / gyro σ=0.001 rad/s
_CH10X_IMU: Final[IMUConfig] = IMUConfig(
    frequency_hz=100.0,
    accel_noise_stddev=0.01,
    gyro_noise_stddev=0.001,
    accel_bias_drift=0.0,
    gyro_bias_drift=0.0,
)


HUNTER_SE_PROFILES: Final[dict[str, SensorConfigBundle]] = {
    LIDAR_PROFILE: SensorConfigBundle(
        profile_name=LIDAR_PROFILE,
        lidar=_HELIOS_16P_LIDAR,
    ),
    CAMERA_PROFILE: SensorConfigBundle(
        profile_name=CAMERA_PROFILE,
        rgb_camera=_D435_RGB,
        depth_camera=_D435_DEPTH,
    ),
    IMU_PROFILE: SensorConfigBundle(
        profile_name=IMU_PROFILE,
        imu=_CH10X_IMU,
    ),
    DEFAULT_PROFILE: SensorConfigBundle(
        profile_name=DEFAULT_PROFILE,
        lidar=_HELIOS_16P_LIDAR,
        rgb_camera=_D435_RGB,
        depth_camera=_D435_DEPTH,
        imu=_CH10X_IMU,
        # 事件传感器不属型号剖面，但场景事件依赖其回调；默认全挂且在无蓝图时跳过
        collision=EventSensorConfig(),
        lane_invasion=EventSensorConfig(),
        obstacle=EventSensorConfig(),
    ),
}


def get_profile(name: str) -> SensorConfigBundle:
    """取指定剖面的传感器配置（未知名称回退默认剖面）。

    Args:
        name: 剖面名（``helios_16p`` / ``d435`` / ``ch10x`` / ``hunter_se_full``）。

    Returns:
        传感器配置集合（副本，调用方可安全修改而不污染全局剖面）。
    """
    bundle = HUNTER_SE_PROFILES.get(name.strip().lower()) if name else None
    if bundle is None:
        bundle = HUNTER_SE_PROFILES[DEFAULT_PROFILE]
    # model_copy(deep=True) 避免调用方原地修改配置文件对象
    return bundle.model_copy(deep=True)


def resolve_profiles(names: list[str]) -> SensorConfigBundle:
    """把多个剖面合并为一个传感器配置集合（后者覆盖前者的同类型配置）。

    场景允许 ``sensors: [{profile: helios_16p}, {profile: d435}]`` 分条声明，
    此处合并成一次蓝图创建所需的单一 bundle。

    Args:
        names: 剖面名列表（空列表等价于默认剖面）。

    Returns:
        合并后的传感器配置集合。
    """
    if not names:
        return get_profile(DEFAULT_PROFILE)
    merged = SensorConfigBundle()
    for name in names:
        partial = get_profile(name)
        for field in _BUNDLE_FIELDS:
            value = getattr(partial, field)
            if value is not None:
                setattr(merged, field, value)
    # 合并结果的标识用于回传/日志，多剖面时以加号连接
    merged.profile_name = "+".join(names)
    return merged


# SensorConfigBundle 的传感器字段（顺序即覆盖优先级：后写覆盖先写）
_BUNDLE_FIELDS: Final[tuple[str, ...]] = (
    "lidar",
    "rgb_camera",
    "depth_camera",
    "imu",
    "gnss",
    "collision",
    "lane_invasion",
    "obstacle",
)


__all__ = [
    "CAMERA_PROFILE",
    "DEFAULT_PROFILE",
    "HUNTER_SE_PROFILES",
    "IMU_PROFILE",
    "LIDAR_PROFILE",
    "get_profile",
    "resolve_profiles",
]
