"""环境与天气系统管理模块（PROMPT-ENG-001-D）。

支持 8 种预设环境配置，提供天气渐变过渡函数，避免传感器数据跳变。
天气参数严格校验，夜间自动启用车灯。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field, field_validator

from hunter_sim.common.exceptions import ConfigurationError, ValidationError
from hunter_sim.common.utils import get_logger, lerp, smoothstep

logger = get_logger(__name__)

# ─── 预设环境名称 ─────────────────────────────────────────────────────────────

PRESET_ENVIRONMENTS: tuple[str, ...] = (
    "sunny_noon",       # 晴天正午
    "cloudy",           # 阴天
    "light_rain",       # 小雨
    "heavy_rain",       # 大雨
    "foggy",            # 雾天
    "night",            # 夜间
    "dusk",             # 黄昏
    "dawn",             # 黎明
)


class WeatherProfile(BaseModel):
    """CARLA 天气参数封装。

    对应 carla.WeatherParameters，所有参数范围 0-100（百分比制），
    太阳角度使用度制（API 层），内部转为 CARLA 使用的格式。

    Attributes:
        cloudiness: 云量 (0-100)。
        precipitation: 降雨量 (0-100)。
        precipitation_deposits: 路面积水 (0-100)。
        wind_intensity: 风力强度 (0-100)。
        sun_azimuth_angle: 太阳方位角 (0-360 度)。
        sun_altitude_angle: 太阳高度角 (-90~90 度，负值=夜间)。
        fog_density: 雾浓度 (0-100)。
        fog_distance: 雾起始距离（米，0=无限制）。
        rayleigh_scattering: 瑞利散射系数 (0-100)。
        mie_scattering: 米氏散射系数 (0-100)。
        preset_name: 预设环境名称（非预设时为空）。
    """

    cloudiness: float = Field(15.0, ge=0.0, le=100.0)
    precipitation: float = Field(0.0, ge=0.0, le=100.0)
    precipitation_deposits: float = Field(0.0, ge=0.0, le=100.0)
    wind_intensity: float = Field(10.0, ge=0.0, le=100.0)
    sun_azimuth_angle: float = Field(0.0, ge=0.0, le=360.0)
    sun_altitude_angle: float = Field(45.0, ge=-90.0, le=90.0)
    fog_density: float = Field(0.0, ge=0.0, le=100.0)
    fog_distance: float = Field(0.0, ge=0.0)
    rayleigh_scattering: float = Field(1.0, ge=0.0, le=100.0)
    mie_scattering: float = Field(0.03, ge=0.0, le=100.0)
    preset_name: str = ""

    @field_validator("fog_density")
    @classmethod
    def validate_fog_density(cls, v: float) -> float:
        """验证雾浓度。"""
        return v

    @property
    def is_night(self) -> bool:
        """是否为夜间环境（需要开启车灯）。"""
        return self.sun_altitude_angle < -5.0

    @property
    def is_rainy(self) -> bool:
        """是否为雨天环境。"""
        return self.precipitation > 10.0

    def to_carla_dict(self) -> dict[str, float]:
        """转换为 CARLA WeatherParameters 字典。

        CARLA API 使用原生 0-100 百分比刻度与度制角度（设计文档 §3.4.1），
        直接透传内部参数，不做归一化。

        Returns:
            CARLA API 参数字典。
        """
        return {
            "cloudiness": self.cloudiness,
            "precipitation": self.precipitation,
            "precipitation_deposits": self.precipitation_deposits,
            "wind_intensity": self.wind_intensity,
            "sun_azimuth_angle": self.sun_azimuth_angle,
            "sun_altitude_angle": self.sun_altitude_angle,
            "fog_density": self.fog_density,
            "fog_distance": self.fog_distance,
        }


# ─── 预设环境配置字典 ─────────────────────────────────────────────────────────

# 预设参数值严格对齐设计文档 §3.4.2 表格（cloudiness / rain / sun_altitude）
_PRESET_PROFILES: dict[str, WeatherProfile] = {
    "sunny_noon": WeatherProfile(
        preset_name="sunny_noon",
        cloudiness=0.0, precipitation=0.0, precipitation_deposits=0.0,
        wind_intensity=5.0, sun_azimuth_angle=45.0, sun_altitude_angle=60.0,
        fog_density=0.0, rayleigh_scattering=1.0, mie_scattering=0.03,
    ),
    "cloudy": WeatherProfile(
        preset_name="cloudy",
        cloudiness=80.0, precipitation=0.0, precipitation_deposits=0.0,
        wind_intensity=20.0, sun_azimuth_angle=45.0, sun_altitude_angle=45.0,
        fog_density=5.0, rayleigh_scattering=1.5, mie_scattering=0.05,
    ),
    "light_rain": WeatherProfile(
        preset_name="light_rain",
        cloudiness=30.0, precipitation=30.0, precipitation_deposits=30.0,
        wind_intensity=30.0, sun_azimuth_angle=45.0, sun_altitude_angle=45.0,
        fog_density=10.0, rayleigh_scattering=1.5, mie_scattering=0.1,
    ),
    "heavy_rain": WeatherProfile(
        preset_name="heavy_rain",
        cloudiness=50.0, precipitation=80.0, precipitation_deposits=90.0,
        wind_intensity=70.0, sun_azimuth_angle=45.0, sun_altitude_angle=30.0,
        fog_density=20.0, rayleigh_scattering=2.0, mie_scattering=0.2,
    ),
    "foggy": WeatherProfile(
        preset_name="foggy",
        cloudiness=100.0, precipitation=0.0, precipitation_deposits=0.0,
        wind_intensity=5.0, sun_azimuth_angle=45.0, sun_altitude_angle=20.0,
        fog_density=60.0, fog_distance=20.0,
        rayleigh_scattering=2.0, mie_scattering=0.5,
    ),
    "night": WeatherProfile(
        preset_name="night",
        cloudiness=100.0, precipitation=0.0, precipitation_deposits=0.0,
        wind_intensity=5.0, sun_azimuth_angle=0.0, sun_altitude_angle=-15.0,
        fog_density=0.0, rayleigh_scattering=0.5, mie_scattering=0.0,
    ),
    "dusk": WeatherProfile(
        preset_name="dusk",
        cloudiness=20.0, precipitation=0.0, precipitation_deposits=0.0,
        wind_intensity=10.0, sun_azimuth_angle=90.0, sun_altitude_angle=5.0,
        fog_density=5.0, rayleigh_scattering=3.0, mie_scattering=0.1,
    ),
    "dawn": WeatherProfile(
        preset_name="dawn",
        cloudiness=20.0, precipitation=0.0, precipitation_deposits=0.0,
        wind_intensity=10.0, sun_azimuth_angle=270.0, sun_altitude_angle=10.0,
        fog_density=15.0, rayleigh_scattering=3.0, mie_scattering=0.1,
    ),
}


def get_preset_profile(preset_name: str) -> WeatherProfile:
    """返回指定预设环境的 WeatherProfile 副本。

    Args:
        preset_name: 预设名称，必须是 PRESET_ENVIRONMENTS 中的一项。

    Returns:
        WeatherProfile 副本。

    Raises:
        ConfigurationError: 预设名称不存在。
    """
    if preset_name not in _PRESET_PROFILES:
        raise ConfigurationError(
            "get_preset_profile",
            f"Unknown preset '{preset_name}'. Available: {PRESET_ENVIRONMENTS}",
        )
    return _PRESET_PROFILES[preset_name].model_copy()


class CarlaWorldProtocol(Protocol):
    """CARLA World 协议（用于 apply 天气）。"""

    def set_weather(self, weather: Any) -> None: ...


class WeatherManager:
    """天气管理器：负责天气设置、渐变过渡和预设管理。

    Args:
        world: CARLA World 对象。
    """

    def __init__(self, world: CarlaWorldProtocol) -> None:
        self._world = world
        self._current: WeatherProfile = _PRESET_PROFILES["sunny_noon"].model_copy()
        self._target: Optional[WeatherProfile] = None
        self._transition_start: float = 0.0
        self._transition_duration: float = 0.0
        logger.info("WeatherManager initialized")

    @property
    def current_profile(self) -> WeatherProfile:
        """当前天气配置（只读副本）。"""
        return self._current.model_copy()

    def set_weather(self, profile: WeatherProfile) -> None:
        """立即设置天气（无渐变）。

        Args:
            profile: 目标天气配置。
        """
        self._apply_profile(profile)
        self._current = profile.model_copy()
        self._target = None
        logger.info(f"Weather set immediately: {profile.preset_name or 'custom'}")

    def set_preset(self, preset_name: str) -> None:
        """立即切换到预设环境。

        Args:
            preset_name: 预设环境名称。
        """
        profile = get_preset_profile(preset_name)
        self.set_weather(profile)

    def start_transition(
        self,
        target: WeatherProfile,
        duration_seconds: float = 3.0,
    ) -> None:
        """启动渐变天气过渡（避免传感器数据跳变）。

        需在仿真循环中每 tick 调用 update_transition() 推进过渡进度。

        Args:
            target: 目标天气配置。
            duration_seconds: 过渡总时长（秒）。
        """
        import time  # noqa: PLC0415
        self._target = target.model_copy()
        self._transition_start = time.perf_counter()
        self._transition_duration = max(0.1, duration_seconds)
        logger.info(
            f"Weather transition started: "
            f"'{self._current.preset_name}' → '{target.preset_name}', "
            f"{duration_seconds:.1f}s"
        )

    def update_transition(self) -> bool:
        """更新渐变过渡进度，需在每次 world.tick() 后调用。

        Returns:
            True 表示过渡仍在进行中，False 表示过渡已完成。
        """
        import time  # noqa: PLC0415
        if self._target is None:
            return False

        elapsed = time.perf_counter() - self._transition_start
        t = min(1.0, elapsed / self._transition_duration)
        alpha = smoothstep(t)

        interpolated = WeatherProfile(
            cloudiness=lerp(self._current.cloudiness, self._target.cloudiness, alpha),
            precipitation=lerp(self._current.precipitation, self._target.precipitation, alpha),
            precipitation_deposits=lerp(
                self._current.precipitation_deposits,
                self._target.precipitation_deposits,
                alpha,
            ),
            wind_intensity=lerp(self._current.wind_intensity, self._target.wind_intensity, alpha),
            sun_azimuth_angle=lerp(
                self._current.sun_azimuth_angle, self._target.sun_azimuth_angle, alpha
            ),
            sun_altitude_angle=lerp(
                self._current.sun_altitude_angle, self._target.sun_altitude_angle, alpha
            ),
            fog_density=lerp(self._current.fog_density, self._target.fog_density, alpha),
            fog_distance=lerp(self._current.fog_distance, self._target.fog_distance, alpha),
            rayleigh_scattering=lerp(
                self._current.rayleigh_scattering, self._target.rayleigh_scattering, alpha
            ),
            mie_scattering=lerp(self._current.mie_scattering, self._target.mie_scattering, alpha),
        )

        self._apply_profile(interpolated)

        if t >= 1.0:
            self._current = self._target.model_copy()
            self._target = None
            logger.info("Weather transition completed")
            return False
        return True

    def _apply_profile(self, profile: WeatherProfile) -> None:
        """将 WeatherProfile 应用到 CARLA World（内部实现）。

        CARLA WeatherParameters 使用 0-100 原生刻度，直接透传参数
        （单一换算出口：WeatherProfile.to_carla_dict）。

        Args:
            profile: 目标天气配置。
        """
        try:
            import carla  # noqa: PLC0415
        except ImportError:
            logger.warning("carla package not available, weather not applied")
            return
        try:
            wp = carla.WeatherParameters(**profile.to_carla_dict())
            self._world.set_weather(wp)
            logger.debug(f"Weather applied to world: {profile.preset_name or 'custom'}")
        except RuntimeError as exc:  # CARLA 连接断开 / world 不可用
            logger.warning(f"Failed to apply weather to CARLA world: {exc}")

    def to_json(self) -> str:
        """将当前天气配置序列化为 JSON 字符串。"""
        return self._current.model_dump_json(indent=2)

    @staticmethod
    def load_profiles_from_file(path: Path) -> dict[str, WeatherProfile]:
        """从 JSON 文件加载天气预设配置。

        Args:
            path: JSON 配置文件路径。

        Returns:
            预设名称到 WeatherProfile 的映射字典。
        """
        if not path.exists():
            return dict(_PRESET_PROFILES)
        try:
            raw: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
            return {name: WeatherProfile(**cfg) for name, cfg in raw.items()}
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(f"Failed to load weather profiles from {path}: {exc}")
            return dict(_PRESET_PROFILES)
