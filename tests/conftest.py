"""pytest 公共 fixtures。

CARLA 相关代码在本阶段不涉及；后续层的 fixtures 将在此集中提供 mock。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

# 注入伪 carla 模块：L1 实现引用 carla 原生类型，但测试环境无 CARLA 包/服务端。
# 各实现均通过注入 mock world/client/vehicle 完成测试，构造函数仅需要存在即可。
if "carla" not in sys.modules:
    sys.modules["carla"] = MagicMock(name="carla")

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def configs_dir() -> Path:
    """示例配置目录路径。"""
    return PROJECT_ROOT / "configs"


@pytest.fixture()
def default_scenario_path(configs_dir: Path) -> Path:
    return configs_dir / "default_scenario.yaml"


@pytest.fixture()
def sensors_suite_path(configs_dir: Path) -> Path:
    return configs_dir / "sensors" / "full_suite.yaml"


@pytest.fixture()
def sample_image() -> np.ndarray:
    """构造一张合法的 RGB 测试图像 (H, W, 3) uint8。"""
    return np.zeros((8, 8, 3), dtype=np.uint8)


@pytest.fixture()
def sample_points() -> np.ndarray:
    """构造一段合法的 LiDAR 点云 (N, 4) float64。"""
    return np.zeros((16, 4), dtype=np.float64)


@pytest.fixture()
def sample_start_time() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture()
def sample_vehicle_state() -> object:
    """一个填充完整的 VehicleState 样本，供 L1/L2 测试复用。"""
    from hunter_sim.core.contracts import VehicleState

    return VehicleState(
        timestamp=1.0,
        frame_id=0,
        x=1.0,
        y=2.0,
        z=0.0,
        roll=0.0,
        pitch=0.0,
        yaw=90.0,
        velocity_x=5.0,
        velocity_y=0.0,
        velocity_z=0.0,
        acceleration_x=0.0,
        acceleration_y=0.0,
        acceleration_z=9.8,
        throttle=0.0,
        brake=0.0,
        steer=0.0,
        gear=1,
    )


@pytest.fixture()
def fake_carla_transform() -> SimpleNamespace:
    """模拟 carla.Transform 的读取结构（location/rotation 为数值属性）。"""
    return SimpleNamespace(
        location=SimpleNamespace(x=10.0, y=20.0, z=0.5),
        rotation=SimpleNamespace(pitch=0.0, yaw=90.0, roll=0.0),
    )
