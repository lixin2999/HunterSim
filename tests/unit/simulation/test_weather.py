"""simulation.weather 单元测试（§3.4：天气参数与预设环境）。"""

from __future__ import annotations

import pytest

from hunter_sim.core.exceptions import ConfigurationError
from hunter_sim.simulation.weather import (
    WEATHER_PRESET_REGISTRY,
    PresetEnvironment,
    WeatherParameters,
    list_presets,
    resolve_preset,
)


def test_defaults_match_carla_ranges() -> None:
    params = WeatherParameters()
    assert params.cloudiness == 0.0
    assert params.precipitation == 0.0
    assert params.precipitation_deposits == 0.0
    assert params.wind_intensity == 0.0
    assert params.sun_azimuth_angle == 0.0
    assert params.sun_altitude_angle == 45.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cloudiness": 101.0},
        {"precipitation": -0.1},
        {"precipitation_deposits": 200.0},
        {"wind_intensity": -1.0},
        {"sun_azimuth_angle": 361.0},
        {"sun_altitude_angle": 91.0},
        {"sun_altitude_angle": -91.0},
    ],
)
def test_out_of_range_raises(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        WeatherParameters(**kwargs)


def test_boundary_values_accepted() -> None:
    WeatherParameters(
        cloudiness=100.0,
        precipitation=100.0,
        precipitation_deposits=100.0,
        wind_intensity=100.0,
        sun_azimuth_angle=360.0,
        sun_altitude_angle=-90.0,
    )


def test_registry_covers_all_presets() -> None:
    assert set(WEATHER_PRESET_REGISTRY) == set(PresetEnvironment)
    assert len(list_presets()) == 8


def test_registry_parameters_are_valid() -> None:
    for environment, preset in WEATHER_PRESET_REGISTRY.items():
        assert isinstance(preset.parameters, WeatherParameters)
        assert preset.environment is environment
        assert preset.description


@pytest.mark.parametrize(
    ("name", "cloudiness", "precipitation", "sun_altitude"),
    [
        ("clear_noon", 0.0, 0.0, 60.0),
        ("overcast", 80.0, 0.0, 45.0),
        ("light_rain", 30.0, 30.0, 45.0),
        ("heavy_rain", 50.0, 80.0, 30.0),
        ("foggy", 100.0, 0.0, 20.0),
        ("night", 100.0, 0.0, -15.0),
        ("dusk", 20.0, 0.0, 5.0),
        ("dawn", 20.0, 0.0, 10.0),
    ],
)
def test_preset_table_values(
    name: str, cloudiness: float, precipitation: float, sun_altitude: float
) -> None:
    """§3.4.2 预设表格核心维度：云量 / 降雨 / 太阳高度角。"""
    params = resolve_preset(name)
    assert params.cloudiness == cloudiness
    assert params.precipitation == precipitation
    assert params.sun_altitude_angle == sun_altitude


def test_resolve_unknown_preset_raises() -> None:
    with pytest.raises(ConfigurationError):
        resolve_preset("blizzard")
