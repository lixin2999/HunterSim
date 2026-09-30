"""天气管理器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.engine.weather_manager import (
    PRESET_ENVIRONMENTS,
    WeatherManager,
    WeatherProfile,
    get_preset_profile,
)


class _MockWorld:
    """记录 set_weather 调用的最小 World Mock。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def set_weather(self, weather: Any) -> None:
        self.calls.append(weather)


class TestWeatherProfile:
    def test_is_night(self) -> None:
        assert WeatherProfile(sun_altitude_angle=-30.0).is_night is True
        assert WeatherProfile(sun_altitude_angle=45.0).is_night is False

    def test_is_rainy(self) -> None:
        assert WeatherProfile(precipitation=50.0).is_rainy is True
        assert WeatherProfile(precipitation=5.0).is_rainy is False

    def test_to_carla_dict_normalizes(self) -> None:
        d = WeatherProfile(cloudiness=80.0, fog_density=20.0, fog_distance=15.0).to_carla_dict()
        assert d["cloudiness"] == pytest.approx(0.8)
        assert d["fog_density"] == pytest.approx(0.2)
        assert d["fog_distance"] == 15.0

    def test_range_validation(self) -> None:
        with pytest.raises(Exception):
            WeatherProfile(cloudiness=200.0)


class TestPresetProfile:
    @pytest.mark.parametrize("name", PRESET_ENVIRONMENTS)
    def test_all_presets_loadable(self, name: str) -> None:
        p = get_preset_profile(name)
        assert isinstance(p, WeatherProfile)

    def test_returns_copy(self) -> None:
        a = get_preset_profile("sunny_noon")
        b = get_preset_profile("sunny_noon")
        assert a is not b

    def test_unknown_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            get_preset_profile("blizzard")


class TestWeatherManager:
    def setup_method(self) -> None:
        self.world = _MockWorld()
        self.mgr = WeatherManager(self.world)

    def test_initial_default(self) -> None:
        assert self.mgr.current_profile.preset_name == "sunny_noon"

    def test_current_profile_is_copy(self) -> None:
        p1 = self.mgr.current_profile
        p1.cloudiness = 99.0
        assert self.mgr.current_profile.cloudiness != 99.0

    def test_set_weather_immediate(self) -> None:
        self.mgr.set_weather(WeatherProfile(cloudiness=42.0, preset_name="custom"))
        assert self.mgr.current_profile.cloudiness == 42.0

    def test_set_preset(self) -> None:
        self.mgr.set_preset("night")
        assert self.mgr.current_profile.is_night is True

    def test_unknown_preset_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            self.mgr.set_preset("typhoon")

    def test_transition_in_progress_then_complete(self) -> None:
        target = WeatherProfile(cloudiness=100.0, preset_name="storm")
        self.mgr.start_transition(target, duration_seconds=2.0)
        # 立即推进：处于过渡中
        assert self.mgr.update_transition() is True
        # 手动回拨起始时间使其超过总时长（禁止 sleep）
        self.mgr._transition_start = time.perf_counter() - 10.0
        assert self.mgr.update_transition() is False
        assert self.mgr.current_profile.cloudiness == pytest.approx(100.0)

    def test_update_without_transition_returns_false(self) -> None:
        assert self.mgr.update_transition() is False

    def test_to_json_valid(self) -> None:
        data = json.loads(self.mgr.to_json())
        assert "cloudiness" in data

    def test_load_profiles_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        profiles = WeatherManager.load_profiles_from_file(tmp_path / "nope.json")
        assert "sunny_noon" in profiles

    def test_load_profiles_from_file(self, tmp_path: Path) -> None:
        f = tmp_path / "wx.json"
        f.write_text(json.dumps({"custom_a": {"cloudiness": 33.0}}), encoding="utf-8")
        profiles = WeatherManager.load_profiles_from_file(f)
        assert profiles["custom_a"].cloudiness == 33.0

    def test_load_profiles_bad_json_falls_back(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("{ not json", encoding="utf-8")
        profiles = WeatherManager.load_profiles_from_file(f)
        assert "sunny_noon" in profiles
