"""数字孪生轨迹对比与场景重建单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import pytest

from hunter_sim.replay_service.digital_twin import (
    SceneReconstructor,
    TwinMode,
    TrajectoryComparator,
)


def _frame(ts: float, x: float, y: float, yaw: float = 0.0, speed: float = 0.0) -> dict:
    return {
        "timestamp": ts,
        "position": {"x": x, "y": y},
        "rotation": {"yaw": yaw},
        "velocity": {"speed": speed},
    }


class TestTwinMode:
    def test_modes_present(self) -> None:
        assert {m.value for m in TwinMode} == {"realtime", "replay", "comparison"}


class TestTrajectoryComparator:
    def test_identical_trajectories_zero_error(self) -> None:
        traj = [_frame(i * 0.1, i, i) for i in range(5)]
        result = TrajectoryComparator().compare(traj, list(traj))
        assert result["position_rmse_m"] == pytest.approx(0.0)
        assert result["matched_frames"] == 5
        assert result["total_real_frames"] == 5

    def test_position_rmse_computed(self) -> None:
        real = [_frame(0.0, 0.0, 0.0)]
        sim = [_frame(0.0, 3.0, 4.0)]  # 误差 5.0
        result = TrajectoryComparator().compare(real, sim)
        assert result["position_rmse_m"] == pytest.approx(5.0)
        assert result["matched_frames"] == 1

    def test_unmatched_frames_skipped_by_tolerance(self) -> None:
        real = [_frame(0.0, 0.0, 0.0), _frame(10.0, 1.0, 1.0)]
        sim = [_frame(0.0, 0.0, 0.0)]  # 第二帧无近似时间戳
        result = TrajectoryComparator().compare(real, sim, time_tolerance_s=0.1)
        assert result["matched_frames"] == 1
        assert result["total_real_frames"] == 2

    def test_heading_and_speed_rmse(self) -> None:
        real = [_frame(0.0, 0.0, 0.0, yaw=0.5, speed=10.0)]
        sim = [_frame(0.0, 0.0, 0.0, yaw=0.0, speed=4.0)]
        result = TrajectoryComparator().compare(real, sim)
        assert result["heading_rmse_deg"] == pytest.approx(0.5)
        assert result["speed_rmse_ms"] == pytest.approx(6.0)

    def test_empty_sim_returns_zero(self) -> None:
        real = [_frame(0.0, 1.0, 1.0)]
        result = TrajectoryComparator().compare(real, [])
        assert result["matched_frames"] == 0
        assert result["position_rmse_m"] == 0.0

    def test_find_nearest_frame_static(self) -> None:
        sim_by_ts = {1.0: _frame(1.0, 0, 0), 2.0: _frame(2.0, 5, 5)}
        matched = TrajectoryComparator._find_nearest_frame(sim_by_ts, 1.05, 0.1)
        assert matched is not None
        assert matched["timestamp"] == 1.0
        assert TrajectoryComparator._find_nearest_frame(sim_by_ts, 5.0, 0.1) is None


class TestSceneReconstructor:
    def test_ego_start_and_duration(self) -> None:
        traj = [_frame(10.0, 1.0, 2.0), _frame(40.0, 5.0, 6.0)]
        config = SceneReconstructor().reconstruct(traj, [], map_id="Town01")
        assert config["map_id"] == "Town01"
        assert config["ego_vehicle"]["spawn_point"]["x"] == 1.0
        assert config["ego_vehicle"]["spawn_point"]["y"] == 2.0
        assert config["duration_seconds"] == pytest.approx(30.0)

    def test_default_duration_single_frame(self) -> None:
        traj = [_frame(0.0, 0.0, 0.0)]
        config = SceneReconstructor().reconstruct(traj, [], map_id="Town02")
        assert config["duration_seconds"] == 60.0

    def test_participants_deduplicated_by_id(self) -> None:
        traj = [_frame(0.0, 0.0, 0.0), _frame(1.0, 0.0, 0.0)]
        perception = [
            {"objects": [{"id": 1, "type": "vehicle", "x": 3.0, "y": 4.0, "speed": 6.0}]},
            {"objects": [{"id": 1, "type": "vehicle", "x": 9.0, "y": 9.0}]},  # 重复 id 忽略
            {"objects": [{"id": 2, "type": "pedestrian", "x": 1.0, "y": 1.0}]},
        ]
        config = SceneReconstructor().reconstruct(traj, perception, map_id="Town03")
        parts = config["traffic_participants"]
        assert len(parts) == 2
        by_id = {p["participant_id"]: p for p in parts}
        assert by_id["reconstructed_1"]["spawn_point"]["x"] == 3.0  # 取首次出现
        assert by_id["reconstructed_1"]["actor_type"] == "vehicle"
        assert by_id["reconstructed_2"]["actor_type"] == "walker"

    def test_cyclist_type_mapping(self) -> None:
        # 文档 §7.2：非机动车映射为 cyclist + 自行车蓝图
        traj = [_frame(0.0, 0.0, 0.0)]
        perception = [{"objects": [{"id": 7, "type": "bicycle", "x": 2.0, "y": 1.0}]}]
        config = SceneReconstructor().reconstruct(traj, perception, map_id="Town03")
        part = config["traffic_participants"][0]
        assert part["actor_type"] == "cyclist"
        assert part["blueprint"] == "vehicle.diamondback.century"

    def test_participants_capped_at_50(self) -> None:
        traj = [_frame(0.0, 0.0, 0.0)]
        objects = [{"id": i, "type": "vehicle"} for i in range(80)]
        config = SceneReconstructor().reconstruct(traj, [{"objects": objects}], "Town04")
        assert len(config["traffic_participants"]) == 50

    def test_empty_trajectory_defaults(self) -> None:
        config = SceneReconstructor().reconstruct([], [], map_id="Town05")
        assert config["ego_vehicle"]["spawn_point"]["x"] == 0.0
        assert config["duration_seconds"] == 60.0
