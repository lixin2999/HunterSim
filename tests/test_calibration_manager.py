"""VIL 标定管理单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.engine.coordinate_converter import CalibrationParams
from hunter_sim.vil_mapper.calibration_manager import CalibrationManager


class TestCalibrationManager:
    def test_current_none_before_load(self, tmp_path: Path) -> None:
        mgr = CalibrationManager(tmp_path / "c.json")
        assert mgr.current is None

    def test_load_missing_creates_default(self, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "c.json"
        mgr = CalibrationManager(path)
        params = mgr.load()
        assert params.x0 == 0.0
        assert path.exists()  # 自动写入默认文件

    def test_save_then_load(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        mgr = CalibrationManager(path)
        mgr.save(CalibrationParams(x0=10.0, y0=20.0, yaw0=0.5))
        # 新管理器从磁盘读取
        mgr2 = CalibrationManager(path)
        loaded = mgr2.load()
        assert loaded.x0 == 10.0
        assert loaded.y0 == 20.0
        assert loaded.yaw0 == pytest.approx(0.5)

    def test_load_cached_returns_memory(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        mgr = CalibrationManager(path)
        mgr.update(CalibrationParams(x0=99.0))
        assert mgr.load().x0 == 99.0  # 命中内存缓存，不读盘

    def test_update_does_not_write(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        mgr = CalibrationManager(path)
        mgr.update(CalibrationParams(x0=5.0))
        assert not path.exists()
        assert mgr.current is not None
        assert mgr.current.x0 == 5.0

    def test_reset_to_default(self, tmp_path: Path) -> None:
        mgr = CalibrationManager(tmp_path / "c.json")
        mgr.update(CalibrationParams(x0=5.0, y0=6.0))
        default = mgr.reset_to_default()
        assert default.x0 == 0.0
        assert mgr.current is default

    def test_load_bad_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        path.write_text("{ not valid json", encoding="utf-8")
        mgr = CalibrationManager(path)
        with pytest.raises(ConfigurationError):
            mgr.load()

    def test_saved_file_is_json(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        CalibrationManager(path).save(CalibrationParams(x0=1.0))
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["x0"] == 1.0
