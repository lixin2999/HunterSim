"""模块 4.2：评估报告汇总、JSON 持久化与图表渲染。

- :meth:`build_report` 聚合各指标为 :class:`EvaluationReport`。
- :meth:`write_json` 异步原子写报告 JSON（``asyncio.to_thread`` + ``os.replace``）。
- :meth:`render_chart` **惰性** 依赖 matplotlib：库缺失时优雅降级返回 ``None``，不阻断流程。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunter_sim.core.logging import logger
from hunter_sim.evaluation.models import (
    ComfortMetrics,
    CoverageMetrics,
    EvaluationReport,
    SafetyMetrics,
    TrajectoryMetrics,
    TrajectorySeries,
)


class ReporterImpl:
    """满足 :class:`~hunter_sim.evaluation.protocols.Reporter` 契约。"""

    def build_report(
        self,
        *,
        run_id: str,
        scenario_name: str,
        trajectory: TrajectoryMetrics | None = None,
        comfort: ComfortMetrics | None = None,
        safety: SafetyMetrics | None = None,
        coverage: CoverageMetrics | None = None,
        extras: dict[str, Any] | None = None,
    ) -> EvaluationReport:
        """聚合指标构建评估报告，``generated_at`` 默认取当前 UTC 时间。"""
        return EvaluationReport(
            run_id=run_id,
            scenario_name=scenario_name,
            generated_at=datetime.now(UTC),
            trajectory=trajectory,
            comfort=comfort,
            safety=safety,
            coverage=coverage,
            extras=extras or {},
        )

    async def write_json(self, report: EvaluationReport, path: Path) -> Path:
        """异步原子写报告为 JSON，返回落盘路径。"""
        text = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
        await asyncio.to_thread(_write_text_atomic, path, text)
        return path

    def render_chart(self, payload: Any, path: Path) -> Path | None:
        """渲染单条数值序列为折线图；matplotlib 不可用时返回 ``None``（降级）。"""
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            logger.bind(component="reporter").warning("matplotlib 未安装，跳过图表渲染")
            return None

        series: TrajectorySeries = payload
        figure, axes = plt.subplots()
        axes.plot(list(series.values), marker="o")
        axes.set_title(series.label)
        axes.set_xlabel("step")
        axes.set_ylabel(series.label)
        figure.tight_layout()
        path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(path)
        plt.close(figure)
        return path


def _write_text_atomic(path: Path, text: str) -> None:
    """同步原子写文本：临时文件 + ``os.replace``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


__all__ = ["ReporterImpl"]
