"""核心数据契约（跨模块传递的不可变数据对象）。

约定（见开发提示词 §5.1 / §7.1）：

- 所有采集数据对象使用 ``pydantic.BaseModel`` 且 ``frozen=True``，保证不可变。
- 图像 / 点云等大块数值数据以 ``numpy.ndarray`` 承载，避免拷贝，使用引用传递。
- ``RunMetadata`` 描述单次运行状态，运行过程中需更新（帧数 / 状态 / 结束时间），
  故保持可变；禁止修改的仅是"已有字段语义"，新增只能添加可选字段。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

# 常用数值数组类型别名（仅用于类型标注，运行期不改变行为）。
FloatArray = NDArray[np.float64]
UInt8Image = NDArray[np.uint8]


class _FrozenBase(BaseModel):
    """所有不可变契约模型的公共基类。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class TimestampedData(_FrozenBase):
    """所有采集数据的基类，携带仿真时间戳。"""

    timestamp: float = Field(description="仿真时间戳（秒），从场景开始计时")
    frame_id: int = Field(ge=0, description="帧序号，从 0 开始")


class VehicleState(TimestampedData):
    """主车（Ego Vehicle）在某时刻的完整状态。"""

    x: float
    y: float
    z: float
    roll: float
    pitch: float
    yaw: float
    velocity_x: float
    velocity_y: float
    velocity_z: float
    acceleration_x: float
    acceleration_y: float
    acceleration_z: float
    throttle: float = Field(ge=0.0, le=1.0)
    brake: float = Field(ge=0.0, le=1.0)
    steer: float = Field(ge=-1.0, le=1.0)
    gear: int


class CameraFrame(TimestampedData):
    """相机传感器单帧数据。"""

    sensor_id: str
    image: UInt8Image = Field(description="RGB 图像, shape=(H, W, 3), dtype=uint8")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fov: float


class LidarFrame(TimestampedData):
    """LiDAR 单帧点云数据。"""

    sensor_id: str
    points: FloatArray = Field(description="点云, shape=(N, 4), [x, y, z, intensity]")
    point_count: int = Field(ge=0)


class ImuFrame(TimestampedData):
    """IMU 单帧数据。"""

    sensor_id: str
    accelerometer: tuple[float, float, float]
    gyroscope: tuple[float, float, float]
    compass: float


class GnssFrame(TimestampedData):
    """GNSS 单帧数据。"""

    sensor_id: str
    latitude: float
    longitude: float
    altitude: float


class RadarFrame(TimestampedData):
    """Radar 单帧数据（速度 + RCS 点迹）。"""

    sensor_id: str
    detections: FloatArray = Field(
        description="点迹数组, 每行 [velocity, azimuth, altitude_factor, rcs]"
    )
    detection_count: int = Field(ge=0)


class SynchronizedFrame(_FrozenBase):
    """时间同步后的统一数据帧，聚合同一时刻所有传感器数据。"""

    timestamp: float
    frame_id: int = Field(ge=0)
    vehicle_state: VehicleState
    cameras: dict[str, CameraFrame] = Field(default_factory=dict)
    lidars: dict[str, LidarFrame] = Field(default_factory=dict)
    radars: dict[str, RadarFrame] = Field(default_factory=dict)
    imus: dict[str, ImuFrame] = Field(default_factory=dict)
    gnss: dict[str, GnssFrame] = Field(default_factory=dict)


class RunMetadata(BaseModel):
    """单次运行的元数据（运行期间可更新，故不可冻结）。"""

    run_id: str
    scenario_name: str
    map_name: str
    start_time: datetime
    end_time: datetime | None = None
    duration_seconds: float | None = None
    sensor_configs: list[dict[str, Any]] = Field(default_factory=list)
    vehicle_config: dict[str, Any] = Field(default_factory=dict)
    weather_config: dict[str, Any] = Field(default_factory=dict)
    total_frames: int = 0
    status: str = "running"  # running / completed / failed

    def finalize(self, *, status: str, end_time: datetime) -> None:
        """结束运行并写入终止状态与时长。

        Args:
            status: 终止状态，``completed`` 或 ``failed``。
            end_time: 运行结束时间。
        """
        if self.status != "running":
            raise ValueError(f"运行已终止，当前状态: {self.status}")
        self.status = status
        self.end_time = end_time
        self.duration_seconds = (end_time - self.start_time).total_seconds()


__all__ = [
    "CameraFrame",
    "FloatArray",
    "GnssFrame",
    "ImuFrame",
    "LidarFrame",
    "RadarFrame",
    "RunMetadata",
    "SynchronizedFrame",
    "TimestampedData",
    "UInt8Image",
    "VehicleState",
]
