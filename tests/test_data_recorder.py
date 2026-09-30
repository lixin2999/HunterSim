"""ROS2 Bag 录制器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hunter_sim.common.exceptions import SensorSimulationError
from hunter_sim.common.models import ROS2_TOPIC_MAP
from hunter_sim.sensor_sim import data_recorder as dr_mod
from hunter_sim.sensor_sim.data_recorder import DataRecorder


class _FakePopen:
    last_cmd: list[str] = []

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:
        _FakePopen.last_cmd = list(cmd)
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dr_mod.subprocess, "Popen", _FakePopen)


class TestInit:
    def test_default_topics_from_map(self) -> None:
        rec = DataRecorder()
        # 文档 §6.3.2：默认录制仿真话题 /carla/*
        assert rec._topics == [m.sim_topic for m in ROS2_TOPIC_MAP]

    def test_custom_topics(self) -> None:
        rec = DataRecorder(topics=["/a", "/b"], bag_format="sqlite3")
        assert rec._topics == ["/a", "/b"]
        assert rec._bag_format == "sqlite3"

    def test_not_recording_initially(self) -> None:
        assert DataRecorder().is_recording is False


class TestStart:
    def test_start_missing_ros2_raises(self, tmp_path: Path) -> None:
        rec = DataRecorder(output_dir=tmp_path)
        with pytest.raises(SensorSimulationError):
            rec.start("s1")  # 'ros2' 可执行文件不存在 → FileNotFoundError → 包装

    def test_start_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch)
        rec = DataRecorder(output_dir=tmp_path, topics=["/vel"])
        rec.start("sess1")
        assert rec.is_recording is True
        assert (tmp_path / "sess1").exists()
        assert "/vel" in _FakePopen.last_cmd
        assert "--storage" in _FakePopen.last_cmd

    def test_start_when_already_recording_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch)
        rec = DataRecorder(output_dir=tmp_path)
        rec.start("a")
        rec.start("b")  # 第二次应被忽略
        assert rec._session_id == "a"


class TestStop:
    def test_stop_returns_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch)
        rec = DataRecorder(output_dir=tmp_path)
        rec.start("sess")
        out = rec.stop()
        assert out == tmp_path / "sess"
        assert rec.is_recording is False

    def test_stop_when_idle_returns_none(self) -> None:
        assert DataRecorder().stop() is None

    def test_recording_duration(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch)
        rec = DataRecorder(output_dir=tmp_path)
        assert rec.recording_duration_s == 0.0
        rec.start("d")
        assert rec.recording_duration_s >= 0.0
