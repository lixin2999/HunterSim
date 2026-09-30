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
