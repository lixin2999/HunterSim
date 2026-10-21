"""桌面客户端非界面逻辑单测（V2.2.0，无 Qt）。

只测不依赖 PySide6 的纯逻辑：传感器预设（`sim_presets`）与相机帧转换
（`camera_bridge`）。Qt 界面组件本身不纳入单测。
"""

from __future__ import annotations

import numpy as np
import pytest

from hunter_sim.gui.camera_bridge import image_to_rgb_array, rgb_array_size
from hunter_sim.gui.sim_presets import (
    ALL_SENSOR_BLUEPRINTS,
    CAMERA_BLUEPRINTS,
    MOUNT_PRESETS,
    build_rgb_camera_config,
    resolve_mount_transform,
)
from hunter_sim.gui.theme import stylesheet
from hunter_sim.sensor_sim.sensor_factory import RGBCameraConfig


class FakeCarlaImage:
    """最小 carla.Image 桩：BGRA 字节 + 宽高。"""

    def __init__(self, width: int, height: int, bgra: bytes) -> None:
        self.width = width
        self.height = height
        self.data = bgra


def test_resolution_to_camera_config() -> None:
    """分辨率标签映射到 RGBCameraConfig 的宽高。"""
    cfg = build_rgb_camera_config("1280x720", fov_deg=100.0, fps=24.0)
    assert isinstance(cfg, RGBCameraConfig)
    assert cfg.width == 1280 and cfg.height == 720
    assert cfg.fov_deg == 100.0
    assert cfg.fps == 24.0


def test_unknown_resolution_raises() -> None:
    """未注册分辨率标签抛 KeyError。"""
    with pytest.raises(KeyError):
        build_rgb_camera_config("999x999")


def test_mount_presets_resolve() -> None:
    """预置安装位可按名解析且含相对位姿字段。"""
    preset = resolve_mount_transform("Third Person (Standard)")
    assert preset is MOUNT_PRESETS["Third Person (Standard)"]
    assert preset.z > 0
    assert preset.pitch < 0  # 后方俯视


def test_camera_blueprints_subset_of_all() -> None:
    """相机蓝图是所有传感器蓝图的子集。"""
    assert set(CAMERA_BLUEPRINTS.values()) <= set(ALL_SENSOR_BLUEPRINTS.values())
    assert "sensor.camera.rgb" in CAMERA_BLUEPRINTS.values()


def test_image_to_rgb_flips_rows() -> None:
    """BGRA→RGB 且自底向上翻正为自顶向下。"""
    # 2 行×2 列：第一存储行（自底向上=底部）红，第二行（顶部）绿
    width = 2
    bgra = bytes(
        [0, 0, 255, 0] * width  # 底部行 BGR=(255,0,0)→R
        + [0, 255, 0, 0] * width  # 顶部行 G
    )
    img = FakeCarlaImage(width=width, height=2, bgra=bgra)
    arr = image_to_rgb_array(img)
    assert arr.shape == (2, width, 3)
    assert arr.dtype == np.uint8
    # 翻正后顶行应为原始第二行（绿），底行为红
    assert tuple(arr[0, 0]) == (0, 255, 0)
    assert tuple(arr[1, 0]) == (255, 0, 0)
    assert rgb_array_size(arr) == (width, 2)


def test_stylesheet_known_and_fallback() -> None:
    """主题选择：已知返回对应 QSS，未知回落深色。"""
    dark = stylesheet("dark")
    light = stylesheet("light")
    assert dark != light
    assert "QWidget" in dark and "QWidget" in light
    assert stylesheet("nope") == dark
