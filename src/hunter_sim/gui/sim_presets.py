"""桌面客户端传感器预设与参数（V2.2.0，无 Qt 依赖）。

集中管理"添加传感器"面板的下拉选项与预置安装位（对标 CarlaSceneX 的
Attach Type / Preset / Resolution），并把 GUI 输入转换为 `sensor_sim` 的蓝图配置，
便于脱离界面单测。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from hunter_sim.sensor_sim.sensor_factory import RGBCameraConfig

# CARLA 相机传感器蓝图（GUI 直连时按名过滤蓝图库）
CAMERA_BLUEPRINTS: dict[str, str] = {
    "RGB Camera": "sensor.camera.rgb",
    "Depth Camera": "sensor.camera.depth",
    "Semantic Segmentation": "sensor.camera.semantic_segmentation",
}

# 非相机传感器蓝图（LiDAR / IMU / GNSS）
OTHER_SENSOR_BLUEPRINTS: dict[str, str] = {
    "LiDAR": "sensor.lidar.ray_cast",
    "IMU": "sensor.other.imu",
    "GNSS": "sensor.other.gnss",
    "Collision": "sensor.other.collision",
    "Lane Invasion": "sensor.other.lane_invasion",
}

ALL_SENSOR_BLUEPRINTS: dict[str, str] = {**CAMERA_BLUEPRINTS, **OTHER_SENSOR_BLUEPRINTS}

# 挂载方式（CARLA attachment type）
ATTACH_TYPES: tuple[str, ...] = ("SpringArmGhost", "Rigid")

# 分辨率下拉（label -> (width, height)）
RESOLUTIONS: dict[str, tuple[int, int]] = {
    "640x360": (640, 360),
    "1280x720": (1280, 720),
    "1920x1080": (1920, 1080),
}


@dataclass(frozen=True)
class MountPreset:
    """传感器安装位预置（相对父 Actor 的位姿，角度为度）。

    Attributes:
        name: 预置显示名。
        x/y/z: 相对位置（米，CARLA 右手系：x 前、y 左、z 上）。
        pitch/yaw/roll: 相对旋转（度）。
    """

    name: str
    x: float
    y: float
    z: float
    pitch: float
    yaw: float
    roll: float = 0.0


# 预置安装位（对标截图 Preset 下拉）
MOUNT_PRESETS: dict[str, MountPreset] = {
    preset.name: preset
    for preset in (
        MountPreset("First Person (Cockpit)", x=1.0, y=0.0, z=1.6, pitch=0.0, yaw=0.0),
        MountPreset("Third Person (Standard)", x=-6.0, y=0.0, z=4.0, pitch=-15.0, yaw=0.0),
        MountPreset("Top Down", x=0.0, y=0.0, z=25.0, pitch=-90.0, yaw=0.0),
        MountPreset("HUNTER SE Roof", x=0.40, y=0.0, z=0.65, pitch=0.0, yaw=0.0),
        MountPreset("Front Bumper", x=0.9, y=0.0, z=0.3, pitch=-2.0, yaw=0.0),
    )
}

PRESET_NAMES: tuple[str, ...] = tuple(MOUNT_PRESETS)


def build_rgb_camera_config(
    label: str,
    fov_deg: float = 90.0,
    fps: float = 30.0,
) -> RGBCameraConfig:
    """把分辨率下拉标签 + FOV/FPS 转为 `RGBCameraConfig`。

    Args:
        label: `RESOLUTIONS` 的键（如 "1280x720"）。
        fov_deg: 视场角（度）。
        fps: 帧率。

    Returns:
        RGBCameraConfig 实例。

    Raises:
        KeyError: 分辨率标签未注册。
    """
    width, height = RESOLUTIONS[label]
    return RGBCameraConfig(width=width, height=height, fov_deg=fov_deg, fps=fps)


def resolve_mount_transform(preset_name: str) -> MountPreset:
    """按名称取安装位预置。

    Raises:
        KeyError: 预置名未注册。
    """
    return MOUNT_PRESETS[preset_name]


def choose_spawn_point_index(
    count: int,
    preferred: int | None = None,
    *,
    rng: random.Random | None = None,
) -> int | None:
    """在 `count` 个地图出生点中选一个索引（无 Qt 依赖，可脱离界面单测）。

    世界原点 (0,0) 在多数地图上不是合法出生点，`try_spawn_actor` 会返回 None；
    本函数支撑“自动挑一个合法出生点”的回退策略。

    Args:
        count: 出生点总数（`len(world.get_map().get_spawn_points())`）。
        preferred: 优先索引；落在 `[0, count)` 内则直接采用，否则忽略。
        rng: 随机源（测试可注入固定种子）；缺省用全局 `random`。

    Returns:
        选中的索引；`count <= 0`（地图无出生点）时返回 `None`。
    """
    if count <= 0:
        return None
    if preferred is not None and 0 <= preferred < count:
        return preferred
    return (rng if rng is not None else random).randrange(count)


def pick_spawn_point(
    spawn_points: Sequence[object],
    preferred: int | None = None,
    *,
    rng: random.Random | None = None,
) -> object | None:
    """从出生点序列（`carla.Transform` 列表）按 `choose_spawn_point_index` 取一个。

    Returns:
        选中的出生点对象；序列为空时返回 `None`。
    """
    idx = choose_spawn_point_index(len(spawn_points), preferred, rng=rng)
    return None if idx is None else spawn_points[idx]
