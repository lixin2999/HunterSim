"""模块 4.2 报告器的单元测试（图表渲染对 matplotlib 缺失/存在两路均覆盖）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from hunter_sim.evaluation.models import TrajectoryMetrics, TrajectorySeries
from hunter_sim.evaluation.reporter import ReporterImpl


def test_build_report() -> None:
    traj = TrajectoryMetrics(ade=0.5, fde=1.0, max_error=1.0)
    report = ReporterImpl().build_report(
        run_id="r1", scenario_name="default", trajectory=traj, extras={"note": "x"}
    )
    assert report.run_id == "r1"
    assert report.generated_at is not None
    assert report.trajectory is not None and report.trajectory.ade == 0.5
    assert report.extras == {"note": "x"}


async def test_write_json_atomic(tmp_path: Path) -> None:
    report = ReporterImpl().build_report(run_id="r1", scenario_name="default")
    path = tmp_path / "nested" / "report.json"
    out = await ReporterImpl().write_json(report, path)
    assert out == path and path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "r1"
    assert not any(p.name.endswith(".tmp") for p in path.parent.iterdir())


def test_render_chart_degrades_without_matplotlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    result = ReporterImpl().render_chart(
        TrajectorySeries(values=np.array([1.0, 2.0, 3.0]), label="err"),
        tmp_path / "chart.png",
    )
    assert result is None


def test_render_chart_success_with_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mpl = MagicMock()
    fake_pyplot = MagicMock()
    fake_pyplot.subplots.return_value = (MagicMock(), MagicMock())
    fake_mpl.pyplot = fake_pyplot
    monkeypatch.setitem(sys.modules, "matplotlib", fake_mpl)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", fake_pyplot)

    path = tmp_path / "chart.png"
    result = ReporterImpl().render_chart(
        TrajectorySeries(values=np.array([1.0, 2.0, 3.0]), label="err"), path
    )
    assert result == path
    fake_pyplot.subplots.assert_called_once()
    fake_pyplot.close.assert_called_once()
