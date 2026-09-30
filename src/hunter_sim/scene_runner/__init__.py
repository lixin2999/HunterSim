"""HunterSim 场景管理与运行服务层（ENG-003）。

负责场景配置解析、校验、转换、运行生命周期管理以及事件检测。
"""

from hunter_sim.scene_runner.custom_scenario import CustomScenarioBase
from hunter_sim.scene_runner.event_detector import DetectedEvent, EventDetector
from hunter_sim.scene_runner.scenario_adapter import ScenarioRunnerAdapter
from hunter_sim.scene_runner.scene_config import (
    EgoVehicleConfig,
    SceneConfig,
    SceneEventDefinition,
    SceneEventType,
    ScenarioBehaviorType,
    SpawnPoint,
    TrafficParticipantConfig,
    TriggerType,
    WeatherConfig,
)
from hunter_sim.scene_runner.scene_converter import (
    CarlaSpawnParams,
    SceneConfigConverter,
    SceneRuntimeParams,
)
from hunter_sim.scene_runner.scene_runner_service import SceneRunnerService
from hunter_sim.scene_runner.scene_state_manager import (
    SceneStateManager,
    SceneStateSnapshot,
)
from hunter_sim.scene_runner.scene_validator import (
    SceneConfigValidator,
    ValidationIssue,
    ValidationResult,
)

__all__ = [
    "CustomScenarioBase",
    "DetectedEvent", "EventDetector",
    "ScenarioRunnerAdapter",
    "EgoVehicleConfig", "SceneConfig", "SceneEventDefinition", "SceneEventType",
    "ScenarioBehaviorType", "SpawnPoint", "TrafficParticipantConfig", "TriggerType", "WeatherConfig",
    "CarlaSpawnParams", "SceneConfigConverter", "SceneRuntimeParams",
    "SceneRunnerService",
    "SceneStateManager", "SceneStateSnapshot",
    "SceneConfigValidator", "ValidationIssue", "ValidationResult",
]
