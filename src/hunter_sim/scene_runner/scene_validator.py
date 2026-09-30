"""场景配置校验器（PROMPT-ENG-003-A）。

对 SceneConfig 进行业务级校验（超出 pydantic 字段验证的范围），
包括地图存在性、生成点合法性、参数合理性等。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hunter_sim.common.exceptions import ValidationError
from hunter_sim.common.models import SimMode
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.map_manager import BUILTIN_MAPS, MapManager
from hunter_sim.scene_runner.scene_config import SceneConfig

logger = get_logger(__name__)


@dataclass
class ValidationIssue:
    """单条校验问题。

    Attributes:
        field_path: 问题字段路径（如 "ego_vehicle.spawn_point.x"）。
        level: 严重级别 "error" | "warning"。
        message: 问题描述。
    """

    field_path: str
    level: str
    message: str


@dataclass
class ValidationResult:
    """场景配置校验结果。

    Attributes:
        valid: 是否存在 error 级别问题。
        errors: 错误列表（必须修复才能运行场景）。
        warnings: 警告列表（不阻止运行，但需注意）。
    """

    valid: bool
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]

    @property
    def error_count(self) -> int:
        """错误数量。"""
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        """警告数量。"""
        return len(self.warnings)

    def summary(self) -> str:
        """返回人类可读的校验摘要字符串。"""
        if self.valid and not self.warnings:
            return "Scene config valid, no issues found."
        parts: list[str] = [f"errors={self.error_count}, warnings={self.warning_count}"]
        for e in self.errors[:5]:
            parts.append(f"  ERROR [{e.field_path}]: {e.message}")
        for w in self.warnings[:3]:
            parts.append(f"  WARN  [{w.field_path}]: {w.message}")
        return "\n".join(parts)


class SceneConfigValidator:
    """场景配置业务级校验器。

    Args:
        map_manager: 地图管理器实例（用于检查地图是否可用）。可选。
        available_maps: 可用地图 ID 集合（无 map_manager 时使用）。
    """

    def __init__(
        self,
        map_manager: Optional[MapManager] = None,
        available_maps: Optional[set[str]] = None,
    ) -> None:
        self._map_mgr = map_manager
        self._maps: set[str] = available_maps or set(BUILTIN_MAPS)

    def validate(self, config: SceneConfig) -> ValidationResult:
        """执行完整场景配置校验。

        Args:
            config: 待校验的场景配置。

        Returns:
            ValidationResult，valid=False 时不应尝试运行场景。
        """
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []

        # 1. 地图可用性
        self._check_map(config, errors, warnings)

        # 2. 自车生成点
        self._check_ego_spawn(config, errors, warnings)

        # 3. 天气参数
        self._check_weather(config, errors, warnings)

        # 4. 交通参与者
        self._check_participants(config, errors, warnings)

        # 5. 场景时长
        self._check_duration(config, errors, warnings)

        valid = len(errors) == 0
        result = ValidationResult(valid=valid, errors=errors, warnings=warnings)
        logger.info(
            f"Scene '{config.scene_id}' validation: valid={valid}, "
            f"errors={len(errors)}, warnings={len(warnings)}"
        )
        return result

    def _check_map(
        self,
        config: SceneConfig,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
    ) -> None:
        if self._map_mgr is not None:
            if not self._map_mgr.is_map_available(config.map_id):
                errors.append(ValidationIssue(
                    field_path="map_id",
                    level="error",
                    message=f"Map '{config.map_id}' not available in MapManager",
                ))
        elif config.map_id not in self._maps:
            errors.append(ValidationIssue(
                field_path="map_id",
                level="error",
                message=f"Map '{config.map_id}' not in available maps list",
            ))

    def _check_ego_spawn(
        self,
        config: SceneConfig,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
    ) -> None:
        sp = config.ego_vehicle.spawn_point
        # 检查坐标范围（防止异常大的值）
        if abs(sp.x) > 5000.0:
            errors.append(ValidationIssue(
                field_path="ego_vehicle.spawn_point.x",
                level="error",
                message=f"X coordinate {sp.x} out of reasonable range (|x| > 5000m)",
            ))
        if abs(sp.y) > 5000.0:
            errors.append(ValidationIssue(
                field_path="ego_vehicle.spawn_point.y",
                level="error",
                message=f"Y coordinate {sp.y} out of reasonable range (|y| > 5000m)",
            ))
        # HUNTER SE 最大速度检查
        if config.ego_vehicle.initial_speed_ms > 4.8:
            warnings.append(ValidationIssue(
                field_path="ego_vehicle.initial_speed_ms",
                level="warning",
                message=(
                    f"Initial speed {config.ego_vehicle.initial_speed_ms} m/s "
                    "exceeds HUNTER SE max speed (4.8 m/s)"
                ),
            ))

    def _check_weather(
        self,
        config: SceneConfig,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
    ) -> None:
        from hunter_sim.engine.weather_manager import PRESET_ENVIRONMENTS
        if config.weather.preset_name and config.weather.preset_name not in PRESET_ENVIRONMENTS:
            warnings.append(ValidationIssue(
                field_path="weather.preset_name",
                level="warning",
                message=f"Unknown weather preset '{config.weather.preset_name}'",
            ))
        # 大雨 + Epic 画质性能警告
        if config.weather.precipitation > 70.0 and config.quality.value == "epic":
            warnings.append(ValidationIssue(
                field_path="quality",
                level="warning",
                message="Heavy rain with Epic quality may reduce FPS below real-time",
            ))

    def _check_participants(
        self,
        config: SceneConfig,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
    ) -> None:
        if len(config.traffic_participants) > 50:
            warnings.append(ValidationIssue(
                field_path="traffic_participants",
                level="warning",
                message=f"{len(config.traffic_participants)} participants may impact performance",
            ))

        seen_ids: set[str] = set()
        for i, tp in enumerate(config.traffic_participants):
            if tp.participant_id in seen_ids:
                errors.append(ValidationIssue(
                    field_path=f"traffic_participants[{i}].participant_id",
                    level="error",
                    message=f"Duplicate participant_id '{tp.participant_id}'",
                ))
            seen_ids.add(tp.participant_id)

    def _check_duration(
        self,
        config: SceneConfig,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
    ) -> None:
        if config.duration_seconds > config.timeout_seconds:
            errors.append(ValidationIssue(
                field_path="duration_seconds",
                level="error",
                message=(
                    f"duration_seconds ({config.duration_seconds}) exceeds "
                    f"timeout_seconds ({config.timeout_seconds})"
                ),
            ))
