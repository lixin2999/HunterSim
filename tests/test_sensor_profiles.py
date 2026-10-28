"""传感器型号剖面测试（V3.0.0）。

覆盖计划 §6.1：
- 三套型号剖面的关键数值（Helios-16P / D435 / CH10X）；
- 默认剖面三件套齐备且含事件传感器；
- ``configs/sensor_configs.json`` 与 ``profiles.py`` 双向一致（含 ``components`` 组合）；
- ``get_profile`` / ``resolve_profiles`` 的合并与回退语义、深拷贝隔离。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hunter_sim.sensor_sim.profiles import (
    CAMERA_PROFILE,
    DEFAULT_PROFILE,
    HUNTER_SE_PROFILES,
    IMU_PROFILE,
    LIDAR_PROFILE,
    get_profile,
    resolve_profiles,
)
from hunter_sim.sensor_sim.sensor_factory import SensorConfigBundle

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "sensor_configs.json"

# JSON 中的剖面字段名 -> SensorConfigBundle 属性名（同名字段直接映射）
_SENSOR_FIELDS = (
    "lidar",
    "rgb_camera",
    "depth_camera",
    "imu",
    "gnss",
    "collision",
    "lane_invasion",
    "obstacle",
)


def _load_json() -> dict[str, Any]:
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


def _resolved_json_profiles(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把 JSON profiles 展开：``components`` 组合项按声明顺序合并。"""
    raw: dict[str, dict[str, Any]] = data["profiles"]
    resolved: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        merged: dict[str, Any] = {}
        for component in entry.get("components", []):
            for field in _SENSOR_FIELDS:
                if field in raw[component]:
                    merged[field] = raw[component][field]
        for field in _SENSOR_FIELDS:
            if field in entry:
                merged[field] = entry[field]
        resolved[name] = merged
    return resolved


# ── 型号数值 ──────────────────────────────────────────────────────────────────


class TestProfilesValues:
    def test_helios_16p_numbers(self) -> None:
        bundle = HUNTER_SE_PROFILES[LIDAR_PROFILE]
        assert bundle.profile_name == "helios_16p"
        assert bundle.lidar is not None
        lidar = bundle.lidar
        assert lidar.channels == 16
        assert lidar.range_m == pytest.approx(150.0)
        assert lidar.points_per_second == 131072
        assert lidar.rotation_frequency_hz == pytest.approx(10.0)
        assert lidar.upper_fov_deg == pytest.approx(15.0)
        assert lidar.lower_fov_deg == pytest.approx(-15.0)
        assert lidar.noise_stddev_m == pytest.approx(0.02)

    def test_d435_numbers(self) -> None:
        bundle = HUNTER_SE_PROFILES[CAMERA_PROFILE]
        assert bundle.rgb_camera is not None
        assert bundle.depth_camera is not None
        rgb = bundle.rgb_camera
        assert (rgb.width, rgb.height) == (1280, 720)
        assert rgb.fov_deg == pytest.approx(69.0)
        assert rgb.fps == pytest.approx(30.0)
        depth = bundle.depth_camera
        assert (depth.width, depth.height) == (1280, 720)
        assert depth.fov_deg == pytest.approx(87.0)
        assert depth.min_depth_m == pytest.approx(0.2)
        assert depth.max_depth_m == pytest.approx(10.0)

    def test_ch10x_numbers(self) -> None:
        bundle = HUNTER_SE_PROFILES[IMU_PROFILE]
        assert bundle.imu is not None
        imu = bundle.imu
        assert imu.frequency_hz == pytest.approx(100.0)
        assert imu.accel_noise_stddev == pytest.approx(0.01)
        assert imu.gyro_noise_stddev == pytest.approx(0.001)

    def test_single_model_profiles_only_enable_own_sensor(self) -> None:
        assert HUNTER_SE_PROFILES[LIDAR_PROFILE].rgb_camera is None
        assert HUNTER_SE_PROFILES[CAMERA_PROFILE].lidar is None
        assert HUNTER_SE_PROFILES[IMU_PROFILE].lidar is None

    def test_default_profile_mounts_three_sensors(self) -> None:
        bundle = HUNTER_SE_PROFILES[DEFAULT_PROFILE]
        assert bundle.lidar is not None
        assert bundle.rgb_camera is not None
        assert bundle.depth_camera is not None
        assert bundle.imu is not None

    def test_default_profile_keeps_event_sensors(self) -> None:
        """场景事件依赖碰撞/车道偏离回调，默认剖面必须保留。"""
        bundle = HUNTER_SE_PROFILES[DEFAULT_PROFILE]
        assert bundle.collision is not None
        assert bundle.lane_invasion is not None
        assert bundle.obstacle is not None

    def test_no_gnss_by_default(self) -> None:
        """GNSS 非实车必需传感器，默认不挂（避免缺少定位回调时误报）。"""
        assert HUNTER_SE_PROFILES[DEFAULT_PROFILE].gnss is None


# ── 与配置文件一致 ────────────────────────────────────────────────────────────


class TestConfigJsonConsistency:
    def test_json_file_exists(self) -> None:
        assert _CONFIG_PATH.is_file()

    def test_default_profile_matches_json(self) -> None:
        data = _load_json()
        assert data["default_profile"] == DEFAULT_PROFILE

    def test_all_profiles_present_in_json(self) -> None:
        data = _load_json()
        assert set(data["profiles"]) == set(HUNTER_SE_PROFILES)

    @pytest.mark.parametrize("name", list(HUNTER_SE_PROFILES))
    def test_profile_fields_match_json(self, name: str) -> None:
        """逐字段断言 profiles.py 与 sensor_configs.json 数值一致。"""
        resolved = _resolved_json_profiles(_load_json())[name]
        bundle: SensorConfigBundle = HUNTER_SE_PROFILES[name]

        assert bundle.profile_name == name
        for field in _SENSOR_FIELDS:
            expected = resolved.get(field)
            actual = getattr(bundle, field)
            if expected is None:
                continue
            assert actual is not None, f"{name}.{field} 在 profiles.py 中缺失"
            dumped = actual.model_dump()
            for key, value in expected.items():
                assert key in dumped, f"{name}.{field}.{key} 不在配置模型中"
                mismatch = f"{name}.{field}.{key} 不一致：json={value} py={dumped[key]}"
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    assert dumped[key] == value, mismatch
                else:
                    assert dumped[key] == pytest.approx(value), mismatch


# ── 查询与合并 ────────────────────────────────────────────────────────────────


class TestProfileLookup:
    def test_get_known_profile(self) -> None:
        assert get_profile("d435").rgb_camera is not None

    def test_get_is_case_insensitive(self) -> None:
        assert get_profile("CH10X").imu is not None

    def test_unknown_profile_falls_back_to_default(self) -> None:
        bundle = get_profile("nonexistent")
        assert bundle.profile_name == DEFAULT_PROFILE

    def test_empty_name_falls_back_to_default(self) -> None:
        assert get_profile("").profile_name == DEFAULT_PROFILE

    def test_get_returns_detached_copy(self) -> None:
        """调用方修改返回值不得污染全局剖面。"""
        bundle = get_profile(LIDAR_PROFILE)
        assert bundle.lidar is not None
        bundle.lidar.channels = 4
        assert HUNTER_SE_PROFILES[LIDAR_PROFILE].lidar is not None
        assert HUNTER_SE_PROFILES[LIDAR_PROFILE].lidar.channels == 16


class TestResolveProfiles:
    def test_empty_list_returns_default(self) -> None:
        bundle = resolve_profiles([])
        assert bundle.rgb_camera is not None
        assert bundle.imu is not None

    def test_single_profile(self) -> None:
        bundle = resolve_profiles([LIDAR_PROFILE])
        assert bundle.lidar is not None
        assert bundle.rgb_camera is None

    def test_merges_multiple_profiles(self) -> None:
        bundle = resolve_profiles([LIDAR_PROFILE, CAMERA_PROFILE, IMU_PROFILE])
        assert bundle.lidar is not None
        assert bundle.rgb_camera is not None
        assert bundle.depth_camera is not None
        assert bundle.imu is not None

    def test_profile_name_marks_combination(self) -> None:
        bundle = resolve_profiles([LIDAR_PROFILE, IMU_PROFILE])
        assert bundle.profile_name == f"{LIDAR_PROFILE}+{IMU_PROFILE}"

    def test_later_profile_wins(self) -> None:
        """同类型配置后者覆盖前者（d435 之后再挂 hunter_se_full 应取后者相机）。"""
        bundle = resolve_profiles([CAMERA_PROFILE, DEFAULT_PROFILE])
        assert bundle.rgb_camera is not None
        assert bundle.rgb_camera.fov_deg == pytest.approx(69.0)
        assert bundle.lidar is not None

    def test_unknown_name_falls_back_inside_merge(self) -> None:
        bundle = resolve_profiles(["unknown"])
        assert bundle.lidar is not None
