"""仿真评估指标计算引擎（PROMPT-ENG-007-A）。

实现安全、效率、舒适性、任务四类评估指标的计算，
以及评估等级判定（S/A/B/C/D）和报告生成。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from hunter_sim.common.models import EvalGrade
from hunter_sim.common.utils import get_logger, rmse

logger = get_logger(__name__)

# 评估等级标准
_EVAL_THRESHOLDS: list[tuple[EvalGrade, float, float]] = [
    (EvalGrade.S, 0.95, 0.00),   # 通过率 >= 95%, 碰撞率 0%
    (EvalGrade.A, 0.85, 0.02),
    (EvalGrade.B, 0.70, 0.05),
    (EvalGrade.C, 0.50, 0.10),
]


def determine_grade(pass_rate: float, collision_rate: float) -> EvalGrade:
    """根据通过率和碰撞率确定评估等级。

    碰撞率按“不超过上限”判定（S 级要求碰撞率恰好为 0）。
    """
    for grade, min_pass, max_coll in _EVAL_THRESHOLDS:
        if pass_rate >= min_pass and collision_rate <= max_coll:
            return grade
    return EvalGrade.D


@dataclass
class SafetyMetrics:
    """安全指标计算结果。

    Attributes:
        collision_count: 碰撞总次数。
        min_ttc_s: 最小碰撞时间（TTC，秒）。
        min_distance_m: 最小车距（米）。
        emergency_brake_count: 紧急制动次数。
        lane_invasion_count: 车道偏离次数。
        red_light_count: 闯红灯次数。
    """

    collision_count: int = 0
    min_ttc_s: float = float("inf")
    min_distance_m: float = float("inf")
    emergency_brake_count: int = 0
    lane_invasion_count: int = 0
    red_light_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "collision_count": self.collision_count,
            "min_ttc_s": self.min_ttc_s if self.min_ttc_s != float("inf") else None,
            "min_distance_m": self.min_distance_m if self.min_distance_m != float("inf") else None,
            "emergency_brake_count": self.emergency_brake_count,
            "lane_invasion_count": self.lane_invasion_count,
            "red_light_count": self.red_light_count,
        }


@dataclass
class EfficiencyMetrics:
    """效率指标计算结果。

    Attributes:
        avg_speed_ms: 平均速度（m/s）。
        total_distance_m: 总行驶距离（米）。
        scene_duration_s: 场景实际运行时长（秒）。
        waiting_time_s: 等待（停止）时间（秒）。
        completion_rate: 场景完成率 (0~1)。
    """

    avg_speed_ms: float = 0.0
    total_distance_m: float = 0.0
    scene_duration_s: float = 0.0
    waiting_time_s: float = 0.0
    completion_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "avg_speed_ms": round(self.avg_speed_ms, 3),
            "total_distance_m": round(self.total_distance_m, 2),
            "scene_duration_s": round(self.scene_duration_s, 2),
            "waiting_time_s": round(self.waiting_time_s, 2),
            "completion_rate": round(self.completion_rate, 3),
        }


@dataclass
class ComfortMetrics:
    """舒适性指标计算结果。

    Attributes:
        avg_acceleration_ms2: 平均加速度绝对值（m/s²）。
        max_acceleration_ms2: 最大加速度（m/s²）。
        avg_jerk_ms3: 平均加加速度（m/s³）。
        max_jerk_ms3: 最大加加速度（m/s³）。
        steering_smoothness_rad_s: 转向平滑度（平均角速度，rad/s）。
    """

    avg_acceleration_ms2: float = 0.0
    max_acceleration_ms2: float = 0.0
    avg_jerk_ms3: float = 0.0
    max_jerk_ms3: float = 0.0
    steering_smoothness_rad_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "avg_acceleration_ms2": round(self.avg_acceleration_ms2, 3),
            "max_acceleration_ms2": round(self.max_acceleration_ms2, 3),
            "avg_jerk_ms3": round(self.avg_jerk_ms3, 3),
            "max_jerk_ms3": round(self.max_jerk_ms3, 3),
            "steering_smoothness_rad_s": round(self.steering_smoothness_rad_s, 4),
        }


@dataclass
class SceneEvaluationResult:
    """单场景完整评估结果。"""

    scene_id: str
    grade: EvalGrade
    safety: SafetyMetrics
    efficiency: EfficiencyMetrics
    comfort: ComfortMetrics
    passed: bool
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "grade": self.grade.value,
            "passed": self.passed,
            "timestamp": self.timestamp,
            "safety": self.safety.to_dict(),
            "efficiency": self.efficiency.to_dict(),
            "comfort": self.comfort.to_dict(),
            "events": self.events,
        }


class EvaluationEngine:
    """评估指标计算主引擎。

    输入仿真运行数据（轨迹、事件列表），输出 SceneEvaluationResult。
    """

    def evaluate_scene(
        self,
        scene_id: str,
        vehicle_states: list[dict[str, Any]],
        events: list[dict[str, Any]],
        collision_events: list[dict[str, Any]],
        ttc_samples: list[float],
        min_distances: list[float],
        speed_limit_ms: float = 10.0,
        scene_duration_s: float = 60.0,
        completed: bool = True,
    ) -> SceneEvaluationResult:
        """计算单场景的完整评估结果。

        Args:
            scene_id: 场景 ID。
            vehicle_states: 车辆状态帧列表（含 velocity/acceleration 字段）。
            events: 检测到的事件列表。
            collision_events: 碰撞事件列表。
            ttc_samples: TTC 采样值列表。
            min_distances: 最小距离采样列表。
            speed_limit_ms: 限速值。
            scene_duration_s: 场景设定运行时长。
            completed: 场景是否正常完成。

        Returns:
            SceneEvaluationResult。
        """
        # 安全指标
        safety = self._compute_safety(collision_events, events, ttc_samples, min_distances)

        # 效率指标
        efficiency = self._compute_efficiency(vehicle_states, scene_duration_s, completed)

        # 舒适性指标
        comfort = self._compute_comfort(vehicle_states)

        # 任务指标
        passed = completed and safety.collision_count == 0

        # 等级判定（单场景用 pass/fail，批量用 pass_rate）
        grade = EvalGrade.S if passed else EvalGrade.D

        return SceneEvaluationResult(
            scene_id=scene_id,
            grade=grade,
            safety=safety,
            efficiency=efficiency,
            comfort=comfort,
            passed=passed,
            events=events,
        )

    @staticmethod
    def _compute_safety(
        collisions: list[dict[str, Any]],
        events: list[dict[str, Any]],
        ttc_samples: list[float],
        min_distances: list[float],
    ) -> SafetyMetrics:
        lane_inv = sum(1 for e in events if e.get("event_type") == "lane_invasion")
        red_light = sum(1 for e in events if e.get("event_type") == "red_light")
        emergency_brake = sum(1 for e in events if e.get("event_type") == "emergency_brake")
        return SafetyMetrics(
            collision_count=len(collisions),
            min_ttc_s=min(ttc_samples) if ttc_samples else float("inf"),
            min_distance_m=min(min_distances) if min_distances else float("inf"),
            emergency_brake_count=emergency_brake,
            lane_invasion_count=lane_inv,
            red_light_count=red_light,
        )

    @staticmethod
    def _compute_efficiency(
        states: list[dict[str, Any]],
        duration_s: float,
        completed: bool,
    ) -> EfficiencyMetrics:
        speeds = [s.get("vehicle_speed", 0.0) for s in states]
        avg_speed = sum(speeds) / len(speeds) if speeds else 0.0
        total_dist = 0.0
        for i in range(1, len(states)):
            dt = states[i].get("timestamp", 0.0) - states[i - 1].get("timestamp", 0.0)
            if 0.0 < dt < 0.1:
                total_dist += states[i].get("vehicle_speed", 0.0) * dt
        waiting = sum(1 for s in speeds if s < 0.2) * 0.02  # 50Hz 帧
        return EfficiencyMetrics(
            avg_speed_ms=avg_speed,
            total_distance_m=total_dist,
            scene_duration_s=duration_s,
            waiting_time_s=waiting,
            completion_rate=1.0 if completed else 0.0,
        )

    @staticmethod
    def _compute_comfort(states: list[dict[str, Any]]) -> ComfortMetrics:
        accels = []
        yaws = []
        prev_speeds: list[float] = []
        for s in states:
            ax, ay, _ = s.get("acceleration", (0.0, 0.0, 0.0))
            accels.append(math.sqrt(ax ** 2 + ay ** 2))
            yaws.append(s.get("steering", 0.0))
            prev_speeds.append(s.get("vehicle_speed", 0.0))

        avg_acc = sum(accels) / len(accels) if accels else 0.0
        max_acc = max(accels) if accels else 0.0

        # Jerk 估算（加速度差分）
        jerks: list[float] = []
        for i in range(1, len(accels)):
            dt = 0.02
            jerks.append(abs(accels[i] - accels[i - 1]) / dt)
        avg_jerk = sum(jerks) / len(jerks) if jerks else 0.0
        max_jerk = max(jerks) if jerks else 0.0

        # 转向平滑度
        steer_rates: list[float] = []
        for i in range(1, len(yaws)):
            steer_rates.append(abs(yaws[i] - yaws[i - 1]) / 0.02)
        steer_smooth = sum(steer_rates) / len(steer_rates) if steer_rates else 0.0

        return ComfortMetrics(
            avg_acceleration_ms2=avg_acc,
            max_acceleration_ms2=max_acc,
            avg_jerk_ms3=avg_jerk,
            max_jerk_ms3=max_jerk,
            steering_smoothness_rad_s=steer_smooth,
        )


class BatchEvaluator:
    """批量场景评估器。

    管理多个场景评估结果，计算总体通过率和等级。
    """

    def __init__(self) -> None:
        self._results: list[SceneEvaluationResult] = []

    def add_result(self, result: SceneEvaluationResult) -> None:
        """添加单场景评估结果。"""
        self._results.append(result)

    def overall_grade(self) -> EvalGrade:
        """计算批量评估总体等级。"""
        if not self._results:
            return EvalGrade.D
        pass_rate = sum(1 for r in self._results if r.passed) / len(self._results)
        collision_rate = sum(1 for r in self._results if r.safety.collision_count > 0) / len(self._results)
        return determine_grade(pass_rate, collision_rate)

    def summary(self) -> dict[str, Any]:
        """返回批量评估摘要。"""
        total = len(self._results)
        if total == 0:
            return {"total": 0}
        passed = sum(1 for r in self._results if r.passed)
        return {
            "total_scenes": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total,
            "overall_grade": self.overall_grade().value,
            "avg_collision_count": sum(r.safety.collision_count for r in self._results) / total,
            "results": [r.to_dict() for r in self._results],
        }

    def to_json(self, indent: int = 2) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(self.summary(), indent=indent, default=str)
