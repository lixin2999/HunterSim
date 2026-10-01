"""评估报告生成器（PROMPT-ENG-007-A）。

支持生成 JSON 和 HTML 格式的评估报告，包含：
- 单场景报告结构（设计文档 §9.4：safety_metrics / efficiency_metrics /
  comfort_metrics / success_criteria / events / grade）
- 批量汇总与趋势对比（设计文档 §9.5）
- 等级判定（S/A/B/C/D）与事件时间线
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hunter_sim.common.utils import get_logger
from hunter_sim.eval_service.evaluation_engine import (
    BatchEvaluator,
    SceneEvaluationResult,
)

logger = get_logger(__name__)

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HunterSim 仿真评估报告 - {report_id}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
  .header {{ background: #1a237e; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
  .header h1 {{ margin: 0 0 8px; font-size: 24px; }}
  .header .meta {{ opacity: 0.8; font-size: 14px; }}
  .grade-badge {{ display: inline-block; padding: 4px 16px; border-radius: 20px; font-weight: bold; font-size: 18px; }}
  .grade-S {{ background: #4caf50; color: white; }}
  .grade-A {{ background: #8bc34a; color: white; }}
  .grade-B {{ background: #ffeb3b; color: #333; }}
  .grade-C {{ background: #ff9800; color: white; }}
  .grade-D {{ background: #f44336; color: white; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px; }}
  .card {{ background: white; border-radius: 8px; padding: 16px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
  .card h3 {{ margin: 0 0 8px; font-size: 14px; color: #666; }}
  .card .value {{ font-size: 28px; font-weight: bold; color: #1a237e; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
  th {{ background: #1a237e; color: white; padding: 12px 16px; text-align: left; font-size: 13px; }}
  td {{ padding: 10px 16px; border-bottom: 1px solid #eee; font-size: 13px; }}
  tr:hover td {{ background: #f0f4ff; }}
  .pass {{ color: #4caf50; font-weight: bold; }}
  .fail {{ color: #f44336; font-weight: bold; }}
  .section-title {{ font-size: 18px; margin: 24px 0 12px; color: #1a237e; }}
</style>
</head>
<body>
<div class="header">
  <h1>HunterSim 仿真评估报告</h1>
  <div class="meta">报告 ID: {report_id} | 生成时间: {generated_at} | 评估等级: <span class="grade-badge grade-{grade}">{grade}</span></div>
</div>
<div class="summary-grid">
  <div class="card"><h3>总场景数</h3><div class="value">{total}</div></div>
  <div class="card"><h3>通过数</h3><div class="value pass">{passed}</div></div>
  <div class="card"><h3>失败数</h3><div class="value fail">{failed}</div></div>
  <div class="card"><h3>通过率</h3><div class="value">{pass_rate_pct}%</div></div>
  <div class="card"><h3>平均碰撞次数</h3><div class="value">{avg_collision}</div></div>
</div>
<div class="section-title">场景详细结果</div>
<table>
<thead><tr><th>场景 ID</th><th>等级</th><th>通过</th><th>碰撞次数</th><th>最小TTC(s)</th><th>平均速度(m/s)</th><th>最大加速度(m/s²)</th><th>时间戳</th></tr></thead>
<tbody>{scene_rows}</tbody>
</table>
</body>
</html>
"""


class EvaluationReportGenerator:
    """评估报告生成器。

    Args:
        report_dir: 报告输出目录（None 时不自动写入文件）。
    """

    def __init__(self, report_dir: Optional[Path] = None) -> None:
        self._report_dir = report_dir
        if report_dir:
            report_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _scene_to_doc_format(
        result: SceneEvaluationResult,
        sim_instance_id: str = "",
        scene_name: str = "",
        start_time: str = "",
        end_time: str = "",
    ) -> dict[str, Any]:
        """将单场景评估结果转换为设计文档 §9.4 报告字段结构。

        Args:
            result: 单场景评估结果。
            sim_instance_id: 仿真实例 ID。
            scene_name: 场景名称。
            start_time: 场景开始时间（空则用评估时间）。
            end_time: 场景结束时间（空则用评估时间）。

        Returns:
            符合 §9.4 结构的报告字典。
        """
        ts = result.timestamp
        safety = result.safety
        efficiency = result.efficiency
        comfort = result.comfort

        def _num(value: Optional[float], digits: int) -> Optional[float]:
            """inf/None 统一输出为 None，其余四舍五入。"""
            if value is None or value == float("inf"):
                return None
            return round(value, digits)

        return {
            "sim_instance_id": sim_instance_id,
            "scene_id": result.scene_id,
            "scene_name": scene_name,
            "start_time": start_time or ts,
            "end_time": end_time or ts,
            "duration": round(efficiency.scene_duration_s, 2),
            "result": "passed" if result.passed else "failed",
            "safety_metrics": {
                "collisions": safety.collision_count,
                "min_ttc": _num(safety.min_ttc_s, 2),
                "min_distance": _num(safety.min_distance_m, 2),
                "emergency_brakes": safety.emergency_brake_count,
                "lane_invasions": safety.lane_invasion_count,
                "red_light_runs": safety.red_light_count,
            },
            "efficiency_metrics": {
                "avg_speed": round(efficiency.avg_speed_ms, 2),
                "max_speed": round(efficiency.max_speed_ms, 2),
                "distance": round(efficiency.total_distance_m, 2),
                "completion_time": round(efficiency.scene_duration_s, 2),
            },
            "comfort_metrics": {
                "avg_acceleration": round(comfort.avg_acceleration_ms2, 2),
                "max_acceleration": round(comfort.max_acceleration_ms2, 2),
                "avg_jerk": round(comfort.avg_jerk_ms3, 2),
                "steering_smoothness": round(comfort.steering_smoothness_rad_s, 3),
            },
            "success_criteria": result.success_criteria,
            "events": result.events,
            "grade": result.grade.value,
        }

    def generate_scene_report(
        self,
        result: SceneEvaluationResult,
        sim_instance_id: str = "",
        scene_name: str = "",
        start_time: str = "",
        end_time: str = "",
    ) -> dict[str, Any]:
        """生成单场景评估报告（设计文档 §9.4 结构）。

        Args:
            result: 单场景评估结果。
            sim_instance_id: 仿真实例 ID。
            scene_name: 场景名称。
            start_time: 场景开始时间。
            end_time: 场景结束时间。

        Returns:
            报告字典。
        """
        return self._scene_to_doc_format(
            result, sim_instance_id, scene_name, start_time, end_time
        )

    def generate_json(
        self,
        evaluator: BatchEvaluator,
        report_id: str = "",
        previous_summary: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """生成 JSON 格式批量评估报告。

        Args:
            evaluator: 已包含所有场景结果的批量评估器。
            report_id: 报告唯一 ID（空则自动生成时间戳 ID）。
            previous_summary: 上一版本批量评估摘要，提供时输出趋势对比
                （文档 §9.5 “与历史版本对比”）。

        Returns:
            报告字典，含 summary 字段与 §9.4 格式的 scenes 列表。
        """
        if not report_id:
            report_id = f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        summary = evaluator.summary()
        report: dict[str, Any] = {
            "report_id": report_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **summary,
            # 逐场景报告（§9.4 结构）
            "scenes": [
                self._scene_to_doc_format(r) for r in evaluator.results()
            ],
        }
        if previous_summary:
            report["trend"] = self._build_trend(summary, previous_summary)
        return report

    @staticmethod
    def _build_trend(
        current: dict[str, Any],
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        """计算当前与历史评估摘要的趋势对比（文档 §9.5）。"""
        return {
            "pass_rate_delta": round(
                current.get("pass_rate", 0) - previous.get("pass_rate", 0), 4
            ),
            "collision_rate_delta": round(
                current.get("collision_rate", 0) - previous.get("collision_rate", 0), 4
            ),
            "grade_change": f"{previous.get('overall_grade', 'D')} -> {current.get('overall_grade', 'D')}",
        }

    def generate_html(
        self,
        evaluator: BatchEvaluator,
        report_id: str = "",
    ) -> str:
        """生成 HTML 格式报告。

        Args:
            evaluator: 批量评估器。
            report_id: 报告 ID。

        Returns:
            HTML 字符串。
        """
        if not report_id:
            report_id = f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        summary = evaluator.summary()
        total = summary.get("total_scenes", 0)
        passed = summary.get("passed", 0)
        grade = summary.get("overall_grade", "D")

        scene_rows = ""
        for r in summary.get("results", []):
            safety = r.get("safety", {})
            efficiency = r.get("efficiency", {})
            comfort = r.get("comfort", {})
            passed_cls = "pass" if r.get("passed") else "fail"
            ttc = safety.get("min_ttc_s")
            ttc_str = f"{ttc:.2f}" if ttc is not None else "-"
            avg_speed = efficiency.get("avg_speed_ms", 0)
            max_acc = comfort.get("max_acceleration_ms2", 0)
            scene_rows += (
                f"<tr>"
                f"<td>{r.get('scene_id', '-')}</td>"
                f"<td><span class='grade-badge grade-{r.get('grade', 'D')}'>{r.get('grade', '-')}</span></td>"
                f"<td class='{passed_cls}'>{'通过' if r.get('passed') else '失败'}</td>"
                f"<td>{safety.get('collision_count', 0)}</td>"
                f"<td>{ttc_str}</td>"
                f"<td>{avg_speed:.2f}</td>"
                f"<td>{max_acc:.2f}</td>"
                f"<td>{r.get('timestamp', '-')}</td>"
                f"</tr>\n"
            )

        html = _HTML_TEMPLATE.format(
            report_id=report_id,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            grade=grade,
            total=total,
            passed=passed,
            failed=total - passed,
            pass_rate_pct=round(summary.get("pass_rate", 0) * 100, 1),
            avg_collision=round(summary.get("avg_collision_count", 0), 2),
            scene_rows=scene_rows,
        )
        return html

    def save_report(
        self,
        evaluator: BatchEvaluator,
        report_id: str = "",
        formats: tuple[str, ...] = ("json", "html"),
    ) -> dict[str, str]:
        """保存报告到文件（需初始化时指定 report_dir）。

        Args:
            evaluator: 批量评估器。
            report_id: 报告 ID。
            formats: 需要生成的格式元组，可选 "json" / "html"。

        Returns:
            格式到文件路径的映射。
        """
        if self._report_dir is None:
            raise ValueError("report_dir not configured; cannot save files")

        paths: dict[str, str] = {}
        if not report_id:
            report_id = f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        if "json" in formats:
            data = self.generate_json(evaluator, report_id)
            fp = self._report_dir / f"{report_id}.json"
            fp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            paths["json"] = str(fp)
            logger.info(f"JSON report saved: {fp}")

        if "html" in formats:
            html = self.generate_html(evaluator, report_id)
            fp = self._report_dir / f"{report_id}.html"
            fp.write_text(html, encoding="utf-8")
            paths["html"] = str(fp)
            logger.info(f"HTML report saved: {fp}")

        return paths
