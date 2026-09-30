"""传感器配置工厂（PROMPT-ENG-004-A）。

根据传感器类型和参数生成 CARLA ActorBlueprint，
支持 LiDAR、RGB 相机、深度相机、IMU、GNSS、事件传感器。
所有参数均可通过 sensor_configs.json 配置，支持运行时修改。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from hunter_sim.common.exceptions import ConfigurationError, SensorSimulationError
from hunter_sim.common.models import SensorType
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# ─── 传感器参数模型 ───────────────────────────────────────────────────────────


class LidarConfig(BaseModel):
    """LiDAR 传感器参数。

    Attributes:
        channels: 激光线数。
        range_m: 最大量程（米）。
        points_per_second: 每秒点数。
        rotation_frequency_hz: 旋转频率（Hz）。
        upper_fov_deg: 上部视场角（度）。
        lower_fov_deg: 下部视场角（度）。
        horizontal_fov_deg: 水平视场角（度）。
        noise_stddev_m: 高斯噪声标准差（米）。
        dropoff_rate: 雨天衰减率 (0~1)。
    """

    channels: int = Field(16, ge=1, le=128)
    range_m: float = Field(150.0, gt=0.0, le=300.0)
    points_per_second: int = Field(300000, ge=1000)
    rotation_frequency_hz: float = Field(10.0, gt=0.0, le=30.0)
    upper_fov_deg: float = Field(15.0, ge=0.0, le=90.0)
    lower_fov_deg: float = Field(-15.0, ge=-90.0, le=0.0)
    horizontal_fov_deg: float = Field(360.0, gt=0.0, le=360.0)
    noise_stddev_m: float = Field(0.02, ge=0.0)
    dropoff_rate: float = Field(0.0, ge=0.0, le=1.0)


class RGBCameraConfig(BaseModel):
    """RGB 相机参数（对应 Intel D435 RGB 传感器）。

    Attributes:
        width: 图像宽度（像素）。
        height: 图像高度（像素）。
        fov_deg: 视场角（度）。
        fps: 帧率（Hz）。
        noise_intensity: 高斯噪声强度 (0~1)。
        motion_blur: 是否启用运动模糊。
    """

    width: int = Field(1280, ge=64, le=3840)
    height: int = Field(720, ge=64, le=2160)
    fov_deg: float = Field(69.0, gt=0.0, le=180.0)
    fps: float = Field(30.0, gt=0.0, le=120.0)
    noise_intensity: float = Field(0.0, ge=0.0, le=1.0)
    motion_blur: bool = False


class DepthCameraConfig(BaseModel):
    """深度相机参数（对应 Intel D435 深度传感器）。

    Attributes:
        width: 图像宽度。
        height: 图像高度。
        fov_deg: 视场角（度）。
        fps: 帧率（Hz）。
        max_depth_m: 最大测距（米）。
    """

    width: int = Field(1280, ge=64, le=3840)
    height: int = Field(720, ge=64, le=2160)
    fov_deg: float = Field(85.0, gt=0.0, le=180.0)
    fps: float = Field(30.0, gt=0.0, le=60.0)
    max_depth_m: float = Field(10.0, gt=0.0)


class IMUConfig(BaseModel):
    """IMU 惯性测量单元参数。

    Attributes:
        frequency_hz: 输出频率（Hz）。
        accel_noise_stddev: 加速度计噪声标准差（m/s²）。
        gyro_noise_stddev: 陀螺仪噪声标准差（rad/s）。
        accel_bias_drift: 加速度计零偏漂移。
        gyro_bias_drift: 陀螺仪零偏漂移。
    """

    frequency_hz: float = Field(100.0, gt=0.0, le=1000.0)
    accel_noise_stddev: float = Field(0.01, ge=0.0)
    gyro_noise_stddev: float = Field(0.001, ge=0.0)
    accel_bias_drift: float = Field(0.0, ge=0.0)
    gyro_bias_drift: float = Field(0.0, ge=0.0)


class GNSSConfig(BaseModel):
    """GNSS 定位传感器参数。

    Attributes:
        frequency_hz: 输出频率。
        altitude_noise_stddev_m: 海拔噪声标准差（米）。
        lat_lon_noise_deg: 经纬度噪声（度）。
    """

    frequency_hz: float = Field(10.0, gt=0.0)
    altitude_noise_stddev_m: float = Field(0.5, ge=0.0)
    lat_lon_noise_deg: float = Field(0.0001, ge=0.0)


class EventSensorConfig(BaseModel):
    """事件传感器参数（碰撞、车道偏离、障碍物）。

    Attributes:
        collision_impulse_threshold: 碰撞触发阈值（N·s）。
        obstacle_detection_range_m: 障碍物检测距离（米）。
    """

    collision_impulse_threshold: float = Field(0.0, ge=0.0)
    obstacle_detection_range_m: float = Field(50.0, gt=0.0)


class SensorConfigBundle(BaseModel):
    """传感器全套配置集合。

    Attributes:
        lidar: LiDAR 配置（None 表示不启用）。
        rgb_camera: RGB 相机配置。
        depth_camera: 深度相机配置。
        imu: IMU 配置。
        gnss: GNSS 配置（可选）。
        collision: 碰撞传感器配置。
        lane_invasion: 车道偏离传感器配置。
        obstacle: 障碍物传感器配置。
    """

    lidar: Optional[LidarConfig] = None
    rgb_camera: Optional[RGBCameraConfig] = None
    depth_camera: Optional[DepthCameraConfig] = None
    imu: Optional[IMUConfig] = None
    gnss: Optional[GNSSConfig] = None
    collision: Optional[EventSensorConfig] = None
    lane_invasion: Optional[EventSensorConfig] = None
    obstacle: Optional[EventSensorConfig] = None

    @classmethod
    def hunter_se_default(cls) -> "SensorConfigBundle":
        """返回 HUNTER SE 标准传感器配置（设计文档 §6.2.5 含三种事件传感器）。"""
        return cls(
            lidar=LidarConfig(),
            rgb_camera=RGBCameraConfig(),
            depth_camera=DepthCameraConfig(),
            imu=IMUConfig(),
            collision=EventSensorConfig(),
            lane_invasion=EventSensorConfig(),
            obstacle=EventSensorConfig(),
        )


# ─── 蓝图工厂 ─────────────────────────────────────────────────────────────────


class SensorBlueprintFactory:
    """根据传感器类型和配置生成 CARLA ActorBlueprint。

    Args:
        blueprint_library: CARLA BlueprintLibrary 实例。
    """

    # CARLA 传感器蓝图名称映射
    _BLUEPRINT_NAMES: dict[SensorType, str] = {
        SensorType.LIDAR: "sensor.lidar.ray_cast",
        SensorType.RGB_CAMERA: "sensor.camera.rgb",
        SensorType.DEPTH_CAMERA: "sensor.camera.depth",
        SensorType.IMU: "sensor.other.imu",
        SensorType.GNSS: "sensor.other.gnss",
        SensorType.COLLISION: "sensor.other.collision",
        SensorType.LANE_INVASION: "sensor.other.lane_invasion",
        # 设计文档 §6.2.5：障碍物传感器使用 sensor.other.obstacle（distance=50）
        SensorType.OBSTACLE: "sensor.other.obstacle",
    }

    def __init__(self, blueprint_library: Any) -> None:
        self._lib = blueprint_library

    def create(self, sensor_type: SensorType, config: Any) -> Any:
        """创建指定类型的传感器蓝图并设置参数。

        Args:
            sensor_type: 传感器类型枚举。
            config: 对应的配置对象（LidarConfig/RGBCameraConfig 等）。

        Returns:
            CARLA ActorBlueprint 对象。

        Raises:
            SensorSimulationError: 蓝图创建失败。
        """
        bp_name = self._BLUEPRINT_NAMES.get(sensor_type)
        if bp_name is None:
            raise SensorSimulationError(
                sensor_type.value, "unknown", f"No blueprint for type {sensor_type}"
            )

        bp = self._lib.find(bp_name)
        if bp is None:
            raise SensorSimulationError(
                sensor_type.value, bp_name, f"Blueprint '{bp_name}' not found"
            )

        self._apply_attributes(bp, sensor_type, config)
        logger.debug(f"Sensor blueprint created: {sensor_type.value}")
        return bp

    def create_bundle(
        self, bundle: SensorConfigBundle
    ) -> dict[SensorType, Any]:
        """批量创建所有已启用传感器蓝图。

        Args:
            bundle: 传感器配置集合。

        Returns:
            SensorType -> ActorBlueprint 字典。
        """
        type_to_config: list[tuple[SensorType, Any]] = [
            (SensorType.LIDAR, bundle.lidar),
            (SensorType.RGB_CAMERA, bundle.rgb_camera),
            (SensorType.DEPTH_CAMERA, bundle.depth_camera),
            (SensorType.IMU, bundle.imu),
            (SensorType.GNSS, bundle.gnss),
            (SensorType.COLLISION, bundle.collision),
            (SensorType.LANE_INVASION, bundle.lane_invasion),
            (SensorType.OBSTACLE, bundle.obstacle),
        ]
        result: dict[SensorType, Any] = {}
        for stype, cfg in type_to_config:
            if cfg is not None:
                try:
                    result[stype] = self.create(stype, cfg)
                except SensorSimulationError as exc:
                    logger.warning(f"Skip sensor {stype.value}: {exc}")
        return result

    @staticmethod
    def _safe_set_attribute(bp: Any, name: str, value: str) -> None:
        """写入单个蓝图属性，属性不存在时仅告警（不同 CARLA 版本属性集有差异）。"""
        try:
            bp.set_attribute(name, value)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Blueprint attribute '{name}' set failed: {exc}")

    @classmethod
    def _apply_attributes(cls, bp: Any, sensor_type: SensorType, config: Any) -> None:
        """将配置参数写入蓝图属性（严格对齐设计文档 §6.2 各传感器配置代码）。"""
        if sensor_type == SensorType.LIDAR and isinstance(config, LidarConfig):
            cls._safe_set_attribute(bp, "channels", str(config.channels))
            cls._safe_set_attribute(bp, "range", str(config.range_m))
            cls._safe_set_attribute(bp, "points_per_second", str(config.points_per_second))
            cls._safe_set_attribute(bp, "rotation_frequency", str(config.rotation_frequency_hz))
            cls._safe_set_attribute(bp, "upper_fov", str(config.upper_fov_deg))
            cls._safe_set_attribute(bp, "lower_fov", str(config.lower_fov_deg))
            cls._safe_set_attribute(bp, "horizontal_fov", str(config.horizontal_fov_deg))
            if config.noise_stddev_m > 0:
                # 文档 §6.2.1/§6.4：距离高斯噪声 stddev=0.02m
                cls._safe_set_attribute(bp, "noise_stddev", str(config.noise_stddev_m))
            if config.dropoff_rate > 0:
                # 文档 §6.4：雨天距离衰减
                cls._safe_set_attribute(bp, "dropoff_general_rate", str(config.dropoff_rate))

        elif sensor_type == SensorType.RGB_CAMERA and isinstance(config, RGBCameraConfig):
            cls._safe_set_attribute(bp, "image_size_x", str(config.width))
            cls._safe_set_attribute(bp, "image_size_y", str(config.height))
            cls._safe_set_attribute(bp, "fov", str(config.fov_deg))
            # 文档 §6.2.2：相机帧率 fps=30
            cls._safe_set_attribute(bp, "fps", str(config.fps))
            # 文档 §6.4：运动模糊可配置（蓝图原生属性 motion_blur_intensity）
            if config.motion_blur:
                cls._safe_set_attribute(bp, "motion_blur_intensity", "0.45")
            # 注：相机高斯噪声非蓝图原生属性，需在数据回调中由 sensor 层手动注入

        elif sensor_type == SensorType.DEPTH_CAMERA and isinstance(config, DepthCameraConfig):
            cls._safe_set_attribute(bp, "image_size_x", str(config.width))
            cls._safe_set_attribute(bp, "image_size_y", str(config.height))
            cls._safe_set_attribute(bp, "fov", str(config.fov_deg))
            # 文档 §6.2.2：深度相机帧率 fps=30
            cls._safe_set_attribute(bp, "fps", str(config.fps))

        elif sensor_type == SensorType.IMU and isinstance(config, IMUConfig):
            # 文档 §6.2.3：三轴加速度计/陀螺仪噪声标准差 + sensor_tick（100Hz → 0.01）
            accel = str(config.accel_noise_stddev)
            gyro = str(config.gyro_noise_stddev)
            cls._safe_set_attribute(bp, "noise_accel_stddev_x", accel)
            cls._safe_set_attribute(bp, "noise_accel_stddev_y", accel)
            cls._safe_set_attribute(bp, "noise_accel_stddev_z", accel)
            cls._safe_set_attribute(bp, "noise_gyro_stddev_x", gyro)
            cls._safe_set_attribute(bp, "noise_gyro_stddev_y", gyro)
            cls._safe_set_attribute(bp, "noise_gyro_stddev_z", gyro)
            cls._safe_set_attribute(bp, "sensor_tick", str(1.0 / config.frequency_hz))

        elif sensor_type == SensorType.GNSS and isinstance(config, GNSSConfig):
            # 文档 §6.2.4：海拔/经纬度噪声标准差
            cls._safe_set_attribute(bp, "noise_alt_stddev", str(config.altitude_noise_stddev_m))
            cls._safe_set_attribute(bp, "noise_lat_stddev", str(config.lat_lon_noise_deg))
            cls._safe_set_attribute(bp, "noise_lon_stddev", str(config.lat_lon_noise_deg))
            cls._safe_set_attribute(bp, "sensor_tick", str(1.0 / config.frequency_hz))

        elif sensor_type == SensorType.OBSTACLE and isinstance(config, EventSensorConfig):
            # 文档 §6.2.5：障碍物探测距离 distance=50
            cls._safe_set_attribute(bp, "distance", str(config.obstacle_detection_range_m))

        elif sensor_type == SensorType.COLLISION and isinstance(config, EventSensorConfig):
            # 碰撞冲量阈值非蓝图属性，在事件回调中按 collision_impulse_threshold 过滤
            pass
