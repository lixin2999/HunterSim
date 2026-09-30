"""数据加载器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.replay_service.data_loader import DataLoader, _quat_to_yaw


class TestUnsupportedSource:
    def test_unknown_source_raises(self) -> None:
        loader = DataLoader(data_source="influxdb")
        with pytest.raises(ConfigurationError):
            loader.load_trajectory(Path("x"))


class TestJsonSource:
    def test_load_frames(self, tmp_path: Path) -> None:
        frames = [
            {"timestamp": 1.0, "position": {"x": 0.0, "y": 0.0, "z": 0.0}},
            {"timestamp": 2.0, "position": {"x": 1.0, "y": 0.0, "z": 0.0}},
        ]
        f = tmp_path / "traj.json"
        f.write_text(json.dumps(frames), encoding="utf-8")
        loader = DataLoader(data_source="json")
        out = loader.load_trajectory(f)
        assert len(out) == 2
        assert out[1]["timestamp"] == 2.0

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        loader = DataLoader(data_source="json")
        with pytest.raises(ConfigurationError):
            loader.load_trajectory(tmp_path / "nope.json")


class TestRosbagSource:
    def test_rosbag_unavailable_raises(self, tmp_path: Path) -> None:
        # 非 ROS 环境下 rosbag2_py 不可导入，应转为 ConfigurationError
        loader = DataLoader(data_source="rosbag")
        with pytest.raises(ConfigurationError):
            loader.load_trajectory(tmp_path / "bag")


class TestQuatToYaw:
    def test_zero_quaternion(self) -> None:
        assert _quat_to_yaw(0.0, 1.0) == pytest.approx(0.0)

    def test_90_deg(self) -> None:
        qz = math.sin(math.radians(90.0) / 2.0)
        qw = math.cos(math.radians(90.0) / 2.0)
        assert _quat_to_yaw(qz, qw) == pytest.approx(90.0, abs=1e-6)
