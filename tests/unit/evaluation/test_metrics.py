"""模块 4.1 指标引擎的单元测试。"""

from __future__ import annotations

import numpy as np
import pytest

from hunter_sim.core.contracts import VehicleState
from hunter_sim.core.exceptions import ValidationError
from hunter_sim.evaluation.metrics import MetricsEngineImpl


def make_vs(ax: float, ay: float = 0.0, frame_id: int = 0) -> VehicleState:
    return VehicleState(
        timestamp=float(frame_id),
        frame_id=frame_id,
        x=0.0,
        y=0.0,
        z=0.0,
        roll=0.0,
        pitch=0.0,
        yaw=0.0,
        velocity_x=0.0,
        velocity_y=0.0,
        velocity_z=0.0,
        acceleration_x=ax,
        acceleration_y=ay,
        acceleration_z=9.8,
        throttle=0.0,
        brake=0.0,
        steer=0.0,
        gear=1,
    )


def test_trajectory_error_basic() -> None:
    predicted = np.array([[0.0, 0.0], [1.0, 1.0]], dtype=np.float64)
    reference = np.array([[0.0, 0.0], [1.0, 0.0]], dtype=np.float64)
    metrics = MetricsEngineImpl().trajectory_error(predicted, reference)
    assert metrics.ade == pytest.approx(0.5)
    assert metrics.fde == pytest.approx(1.0)
    assert metrics.max_error == pytest.approx(1.0)


def test_trajectory_error_ignores_extra_columns() -> None:
    predicted = np.array([[0.0, 0.0, 9.0]], dtype=np.float64)
    reference = np.array([[0.0, 0.0, 100.0]], dtype=np.float64)
    metrics = MetricsEngineImpl().trajectory_error(predicted, reference)
    assert metrics.ade == pytest.approx(0.0)


def test_trajectory_error_shape_mismatch() -> None:
    with pytest.raises(ValidationError):
        MetricsEngineImpl().trajectory_error(np.zeros((2, 2)), np.zeros((3, 2)))


def test_trajectory_error_empty() -> None:
    with pytest.raises(ValidationError):
        MetricsEngineImpl().trajectory_error(np.zeros((0, 2)), np.zeros((0, 2)))


def test_comfort_metrics() -> None:
    states = [make_vs(0.0, frame_id=0), make_vs(1.0, frame_id=1), make_vs(2.0, frame_id=2)]
    metrics = MetricsEngineImpl().comfort(states, dt=0.5, a_max=4.0, j_max=10.0)
    assert metrics.max_accel == pytest.approx(2.0)
    assert metrics.rms_accel == pytest.approx(float(np.sqrt(np.mean([0, 1, 4]))))
    # jerk = diff(accel)/dt = [2.0, 2.0] -> max 2.0, 无超阈 -> 得分 1.0
    assert metrics.max_jerk == pytest.approx(2.0)
    assert metrics.comfort_score == pytest.approx(1.0)


def test_comfort_penalizes_excess() -> None:
    states = [make_vs(10.0, frame_id=i) for i in range(3)]  # 全部超 a_max
    metrics = MetricsEngineImpl().comfort(states, dt=0.5, a_max=4.0, j_max=10.0)
    assert metrics.comfort_score == pytest.approx(0.0)


def test_comfort_invalid_inputs() -> None:
    engine = MetricsEngineImpl()
    with pytest.raises(ValidationError):
        engine.comfort([make_vs(0.0)], dt=0.5)  # 少于两个状态
    with pytest.raises(ValidationError):
        engine.comfort([make_vs(0.0), make_vs(1.0)], dt=0.0)


def test_safety_metrics() -> None:
    metrics = MetricsEngineImpl().safety(
        collision_count=2,
        lane_invasion_count=1,
        total_ticks=100,
        tick_rate=10.0,
        route_completion=0.5,
    )
    assert metrics.collision_rate == pytest.approx(0.2)  # 2 次 / 10 秒
    assert metrics.route_completion == pytest.approx(0.5)


def test_safety_invalid() -> None:
    engine = MetricsEngineImpl()
    with pytest.raises(ValidationError):
        engine.safety(collision_count=0, lane_invasion_count=0, total_ticks=10, tick_rate=0.0)
    with pytest.raises(ValidationError):
        engine.safety(
            collision_count=-1,
            lane_invasion_count=0,
            total_ticks=10,
            tick_rate=10.0,
        )


def test_coverage_metrics() -> None:
    metrics = MetricsEngineImpl().coverage(
        total_ticks=100, per_sensor_counts={"cam": 100, "lidar": 50}
    )
    assert metrics.per_sensor == {"cam": 1.0, "lidar": 0.5}
    assert metrics.overall == pytest.approx(0.75)


def test_coverage_clamps_and_zero_ticks() -> None:
    engine = MetricsEngineImpl()
    clamped = engine.coverage(total_ticks=10, per_sensor_counts={"cam": 20})
    assert clamped.per_sensor["cam"] == 1.0
    zero = engine.coverage(total_ticks=0, per_sensor_counts={"cam": 0})
    assert zero.per_sensor == {"cam": 0.0}
    assert zero.overall == 0.0
