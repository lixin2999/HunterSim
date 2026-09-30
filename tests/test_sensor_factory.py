"""传感器配置工厂单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Optional

import pytest

from hunter_sim.common.exceptions import SensorSimulationError
from hunter_sim.common.models import SensorType
from hunter_sim.sensor_sim.sensor_factory import (
    DepthCameraConfig,
    IMUConfig,
    LidarConfig,
    RGBCameraConfig,
    SensorBlueprintFactory,
    SensorConfigBundle,
)


class _StubBP:
    def __init__(self, type_id: str) -> None:
        self.id = type_id
        self.attributes: dict[str, str] = {}

    def set_attribute(self, key: str, value: str) -> None:
        self.attributes[key] = value


class _StubLib:
    """返回任意传感器蓝图名的 stub 库。"""

    def __init__(self, missing: Optional[set[str]] = None) -> None:
        self._missing = missing or set()

    def find(self, type_id: str) -> Optional[_StubBP]:
        if type_id in self._missing:
            return None
        return _StubBP(type_id)


class TestSensorConfigs:
    def test_lidar_defaults(self) -> None:
        c = LidarConfig()
        assert c.channels == 16
        assert c.range_m == 150.0

    def test_lidar_validation(self) -> None:
        with pytest.raises(Exception):
            LidarConfig(channels=0)
        with pytest.raises(Exception):
            LidarConfig(dropoff_rate=2.0)

    def test_hunter_se_default_bundle(self) -> None:
        b = SensorConfigBundle.hunter_se_default()
        assert b.lidar is not None
        assert b.rgb_camera is not None
        assert b.imu is not None
        assert b.gnss is None


class TestSensorBlueprintFactory:
    def test_create_lidar_sets_attributes(self) -> None:
        factory = SensorBlueprintFactory(_StubLib())
        bp = factory.create(SensorType.LIDAR, LidarConfig(channels=32, range_m=100.0))
        assert bp.id == "sensor.lidar.ray_cast"
        assert bp.attributes["channels"] == "32"
        assert bp.attributes["range"] == "100.0"

    def test_lidar_noise_and_dropoff(self) -> None:
        factory = SensorBlueprintFactory(_StubLib())
        bp = factory.create(SensorType.LIDAR, LidarConfig(noise_stddev_m=0.05, dropoff_rate=0.3))
        assert bp.attributes["noise_stddev"] == "0.05"
        assert bp.attributes["dropoff_general_rate"] == "0.3"

    def test_create_rgb(self) -> None:
        factory = SensorBlueprintFactory(_StubLib())
        bp = factory.create(SensorType.RGB_CAMERA, RGBCameraConfig(width=1920, height=1080, fov_deg=90.0))
        assert bp.attributes["image_size_x"] == "1920"
        assert bp.attributes["image_size_y"] == "1080"
        assert bp.attributes["fov"] == "90.0"

    def test_create_depth(self) -> None:
        factory = SensorBlueprintFactory(_StubLib())
        bp = factory.create(SensorType.DEPTH_CAMERA, DepthCameraConfig())
        assert bp.id == "sensor.camera.depth"

    def test_create_imu_no_attributes(self) -> None:
        factory = SensorBlueprintFactory(_StubLib())
        bp = factory.create(SensorType.IMU, IMUConfig())
        assert bp.id == "sensor.other.imu"
        assert bp.attributes == {}

    def test_missing_blueprint_raises(self) -> None:
        factory = SensorBlueprintFactory(_StubLib(missing={"sensor.lidar.ray_cast"}))
        with pytest.raises(SensorSimulationError):
            factory.create(SensorType.LIDAR, LidarConfig())

    def test_create_bundle_skips_none(self) -> None:
        factory = SensorBlueprintFactory(_StubLib())
        bundle = SensorConfigBundle(
            lidar=LidarConfig(),
            imu=IMUConfig(),
            rgb_camera=None,
        )
        result = factory.create_bundle(bundle)
        assert SensorType.LIDAR in result
        assert SensorType.IMU in result
        assert SensorType.RGB_CAMERA not in result

    def test_create_bundle_swallows_blueprint_error(self) -> None:
        factory = SensorBlueprintFactory(_StubLib(missing={"sensor.other.imu"}))
        bundle = SensorConfigBundle(lidar=LidarConfig(), imu=IMUConfig())
        result = factory.create_bundle(bundle)
        assert SensorType.LIDAR in result
        assert SensorType.IMU not in result
