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


class TestBatchEvaluator:
    """批量评估测试。"""

    def _make_result(self, scene_id: str, passed: bool, collisions: int = 0) -> SceneEvaluationResult:
        safety = SafetyMetrics(collision_count=collisions)
        return SceneEvaluationResult(
            scene_id=scene_id,
            grade=EvalGrade.S if passed else EvalGrade.D,
            safety=safety,
            efficiency=EfficiencyMetrics(),
            comfort=ComfortMetrics(),
            passed=passed,
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
