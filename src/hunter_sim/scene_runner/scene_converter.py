"""场景配置转换器（PROMPT-ENG-003-A）。

将平台 SceneConfig 转换为 CARLA API 调用参数集合，
生成 ScenarioRunner 可用的 OpenSCENARIO XML，或自定义脚本可用的参数字典。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from hunter_sim.common.models import SimMode
from hunter_sim.common.utils import get_logger
from hunter_sim.scene_runner.scene_config import SceneConfig, TrafficParticipantConfig

logger = get_logger(__name__)


@dataclass
class CarlaSpawnParams:
    """CARLA Actor 生成参数字典。

    Attributes:
        actor_type: Actor 类型字符串（vehicle/walker 等）。
        blueprint_id: CARLA 蓝图名称。
        x: CARLA 地图 X 坐标。
        y: CARLA 地图 Y 坐标。
        z: CARLA 地图 Z 坐标。
        yaw_deg: 航向角（度）。
        autopilot: 是否启用自动驾驶。
        extra_attrs: 额外蓝图属性字典。
    """

    actor_type: str
    blueprint_id: str
    x: float
    y: float
    z: float
    yaw_deg: float
    autopilot: bool = False
    extra_attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class SceneRuntimeParams:
    """场景运行时参数集合（转换结果，供 SceneRunnerService 使用）。

    Attributes:
        map_id: 目标地图 ID。
        quality: 画质等级字符串。
        mode: 仿真模式。
        ego_spawn: 自车生成参数。
        participant_spawns: 交通参与者生成参数列表。
        weather_dict: CARLA WeatherParameters 字典。
        duration_seconds: 场景时长。
        timeout_seconds: 超时设置。
    """

    map_id: str
    quality: str
    mode: SimMode
    ego_spawn: CarlaSpawnParams
    participant_spawns: list[CarlaSpawnParams]
    weather_dict: dict[str, float]
    duration_seconds: float
    timeout_seconds: float


class SceneConfigConverter:
    """将 SceneConfig 转换为 CARLA 运行时参数。

    无状态转换器，每次 convert() 返回新的 SceneRuntimeParams。
    """

    def convert(self, config: SceneConfig) -> SceneRuntimeParams:
        """执行场景配置转换。

        Args:
            config: 已验证的 SceneConfig。

        Returns:
            SceneRuntimeParams 供 CARLA 层直接使用的参数字典。
        """
        logger.debug(f"Converting scene config: {config.scene_id}")

        # 转换自车生成参数
        ego_sp = config.ego_vehicle.spawn_point
        ego_spawn = CarlaSpawnParams(
            actor_type="vehicle",
            blueprint_id=config.ego_vehicle.vehicle_blueprint,
            x=ego_sp.x,
            y=ego_sp.y,
            z=ego_sp.z,
            yaw_deg=ego_sp.yaw_deg,
            autopilot=config.ego_vehicle.autopilot,
        )

        # 转换交通参与者生成参数
        participant_spawns: list[CarlaSpawnParams] = []
        for tp in config.traffic_participants:
            sp = tp.spawn_point
            participant_spawns.append(
                CarlaSpawnParams(
                    actor_type=tp.actor_type,
                    blueprint_id=tp.blueprint,
                    x=sp.x,
                    y=sp.y,
                    z=sp.z,
                    yaw_deg=sp.yaw_deg,
                    autopilot=(tp.behavior.value == "constant_speed"),
                )
            )

        # 转换天气参数
        weather_dict: dict[str, float] = {
            "cloudiness": config.weather.cloudiness / 100.0,
            "precipitation": config.weather.precipitation / 100.0,
            "precipitation_deposits": config.weather.precipitation_deposits / 100.0,
            "wind_intensity": config.weather.wind_intensity / 100.0,
            "sun_azimuth_angle": config.weather.sun_azimuth_angle,
            "sun_altitude_angle": config.weather.sun_altitude_angle,
            "fog_density": config.weather.fog_density / 100.0,
            "fog_distance": config.weather.fog_distance,
        }

        return SceneRuntimeParams(
            map_id=config.map_id,
            quality=config.quality.value,
            mode=config.mode,
            ego_spawn=ego_spawn,
            participant_spawns=participant_spawns,
            weather_dict=weather_dict,
            duration_seconds=config.duration_seconds,
            timeout_seconds=config.timeout_seconds,
        )

    def to_scenario_runner_dict(self, config: SceneConfig) -> dict[str, Any]:
        """生成 ScenarioRunner 兼容的配置字典（Python 原子场景格式）。

        Args:
            config: 场景配置。

        Returns:
            ScenarioRunner 可加载的配置字典。
        """
        actors: list[dict[str, Any]] = []
        ego_sp = config.ego_vehicle.spawn_point
        actors.append({
            "type": "ego_vehicle",
            "name": "hero",
            "weather": "HardRain" if config.weather.precipitation > 60 else "ClearNoon",
            "model": config.ego_vehicle.vehicle_blueprint,
            "x": ego_sp.x,
            "y": ego_sp.y,
            "z": ego_sp.z,
            "yaw": ego_sp.yaw_deg,
            "speed": config.ego_vehicle.initial_speed_ms,
            "route": [],
        })

        for tp in config.traffic_participants:
            sp = tp.spawn_point
            actors.append({
                "type": "pedestrian" if tp.actor_type == "walker" else "other_vehicle",
                "name": tp.participant_id,
                "model": tp.blueprint,
                "x": sp.x,
                "y": sp.y,
                "z": sp.z,
                "yaw": sp.yaw_deg,
                "speed": tp.speed_ms,
            })

        return {
            "name": config.scene_id,
            "description": config.scene_name,
            "trigger_volume": None,
            "actors": actors,
        }
