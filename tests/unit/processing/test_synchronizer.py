"""模块 3.2 时间同步器的单元测试。"""

from __future__ import annotations

import numpy as np
import pytest

from hunter_sim.core.contracts import (
    CameraFrame,
    ImuFrame,
    LidarFrame,
    VehicleState,
)
from hunter_sim.processing.synchronizer import SynchronizerImpl


def make_camera(t: float, frame_id: int, sensor_id: str = "cam") -> CameraFrame:
    return CameraFrame(
        timestamp=t,
        frame_id=frame_id,
        sensor_id=sensor_id,
        image=np.zeros((2, 2, 3), dtype=np.uint8),
        width=2,
        height=2,
        fov=90.0,
    )


def make_lidar(t: float, frame_id: int, sensor_id: str = "lidar") -> LidarFrame:
    return LidarFrame(
        timestamp=t,
        frame_id=frame_id,
        sensor_id=sensor_id,
        points=np.zeros((4, 4), dtype=np.float64),
        point_count=4,
    )


def make_imu(t: float, frame_id: int, sensor_id: str = "imu") -> ImuFrame:
    return ImuFrame(
        timestamp=t,
        frame_id=frame_id,
        sensor_id=sensor_id,
        accelerometer=(0.0, 0.0, 9.8),
        gyroscope=(0.0, 0.0, 0.0),
        compass=0.0,
    )


def make_vs(t: float, frame_id: int) -> VehicleState:
    return VehicleState(
        timestamp=t,
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
        acceleration_x=0.0,
        acceleration_y=0.0,
        acceleration_z=9.8,
        throttle=0.0,
        brake=0.0,
        steer=0.0,
        gear=1,
    )


def test_synchronize_matches_nearest() -> None:
    streams = {
        "cam": [make_camera(0.0, 0), make_camera(1.0, 1)],
        "lidar": [make_lidar(0.02, 0), make_lidar(0.9, 1)],  # 0.9 距锚点 1.0 超容差
        "imu": [make_imu(0.01, 0), make_imu(1.01, 1)],
    }
    vehicle_states = [make_vs(0.0, 0), make_vs(1.0, 1)]
    result = SynchronizerImpl(tolerance_seconds=0.05).synchronize(
        streams, reference="cam", vehicle_states=vehicle_states
    )
    assert len(result) == 2
    # 锚点 0：相机 +  lidar(0.02) + imu(0.01)
    assert result[0].frame_id == 0
    assert set(result[0].cameras) == {"cam"}
    assert set(result[0].lidars) == {"lidar"}
    assert set(result[0].imus) == {"imu"}
    # 锚点 1：相机 + imu(1.01)，lidar 无容差内样本
    assert result[1].frame_id == 1
    assert result[1].lidars == {}
    assert set(result[1].imus) == {"imu"}
    assert result[0].vehicle_state.timestamp == 0.0
    assert result[1].vehicle_state.timestamp == 1.0


def test_synchronize_picks_min_diff() -> None:
    streams = {
        "cam": [make_camera(0.0, 0)],
        "lidar": [make_lidar(0.04, 9), make_lidar(0.01, 8)],  # 乱序，均入容差
    }
    vehicle_states = [make_vs(0.0, 0)]
    result = SynchronizerImpl(tolerance_seconds=0.05).synchronize(
        streams, reference="cam", vehicle_states=vehicle_states
    )
    # 0.01 距锚点更近，应被选中
    assert result[0].lidars["lidar"].frame_id == 8


def test_synchronize_skips_anchor_without_vehicle_state() -> None:
    streams = {"cam": [make_camera(0.0, 0), make_camera(1.0, 1)]}
    result = SynchronizerImpl(tolerance_seconds=0.05).synchronize(
        streams, reference="cam", vehicle_states=[make_vs(5.0, 0)]
    )
    assert result == []


def test_synchronize_reference_missing_raises() -> None:
    streams = {"cam": [make_camera(0.0, 0)]}
    with pytest.raises(ValueError):
        SynchronizerImpl().synchronize(streams, reference="lidar", vehicle_states=[])


def test_synchronizer_invalid_tolerance() -> None:
    with pytest.raises(ValueError):
        SynchronizerImpl(tolerance_seconds=0.0)
