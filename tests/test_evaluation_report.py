"""评估报告生成器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter_sim.common.models import EvalGrade
from hunter_sim.eval_service.evaluation_engine import (
    BatchEvaluator,
    ComfortMetrics,
    EfficiencyMetrics,
    SafetyMetrics,
    SceneEvaluationResult,
)
from hunter_sim.eval_service.evaluation_report import EvaluationReportGenerator


def _evaluator(n_pass: int = 3, n_fail: int = 1) -> BatchEvaluator:
    ev = BatchEvaluator()
    for i in range(n_pass):
        ev.add_result(
            SceneEvaluationResult(
                scene_id=f"pass_{i}",
                grade=EvalGrade.S,
                safety=SafetyMetrics(collision_count=0, min_ttc_s=5.0),
                efficiency=EfficiencyMetrics(avg_speed_ms=3.2),
                comfort=ComfortMetrics(max_acceleration_ms2=1.5),
                passed=True,
            )
        )
    for i in range(n_fail):
        ev.add_result(
            SceneEvaluationResult(
                scene_id=f"fail_{i}",
                grade=EvalGrade.D,
                safety=SafetyMetrics(collision_count=2, min_ttc_s=None),
                efficiency=EfficiencyMetrics(),
                comfort=ComfortMetrics(),
                passed=False,
            )
        )
    return ev


class TestGenerateJson:
    def test_basic_structure(self) -> None:
        gen = EvaluationReportGenerator()
        report = gen.generate_json(_evaluator(), report_id="rid-1")
        assert report["report_id"] == "rid-1"
        assert report["total_scenes"] == 4
        assert report["passed"] == 3
        assert "generated_at" in report

    def test_auto_report_id(self) -> None:
        gen = EvaluationReportGenerator()
        report = gen.generate_json(_evaluator())
        assert report["report_id"].startswith("report_")

    def test_scenes_use_doc_format(self) -> None:
        """scenes 条目应符合设计文档 §9.4 字段结构。"""
        gen = EvaluationReportGenerator()
        report = gen.generate_json(_evaluator(), report_id="rid-doc")
        assert len(report["scenes"]) == 4
        scene = report["scenes"][0]
        assert scene["result"] == "passed"
        assert set(scene["safety_metrics"]) == {
            "collisions", "min_ttc", "min_distance",
            "emergency_brakes", "lane_invasions", "red_light_runs",
        }
        assert set(scene["efficiency_metrics"]) == {
            "avg_speed", "max_speed", "distance", "completion_time",
        }
        assert set(scene["comfort_metrics"]) == {
            "avg_acceleration", "max_acceleration", "avg_jerk", "steering_smoothness",
        }
        assert "success_criteria" in scene and "grade" in scene

    def test_trend_with_previous_summary(self) -> None:
        """提供历史摘要时输出趋势对比（文档 §9.5）。"""
        gen = EvaluationReportGenerator()
        previous = {"pass_rate": 0.5, "collision_rate": 0.5, "overall_grade": "C"}
        report = gen.generate_json(_evaluator(), previous_summary=previous)
        trend = report["trend"]
        assert trend["pass_rate_delta"] == pytest.approx(0.25)
        assert trend["collision_rate_delta"] == pytest.approx(-0.25)
        assert trend["grade_change"] == "C -> D"


class TestGenerateSceneReport:
    """单场景报告测试（设计文档 §9.4）。"""

    def test_full_structure(self) -> None:
        result = SceneEvaluationResult(
            scene_id="scene_001",
            grade=EvalGrade.A,
            safety=SafetyMetrics(collision_count=0, min_ttc_s=4.2, min_distance_m=5.1),
            efficiency=EfficiencyMetrics(
                avg_speed_ms=3.5, max_speed_ms=5.0,
                total_distance_m=105.2, scene_duration_s=28.5,
            ),
            comfort=ComfortMetrics(
                avg_acceleration_ms2=0.8, max_acceleration_ms2=1.5,
                avg_jerk_ms3=1.2, steering_smoothness_rad_s=0.2,
            ),
            passed=True,
            success_criteria={"no_collision": True, "max_speed_deviation": 1.2, "min_safe_distance": 5.1},
            events=[{"time": 5.2, "type": "actor_decelerate", "description": "前车开始减速"}],
        )
        gen = EvaluationReportGenerator()
        report = gen.generate_scene_report(
            result, sim_instance_id="sim_001", scene_name="城市道路跟车场景",
            start_time="2026-08-19T10:00:00Z", end_time="2026-08-19T10:00:30Z",
        )
        assert report["sim_instance_id"] == "sim_001"
        assert report["scene_name"] == "城市道路跟车场景"
        assert report["start_time"] == "2026-08-19T10:00:00Z"
        assert report["duration"] == 28.5
        assert report["result"] == "passed"
        assert report["safety_metrics"]["min_ttc"] == 4.2
        assert report["efficiency_metrics"]["max_speed"] == 5.0
        assert report["comfort_metrics"]["steering_smoothness"] == 0.2
        assert report["success_criteria"]["no_collision"] is True
        assert report["grade"] == "A"

    def test_inf_metrics_render_as_none(self) -> None:
        """无采样数据（inf/None）时安全指标输出 None。"""
        result = SceneEvaluationResult(
            scene_id="s", grade=EvalGrade.D, safety=SafetyMetrics(),
            efficiency=EfficiencyMetrics(), comfort=ComfortMetrics(), passed=False,
        )
        gen = EvaluationReportGenerator()
        report = gen.generate_scene_report(result)
        assert report["safety_metrics"]["min_ttc"] is None
        assert report["safety_metrics"]["min_distance"] is None


class TestGenerateHtml:
    def test_contains_summary_and_rows(self) -> None:
        gen = EvaluationReportGenerator()
        html = gen.generate_html(_evaluator(), report_id="rid-html")
        assert "rid-html" in html
        assert "HunterSim" in html
        assert "pass_0" in html
        assert "fail_0" in html

    def test_ttc_dash_when_none(self) -> None:
        gen = EvaluationReportGenerator()
        html = gen.generate_html(_evaluator())
        # fail 场景 min_ttc 为 None，渲染为占位符
        assert "-" in html


class TestSaveReport:
    def test_requires_report_dir(self) -> None:
        gen = EvaluationReportGenerator()
        with pytest.raises(ValueError):
            gen.save_report(_evaluator())

    def test_save_json_and_html(self, tmp_path: Path) -> None:
        gen = EvaluationReportGenerator(report_dir=tmp_path)
        paths = gen.save_report(_evaluator(), report_id="save-1")
        assert Path(paths["json"]).exists()
        assert Path(paths["html"]).exists()
        assert "save-1" in paths["json"]

    def test_save_single_format(self, tmp_path: Path) -> None:
        gen = EvaluationReportGenerator(report_dir=tmp_path)
        paths = gen.save_report(_evaluator(), report_id="only-json", formats=("json",))
        assert "json" in paths
        assert "html" not in paths
