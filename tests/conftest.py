"""pytest 测试配置和 fixtures（PROMPT-TEST-001）。

提供全局 fixture：
- MockCarlaClient / MockWorld / MockVehicle
- HunterSimSettings（测试环境）
- FastAPI TestClient
- 各模块服务实例
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# 将 src 目录加入 Python 路径
_src = Path(__file__).parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from hunter_sim.common.models import (  # noqa: E402
    CarlaSettings,
    HunterSimSettings,
    KafkaSettings,
    ResourceSettings,
    Transform,
    VILSettings,
    VehicleState,
)
from hunter_sim.common.utils import RingBuffer  # noqa: E402
from tests.mocks.carla_mocks import (  # noqa: E402
    MockCarlaClient,
    MockVehicle,
    MockWorld,
)

# ─── 配置 Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_settings() -> HunterSimSettings:
    """测试环境全局配置（不连接真实服务）。"""
    return HunterSimSettings(
        env="dev",
        log_level="DEBUG",
        carla=CarlaSettings(host="127.0.0.1", rpc_port=2000),
        kafka=KafkaSettings(bootstrap_servers="localhost:9092"),
        vil=VILSettings(extrapolation_ms=150),
        resource=ResourceSettings(instance_max_lifetime_seconds=60),
    )

# ─── CARLA Mock Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mock_client() -> MockCarlaClient:
    """CARLA Client Mock。"""
    return MockCarlaClient()


@pytest.fixture
def mock_world() -> MockWorld:
    """CARLA World Mock（Town03）。"""
    return MockWorld("Town03")


@pytest.fixture
def mock_vehicle() -> MockVehicle:
    """CARLA Vehicle Actor Mock。"""
    return MockVehicle("vehicle.hunter_se")

# ─── 数据模型 Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def sample_transform() -> Transform:
    """标准测试位姿。"""
    return Transform(x=10.0, y=5.0, z=0.0, pitch=0.0, yaw=0.5, roll=0.0)


@pytest.fixture
def sample_vehicle_state(sample_transform: Transform) -> VehicleState:
    """标准测试车辆状态。"""
    return VehicleState(
        time_stamp=1000000.0,
        transform=sample_transform,
        velocity=(1.0, 0.5, 0.0),
        acceleration=(0.1, 0.0, 0.0),
        angular_velocity=(0.0, 0.0, 0.1),
        steering=0.2,
        throttle=0.5,
        brake=0.0,
        gear=1,
        vehicle_speed=3.0,
    )


@pytest.fixture
def sample_telemetry_frames() -> list[dict[str, Any]]:
    """10 帧模拟遥测数据（匀加速直线运动）。"""
    frames = []
    for i in range(10):
        t = i * 0.1
        x = 0.5 * 1.0 * t * t  # 匀加速 1 m/s²
        frames.append(
            {
                "timestamp": 1000.0 + t,
                "position": {"x": x, "y": 0.0, "z": 0.0},
                "rotation": {"pitch": 0.0, "yaw": 0.0, "roll": 0.0},
                "velocity": {"x": 1.0 * t, "y": 0.0, "z": 0.0, "speed": 1.0 * t},
                "angular_velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
                "steering": 0.0,
                "throttle": 0.5,
                "brake": 0.0,
                "gear": 1,
            }
        )
    return frames

# ─── RingBuffer Fixture ───────────────────────────────────────────────────────


@pytest.fixture
def ring_buffer() -> RingBuffer[int]:
    """测试用 RingBuffer，大小 5。"""
    return RingBuffer(max_size=5)

# ─── FastAPI TestClient ──────────────────────────────────────────────────────

# V3.0.0：8080 治理面（auth/instances/scenes 路由）已移除，共享 TestClient 与
# JWT header fixture 随之删除；控制面端点测试改为在各自模块内注入 fake service
# 后调 ``create_app``（见 tests/test_platform_adapter_app.py）。
