"""评估指标引擎单元测试（PROMPT-TEST-001 / PROMPT-ENG-007-A）。"""

from __future__ import annotations

import pytest

from hunter_sim.common.models import EvalGrade
from hunter_sim.eval_service.evaluation_engine import (
    BatchEvaluator,
    ComfortMetrics,
    determine_grade,
    EfficiencyMetrics,
    EvaluationEngine,
    SafetyMetrics,
    SceneEvaluationResult,
)


class TestDetermineGrade:
    """等级判定函数测试。"""

    def test_grade_s(self) -> None:
        assert determine_grade(0.96, 0.0) == EvalGrade.S

    def test_grade_a(self) -> None:
        assert determine_grade(0.86, 0.015) == EvalGrade.A

    def test_grade_b(self) -> None:
        assert determine_grade(0.72, 0.04) == EvalGrade.B

    def test_grade_c(self) -> None:
        assert determine_grade(0.51, 0.09) == EvalGrade.C

    def test_grade_d_low_pass_rate(self) -> None:
        assert determine_grade(0.4, 0.2) == EvalGrade.D

    def test_grade_d_high_collision_rate(self) -> None:
        assert determine_grade(0.99, 0.11) == EvalGrade.D


class TestEvaluationEngine:
    """评估引擎测试。"""

    def test_safe_scene_passes(self) -> None:
        engine = EvaluationEngine()
        states = [
            {"vehicle_speed": 3.0, "timestamp": i * 0.02, "acceleration": (0.5, 0.0, 0.0), "steering": 0.1}
            for i in range(100)
        ]
        result = engine.evaluate_scene(
            scene_id="test_001",
            vehicle_states=states,
            events=[],
            collision_events=[],
            ttc_samples=[10.0, 8.0, 12.0],
            min_distances=[5.0, 4.5],
            scene_duration_s=2.0,
            completed=True,
        )
        assert result.passed is True
        assert result.safety.collision_count == 0
        assert result.safety.min_ttc_s == 8.0

    def test_collision_scene_fails(self) -> None:
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_002",
            vehicle_states=[],
            events=[{"event_type": "collision"}],
            collision_events=[{"timestamp": 1.0}],
            ttc_samples=[0.5],
            min_distances=[0.5],
            scene_duration_s=10.0,
            completed=True,
        )
        assert result.passed is False
        assert result.safety.collision_count == 1

    def test_emergency_brake_counted(self) -> None:
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_003",
            vehicle_states=[],
            events=[{"event_type": "emergency_brake"}] * 3,
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            scene_duration_s=5.0,
            completed=True,
        )
        assert result.safety.emergency_brake_count == 3

    def test_incomplete_scene_fails(self) -> None:
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_004",
            vehicle_states=[],
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            scene_duration_s=10.0,
            completed=False,
        )
        assert result.passed is False

    def test_takeover_scene_fails(self) -> None:
        """人工接管场景判定失败（文档 §9.2.4 接管率统计）。"""
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_005",
            vehicle_states=[],
            events=[],
            collision_events=[],
            ttc_samples=[],
            min_distances=[],
            scene_duration_s=10.0,
            completed=True,
            takeover=True,
        )
        assert result.passed is False
        assert result.takeover is True

    def test_ttc_below_target_fails(self) -> None:
        """最小 TTC < 3.0s 未达安全目标（文档 §9.2.1）。"""
        engine = EvaluationEngine()
        result = engine.evaluate_scene(
            scene_id="test_006",
            vehicle_states=[],
            events=[],
            collision_events=[],
            ttc_samples=[2.5],
            min_distances=[5.0],
            scene_duration_s=10.0,
            completed=True,
        )
        assert result.passed is False
        assert result.safety.meets_targets() is False

    def test_success_criteria_and_max_speed(self) -> None:
        """成功准则与最大速度字段（文档 §9.4）。"""
        engine = EvaluationEngine()
        states = [
            {"vehicle_speed": 3.0 + (i % 10) * 0.1, "timestamp": i * 0.02}
            for i in range(100)
        ]
        result = engine.evaluate_scene(
            scene_id="test_007",
            vehicle_states=states,
            events=[],
            collision_events=[],
            ttc_samples=[10.0],
            min_distances=[5.1],
            speed_limit_ms=10.0,
            scene_duration_s=2.0,
            completed=True,
        )
        assert result.efficiency.max_speed_ms == pytest.approx(3.9)
        assert result.success_criteria["no_collision"] is True
        assert result.success_criteria["min_safe_distance"] == 5.1
        assert result.success_criteria["max_speed_deviation"] == 0.0


class TestMetricsTargets:
    """指标目标值判定测试（文档 §9.2）。"""

    def test_safety_meets_targets_default_inf_ok(self) -> None:
        # 无采样数据（inf）时视为满足 TTC/车距要求
        assert SafetyMetrics().meets_targets() is True

    def test_safety_fails_on_emergency_brakes(self) -> None:
        # 紧急制动 < 2 次/场景
        assert SafetyMetrics(emergency_brake_count=2).meets_targets() is False

    def test_safety_fails_on_lane_invasion(self) -> None:
        assert SafetyMetrics(lane_invasion_count=1).meets_targets() is False

    def test_comfort_meets_targets(self) -> None:
        assert ComfortMetrics(avg_acceleration_ms2=0.8).meets_targets() is True
        # 平均加速度 < 1.0 m/s²
        assert ComfortMetrics(avg_acceleration_ms2=1.2).meets_targets() is False
        # 转向平滑度 < 0.3 rad/s
        assert ComfortMetrics(steering_smoothness_rad_s=0.4).meets_targets() is False


class TestBatchEvaluator:
    """批量评估测试。"""

    def _make_result(
        self,
        scene_id: str,
        passed: bool,
        collisions: int = 0,
        takeover: bool = False,
        completed: bool = True,
    ) -> SceneEvaluationResult:
        safety = SafetyMetrics(collision_count=collisions)
        return SceneEvaluationResult(
            scene_id=scene_id,
            grade=EvalGrade.S if passed else EvalGrade.D,
            safety=safety,
            efficiency=EfficiencyMetrics(completion_rate=1.0 if completed else 0.0),
            comfort=ComfortMetrics(),
            passed=passed,
            takeover=takeover,
        )

    def test_all_pass_grade_s(self) -> None:
        ev = BatchEvaluator()
        for i in range(20):
            ev.add_result(self._make_result(f"scene_{i}", passed=True))
        summary = ev.summary()
        assert summary["pass_rate"] == 1.0
        assert summary["overall_grade"] == "S"

    def test_some_fail_lower_grade(self) -> None:
        ev = BatchEvaluator()
        for i in range(10):
            ev.add_result(self._make_result(f"s{i}", passed=(i < 7)))
        summary = ev.summary()
        assert summary["pass_rate"] == 0.7
        assert summary["overall_grade"] in ("B", "C")

    def test_to_json(self) -> None:
        ev = BatchEvaluator()
        ev.add_result(self._make_result("s1", passed=True))
        js = ev.to_json()
        assert "overall_grade" in js
        assert '"total_scenes": 1' in js

    def test_task_metrics_summary(self) -> None:
        """任务指标汇总（文档 §9.2.4）。"""
        ev = BatchEvaluator()
        ev.add_result(self._make_result("s1", passed=True))
        ev.add_result(self._make_result("s2", passed=False, collisions=1))
        ev.add_result(self._make_result("s3", passed=False, takeover=True))
        ev.add_result(self._make_result("s4", passed=True, completed=False))
        summary = ev.summary()
        assert summary["pass_rate"] == 0.5
        assert summary["collision_rate"] == 0.25
        assert summary["takeover_rate"] == 0.25
        assert summary["target_achievement_rate"] == 0.75
        assert summary["failed_scenes"] == ["s2", "s3"]

    def test_metric_averages(self) -> None:
        """各指标均值汇总（文档 §9.5）。"""
        ev = BatchEvaluator()
        ev.add_result(SceneEvaluationResult(
            scene_id="s1", grade=EvalGrade.S, safety=SafetyMetrics(),
            efficiency=EfficiencyMetrics(avg_speed_ms=4.0),
            comfort=ComfortMetrics(avg_acceleration_ms2=1.0), passed=True,
        ))
        ev.add_result(SceneEvaluationResult(
            scene_id="s2", grade=EvalGrade.S, safety=SafetyMetrics(),
            efficiency=EfficiencyMetrics(avg_speed_ms=2.0),
            comfort=ComfortMetrics(avg_acceleration_ms2=3.0), passed=True,
        ))
        avg = ev.summary()["metric_averages"]
        assert avg["avg_speed_ms"] == pytest.approx(3.0)
        assert avg["avg_acceleration_ms2"] == pytest.approx(2.0)
