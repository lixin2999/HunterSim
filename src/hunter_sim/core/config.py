"""全局配置模型（pydantic v2）与配置文件加载。

对应开发提示词 §9 的场景配置结构。所有模型默认 ``extra="forbid"``，以便在加载
YAML/JSON 时对拼写错误或不认识的字段尽早报错（配置优先级：显式传参 > 配置文件
> 默认值）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hunter_sim.core.exceptions import ConfigurationError


class _ConfigModel(BaseModel):
    """所有配置模型的基类：禁止未知字段。"""

    model_config = ConfigDict(extra="forbid")


class ConnectionConfig(_ConfigModel):
    """CARLA 连接参数（§2.3：以独立进程运行，通过 carla.Client 连接）。"""

    host: str = "localhost"
    port: int = 2000
    timeout_seconds: float = Field(default=10.0, gt=0)
    world_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=5, ge=0, le=20)
    traffic_manager_port: int = 8000


class SimulationModeConfig(_ConfigModel):
    """仿真步进模式配置（映射到 ``carla.WorldSettings``）。

    - ``synchronous_mode=True``（VIL 实时模式）：仿真由客户端 ``world.tick()`` 驱动，
      步长固定为 ``fixed_delta_seconds``，与实车控制频率对齐；
    - ``synchronous_mode=False``（回放 / SIL 模式）：仿真按真实时间自动推进，
      ``fixed_delta_seconds`` 置 ``None`` 表示可变步长。

    物理子步（``substepping``）用于在高步长下保证碰撞与动力学稳定性。
    """

    mode: Literal["synchronous", "asynchronous"] = Field(
        default="synchronous", description="仿真模式：synchronous=VIL实时；asynchronous=回放/SIL"
    )
    fixed_delta_seconds: float | None = Field(
        default=None,
        gt=0,
        description="固定步长（秒）；为 None 时由 tick_rate 推算（1/tick_rate）",
    )
    substepping: bool = Field(default=True, description="启用物理子步（仅同步模式生效）")
    max_substep_delta_time: float = Field(default=0.01, gt=0, description="单个子步最大时间（秒）")
    max_substeps: int = Field(default=4, ge=1, description="每帧最大子步数")

    @property
    def is_synchronous(self) -> bool:
        """是否为同步（VIL 实时）模式。"""
        return self.mode == "synchronous"


class ScenarioSection(_ConfigModel):
    """场景基本参数（地图、时长、步进频率、仿真模式）。"""

    name: str
    map: str
    duration_seconds: float = Field(gt=0)
    tick_rate: float = Field(gt=0, description="仿真步进频率 Hz")
    simulation_mode: SimulationModeConfig = Field(default_factory=SimulationModeConfig)


class PhysicsConfig(_ConfigModel):
    """车辆物理参数。"""

    mass: float = Field(default=1840.0, gt=0, description="整车质量 kg")
    drag_coefficient: float = Field(default=0.23, ge=0)


class VehicleSection(_ConfigModel):
    """主车蓝图、生成点与物理配置。"""

    blueprint: str
    spawn_point_index: int = Field(ge=0, default=0)
    color: str = "255,0,0"
    physics: PhysicsConfig = Field(default_factory=PhysicsConfig)

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: str) -> str:
        parts = v.split(",")
        if len(parts) != 3 or not all(p.strip().isdigit() and 0 <= int(p) <= 255 for p in parts):
            raise ValueError("color 必须为 'R,G,B' 且各分量在 0-255")
        return v


class SensorConfig(_ConfigModel):
    """单个传感器配置；不同类型使用不同的可选字段子集。"""

    id: str
    type: str
    position: tuple[float, float, float]
    rotation: tuple[float, float, float]
    tick_rate: float | None = Field(default=None, gt=0)
    # 相机
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fov: float | None = Field(default=None, gt=0)
    # LiDAR
    channels: int | None = Field(default=None, gt=0)
    range: float | None = Field(default=None, gt=0)
    points_per_second: int | None = Field(default=None, gt=0)
    rotation_frequency: float | None = Field(default=None, gt=0)

    @field_validator("id", "type")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("字段不能为空")
        return v


class WeatherConfig(_ConfigModel):
    """天气与环境光照参数。"""

    cloudiness: float = Field(ge=0, le=1, default=0.0)
    precipitation: float = Field(ge=0, le=1, default=0.0)
    sun_altitude_angle: float = Field(ge=-90, le=90, default=45.0)


class TrafficConfig(_ConfigModel):
    """交通流（NPC 车辆 / 行人）与 Traffic Manager 参数。"""

    npc_vehicles: int = Field(ge=0, default=0)
    npc_walkers: int = Field(ge=0, default=0)
    traffic_manager_port: int = 8000


class OutputFormats(_ConfigModel):
    """各类数据的输出格式选择。"""

    camera: str = "png"
    lidar: str = "hdf5"
    telemetry: str = "msgpack"


class OutputConfig(_ConfigModel):
    """数据落盘根目录与格式配置。"""

    base_dir: str = "./data/runs"
    formats: OutputFormats = Field(default_factory=OutputFormats)


class ScenarioConfig(_ConfigModel):
    """一次采集任务的完整场景配置根模型。"""

    scenario: ScenarioSection
    vehicle: VehicleSection
    sensors: list[SensorConfig] = Field(min_length=1)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    traffic: TrafficConfig = Field(default_factory=TrafficConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @field_validator("sensors")
    @classmethod
    def _unique_sensor_ids(cls, v: list[SensorConfig]) -> list[SensorConfig]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            dup = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"传感器 id 重复: {sorted(dup)}")
        return v


def load_scenario_config(path: str | Path) -> ScenarioConfig:
    """从 YAML 或 JSON 文件加载并校验场景配置。

    Args:
        path: 配置文件路径，扩展名 ``.yaml`` / ``.yml`` / ``.json``。

    Returns:
        校验通过的 :class:`ScenarioConfig`。

    Raises:
        ConfigurationError: 文件不存在、格式不支持、解析失败或校验不通过。
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigurationError(f"配置文件不存在: {config_path}")

    suffix = config_path.suffix.lower()
    if suffix not in (".yaml", ".yml", ".json"):
        raise ConfigurationError(f"不支持的配置文件格式: {suffix}")

    text = config_path.read_text(encoding="utf-8")
    try:
        raw: Any = yaml.safe_load(text) if suffix in (".yaml", ".yml") else json.loads(text)
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"配置文件解析失败: {config_path} ({exc})") from exc

    try:
        return ScenarioConfig.model_validate(raw)
    except ValueError as exc:  # pydantic ValidationError 属于 ValueError
        raise ConfigurationError(f"配置校验失败: {config_path} ({exc})") from exc


__all__ = [
    "ConnectionConfig",
    "OutputConfig",
    "OutputFormats",
    "PhysicsConfig",
    "ScenarioConfig",
    "ScenarioSection",
    "SensorConfig",
    "SimulationModeConfig",
    "TrafficConfig",
    "VehicleSection",
    "WeatherConfig",
    "load_scenario_config",
]
