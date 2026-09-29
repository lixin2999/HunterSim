"""core.contracts 单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest
from pydantic import ValidationError

from hunter_sim.core.contracts import (
    CameraFrame,
    LidarFrame,
    RunMetadata,
    SynchronizedFrame,
    VehicleState,
)


def _vehicle_state(**overrides: object) -> VehicleState:
    base: dict[str, object] = dict(
        timestamp=1.0,
        frame_id=0,
        x=1.0,
        y=2.0,
        z=0.0,
        roll=0.0,
        pitch=0.0,
        yaw=90.0,
        velocity_x=10.0,
        velocity_y=0.0,
        velocity_z=0.0,
        acceleration_x=0.0,
        acceleration_y=0.0,
        acceleration_z=9.8,
        throttle=0.5,
        brake=0.0,
        steer=0.1,
        gear=3,
    )
    base.update(overrides)
    return VehicleState(**base)  # type: ignore[arg-type]


def test_vehicle_state_fields() -> None:
    state = _vehicle_state()
    assert state.timestamp == 1.0
    assert state.yaw == 90.0
    assert state.gear == 3


def test_vehicle_state_is_immutable() -> None:
    state = _vehicle_state()
    with pytest.raises(ValidationError):
        state.x = 99.0  # type: ignore[misc]


@pytest.mark.parametrize("throttle", [-0.1, 1.5])
def test_throttle_bounds_rejected(throttle: float) -> None:
    with pytest.raises(ValidationError):
        _vehicle_state(throttle=throttle)


def test_negative_frame_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _vehicle_state(frame_id=-1)


def test_camera_frame_holds_numpy(sample_image: np.ndarray) -> None:
    frame = CameraFrame(
        timestamp=0.5,
        frame_id=1,
        sensor_id="rgb_front",
        image=sample_image,
        width=8,
        height=8,
        fov=90.0,
    )
    assert frame.image.shape == (8, 8, 3)
    assert frame.image.dtype == np.uint8


def test_lidar_frame_holds_points(sample_points: np.ndarray) -> None:
    frame = LidarFrame(
        timestamp=0.5,
        frame_id=1,
        sensor_id="lidar_top",
        points=sample_points,
        point_count=16,
    )
    assert frame.points.shape == (16, 4)


def test_synchronized_frame_default_collections() -> None:
    frame = SynchronizedFrame(
        timestamp=2.0,
        frame_id=2,
        vehicle_state=_vehicle_state(frame_id=2, timestamp=2.0),
    )
    assert frame.cameras == {}
    assert frame.lidars == {}
    assert isinstance(frame.vehicle_state, VehicleState)


def test_run_metadata_finalize(sample_start_time: datetime) -> None:
    meta = RunMetadata(
        run_id="run-1",
        scenario_name="default_highway",
        map_name="Town04",
        start_time=sample_start_time,
    )
    assert meta.status == "running"
    meta.finalize(status="completed", end_time=sample_start_time + timedelta(seconds=120))
    assert meta.status == "completed"
    assert meta.duration_seconds == 120.0
    assert meta.end_time is not None


def test_run_metadata_finalize_twice_raises(sample_start_time: datetime) -> None:
    meta = RunMetadata(
        run_id="run-2",
        scenario_name="s",
        map_name="Town04",
        start_time=sample_start_time,
    )
    meta.finalize(status="failed", end_time=sample_start_time + timedelta(seconds=1))
    with pytest.raises(ValueError):
        meta.finalize(status="completed", end_time=sample_start_time + timedelta(seconds=2))
