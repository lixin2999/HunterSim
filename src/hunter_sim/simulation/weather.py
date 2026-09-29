"""天气与环境系统数据模型（模块 3.4）。

定义 CARLA 天气参数、预设环境枚举与预设注册表。所有模型均为不可变
``@dataclass(slots=True, frozen=True)`` 或 ``StrEnum``，不暴露 ``carla.*`` 原生类型，
供 :class:`~hunter_sim.simulation.protocols.ScenarioManager` 实现层构建
``carla.WeatherParameters`` 时消费。

内容对应开发提示词 §3.4：

- 3.4.1 天气参数：``cloudiness`` / ``precipitation`` / ``precipitation_deposits`` /
  ``wind_intensity`` / ``sun_azimuth_angle`` / ``sun_altitude_angle`` 六项，量程与
  CARLA ``WeatherParameters`` 对齐（0-100 / 0-360 / -90~90）；
- 3.4.2 预设环境：晴天正午 / 阴天 / 小雨 / 大雨 / 雾天 / 夜间 / 黄昏 / 黎明 八项注册表。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hunter_sim.core.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class WeatherParameters:
    """天气与环境光照参数（映射到 ``carla.WeatherParameters``，§3.4.1）。

    Attributes:
        cloudiness: 云量 ``[0, 100]``。
        precipitation: 降雨量 ``[0, 100]``。
        precipitation_deposits: 路面积水 ``[0, 100]``（影响路面反光/湿滑）。
        wind_intensity: 风力 ``[0, 100]``。
        sun_azimuth_angle: 太阳方位角 ``[0, 360]``（度）。
        sun_altitude_angle: 太阳高度角 ``[-90, 90]``（度，负值表示日照低于地平线）。
    """

    cloudiness: float = 0.0
    precipitation: float = 0.0
    precipitation_deposits: float = 0.0
    wind_intensity: float = 0.0
    sun_azimuth_angle: float = 0.0
    sun_altitude_angle: float = 45.0

    def __post_init__(self) -> None:
        """校验天气参数取值范围（与 CARLA 服务端量程一致）。"""
        for name in (
            "cloudiness",
            "precipitation",
            "precipitation_deposits",
            "wind_intensity",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 100.0:
                raise ValueError(f"{name} 越界（应为 0-100）: {value}")
        if not 0.0 <= self.sun_azimuth_angle <= 360.0:
            raise ValueError(f"sun_azimuth_angle 越界（应为 0-360）: {self.sun_azimuth_angle}")
        if not -90.0 <= self.sun_altitude_angle <= 90.0:
            raise ValueError(f"sun_altitude_angle 越界（应为 -90~90）: {self.sun_altitude_angle}")


class PresetEnvironment(StrEnum):
    """预设环境标识，取值为配置中可引用的环境名（§3.4.2）。"""

    CLEAR_NOON = "clear_noon"
    OVERCAST = "overcast"
    LIGHT_RAIN = "light_rain"
    HEAVY_RAIN = "heavy_rain"
    FOGGY = "foggy"
    NIGHT = "night"
    DUSK = "dusk"
    DAWN = "dawn"


@dataclass(frozen=True, slots=True)
class WeatherPreset:
    """预设环境描述：环境名、说明与完整天气参数。

    Attributes:
        environment: 预设环境枚举。
        description: 环境说明（如「标准测试环境」「低能见度」）。
        parameters: 该预设对应的完整天气参数。
    """

    environment: PresetEnvironment
    description: str
    parameters: WeatherParameters


# 预设环境注册表（§3.4.2）。表格仅规定 cloudiness / precipitation / sun_altitude_angle
# 三项核心维度，其余（积水、风力、方位角）按环境语义补全为合理默认值。
WEATHER_PRESET_REGISTRY: dict[PresetEnvironment, WeatherPreset] = {
    PresetEnvironment.CLEAR_NOON: WeatherPreset(
        environment=PresetEnvironment.CLEAR_NOON,
        description="标准测试环境",
        parameters=WeatherParameters(
            cloudiness=0.0,
            precipitation=0.0,
            precipitation_deposits=0.0,
            wind_intensity=5.0,
            sun_azimuth_angle=0.0,
            sun_altitude_angle=60.0,
        ),
    ),
    PresetEnvironment.OVERCAST: WeatherPreset(
        environment=PresetEnvironment.OVERCAST,
        description="漫射光",
        parameters=WeatherParameters(
            cloudiness=80.0,
            precipitation=0.0,
            precipitation_deposits=0.0,
            wind_intensity=5.0,
            sun_azimuth_angle=0.0,
            sun_altitude_angle=45.0,
        ),
    ),
    PresetEnvironment.LIGHT_RAIN: WeatherPreset(
        environment=PresetEnvironment.LIGHT_RAIN,
        description="湿滑路面",
        parameters=WeatherParameters(
            cloudiness=30.0,
            precipitation=30.0,
            precipitation_deposits=30.0,
            wind_intensity=10.0,
            sun_azimuth_angle=0.0,
            sun_altitude_angle=45.0,
        ),
    ),
    PresetEnvironment.HEAVY_RAIN: WeatherPreset(
        environment=PresetEnvironment.HEAVY_RAIN,
        description="能见度降低",
        parameters=WeatherParameters(
            cloudiness=50.0,
            precipitation=80.0,
            precipitation_deposits=80.0,
            wind_intensity=40.0,
            sun_azimuth_angle=0.0,
            sun_altitude_angle=30.0,
        ),
    ),
    PresetEnvironment.FOGGY: WeatherPreset(
        environment=PresetEnvironment.FOGGY,
        description="低能见度",
        parameters=WeatherParameters(
            cloudiness=100.0,
            precipitation=0.0,
            precipitation_deposits=0.0,
            wind_intensity=5.0,
            sun_azimuth_angle=0.0,
            sun_altitude_angle=20.0,
        ),
    ),
    PresetEnvironment.NIGHT: WeatherPreset(
        environment=PresetEnvironment.NIGHT,
        description="低照度，需车灯",
        parameters=WeatherParameters(
            cloudiness=100.0,
            precipitation=0.0,
            precipitation_deposits=0.0,
            wind_intensity=5.0,
            sun_azimuth_angle=0.0,
            sun_altitude_angle=-15.0,
        ),
    ),
    PresetEnvironment.DUSK: WeatherPreset(
        environment=PresetEnvironment.DUSK,
        description="逆光",
        parameters=WeatherParameters(
            cloudiness=20.0,
            precipitation=0.0,
            precipitation_deposits=0.0,
            wind_intensity=5.0,
            sun_azimuth_angle=270.0,
            sun_altitude_angle=5.0,
        ),
    ),
    PresetEnvironment.DAWN: WeatherPreset(
        environment=PresetEnvironment.DAWN,
        description="低角度光照",
        parameters=WeatherParameters(
            cloudiness=20.0,
            precipitation=0.0,
            precipitation_deposits=0.0,
            wind_intensity=5.0,
            sun_azimuth_angle=90.0,
            sun_altitude_angle=10.0,
        ),
    ),
}


def list_presets() -> list[WeatherPreset]:
    """返回全部预设环境（按注册表定义顺序，§3.4.2）。"""
    return list(WEATHER_PRESET_REGISTRY.values())


def resolve_preset(name: str) -> WeatherParameters:
    """按环境名解析预设天气参数。

    Args:
        name: 预设环境名（``PresetEnvironment`` 取值，如 ``heavy_rain``）。

    Returns:
        对应的 :class:`WeatherParameters`。

    Raises:
        ConfigurationError: 名称不是合法的预设环境。
    """
    for environment, preset in WEATHER_PRESET_REGISTRY.items():
        if environment.value == name:
            return preset.parameters
    valid = ", ".join(member.value for member in PresetEnvironment)
    raise ConfigurationError(f"未知天气预设环境: {name}（可选: {valid}）")


__all__ = [
    "WEATHER_PRESET_REGISTRY",
    "PresetEnvironment",
    "WeatherParameters",
    "WeatherPreset",
    "list_presets",
    "resolve_preset",
]
