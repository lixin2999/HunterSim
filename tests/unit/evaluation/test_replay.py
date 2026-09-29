"""模块 4.3 数据回放的单元测试。

通过与 L2 :class:`DiskDataWriterImpl` 联动构造真实运行目录，验证 writer→replay 闭环对称。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from hunter_sim.acquisition.writer import DiskDataWriterImpl
from hunter_sim.core.contracts import CameraFrame, ImuFrame, LidarFrame
from hunter_sim.core.exceptions import DataReplayError
from hunter_sim.evaluation.replay import ReplayerImpl


@pytest.fixture()
async def run_root(tmp_path: Path) -> Path:
    """用 L2 写入器构造一个含相机(.npy)/LiDAR(.pcd)/IMU(.json)+metadata 的运行目录。"""
    writer = DiskDataWriterImpl(
        run_id="run1", base_dir=tmp_path, image_format="npy", pointcloud_format="pcd"
    )
    image = np.arange(2 * 2 * 3, dtype=np.uint8).reshape(2, 2, 3)
    await writer.write(
        "cam_front",
        CameraFrame(
            timestamp=1.0,
            frame_id=5,
            sensor_id="cam_front",
            image=image,
            width=2,
            height=2,
            fov=90.0,
        ),
    )
    points = np.array([[1.0, 2.0, 3.0, 0.5], [4.0, 5.0, 6.0, 0.1]], dtype=np.float64)
    await writer.write(
        "lidar_top",
        LidarFrame(timestamp=1.0, frame_id=3, sensor_id="lidar_top", points=points, point_count=2),
    )
    await writer.write(
        "imu",
        ImuFrame(
            timestamp=1.0,
            frame_id=2,
            sensor_id="imu",
            accelerometer=(1.0, 2.0, 3.0),
            gyroscope=(0.0, 0.0, 0.0),
            compass=90.0,
        ),
    )
    await writer.write_metadata({"run_id": "run1", "status": "completed"})
    return tmp_path / "run1"


def test_list_sensors(run_root: Path) -> None:
    assert ReplayerImpl().list_sensors(run_root) == ["cam_front", "lidar_top", "telemetry/imu"]


def test_load_metadata(run_root: Path) -> None:
    assert ReplayerImpl().load_metadata(run_root)["status"] == "completed"


def test_load_metadata_missing(tmp_path: Path) -> None:
    with pytest.raises(DataReplayError):
        ReplayerImpl().load_metadata(tmp_path)


def test_list_sensors_missing_root(tmp_path: Path) -> None:
    with pytest.raises(DataReplayError):
        ReplayerImpl().list_sensors(tmp_path / "ghost")


async def test_iter_npy(run_root: Path) -> None:
    frames = [f async for f in ReplayerImpl().iter_frames(run_root, "cam_front")]
    assert len(frames) == 1
    assert frames[0].frame_id == 5
    assert frames[0].payload.shape == (2, 2, 3)
    assert frames[0].payload.dtype == np.uint8


async def test_iter_pcd(run_root: Path) -> None:
    frames = [f async for f in ReplayerImpl().iter_frames(run_root, "lidar_top")]
    assert len(frames) == 1
    assert frames[0].frame_id == 3
    assert frames[0].payload.shape == (2, 4)
    expected = np.array([[1.0, 2.0, 3.0, 0.5], [4.0, 5.0, 6.0, 0.1]])
    assert np.allclose(frames[0].payload, expected, atol=1e-5)


async def test_iter_telemetry_json(run_root: Path) -> None:
    frames = [f async for f in ReplayerImpl().iter_frames(run_root, "telemetry/imu")]
    assert len(frames) == 1
    assert frames[0].frame_id == 2
    assert frames[0].payload["compass"] == 90.0


async def test_iter_missing_dir(run_root: Path) -> None:
    with pytest.raises(DataReplayError):
        async for _ in ReplayerImpl().iter_frames(run_root, "ghost"):
            pass


@pytest.fixture()
async def storage_run_root(tmp_path: Path) -> Path:
    """用写入器构造含 LiDAR(.h5) + IMU(.msgpack) 的运行目录（需 h5py/msgpack）。"""
    pytest.importorskip("h5py")
    pytest.importorskip("msgpack")
    writer = DiskDataWriterImpl(
        run_id="runh5",
        base_dir=tmp_path,
        pointcloud_format="hdf5",
        telemetry_format="msgpack",
    )
    points = np.array([[1.0, 2.0, 3.0, 0.5], [4.0, 5.0, 6.0, 0.1]], dtype=np.float64)
    await writer.write(
        "lidar_top",
        LidarFrame(timestamp=1.0, frame_id=3, sensor_id="lidar_top", points=points, point_count=2),
    )
    await writer.write(
        "imu",
        ImuFrame(
            timestamp=1.0,
            frame_id=2,
            sensor_id="imu",
            accelerometer=(1.0, 2.0, 3.0),
            gyroscope=(0.0, 0.0, 0.0),
            compass=90.0,
        ),
    )
    await writer.write_metadata({"run_id": "runh5", "status": "completed"})
    return tmp_path / "runh5"


async def test_iter_hdf5_roundtrip(storage_run_root: Path) -> None:
    frames = [f async for f in ReplayerImpl().iter_frames(storage_run_root, "lidar_top")]
    assert len(frames) == 1
    assert frames[0].frame_id == 3
    assert frames[0].payload.shape == (2, 4)
    expected = np.array([[1.0, 2.0, 3.0, 0.5], [4.0, 5.0, 6.0, 0.1]])
    assert np.allclose(frames[0].payload, expected)


async def test_iter_msgpack_roundtrip(storage_run_root: Path) -> None:
    frames = [f async for f in ReplayerImpl().iter_frames(storage_run_root, "telemetry/imu")]
    assert len(frames) == 1
    assert frames[0].payload["compass"] == 90.0


def test_list_sensors_includes_storage_exts(storage_run_root: Path) -> None:
    assert set(ReplayerImpl().list_sensors(storage_run_root)) == {"lidar_top", "telemetry/imu"}


async def test_iter_hdf5_requires_h5py(
    storage_run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "h5py", None)
    with pytest.raises(DataReplayError, match="h5py"):
        async for _ in ReplayerImpl().iter_frames(storage_run_root, "lidar_top"):
            pass


async def test_iter_msgpack_requires_msgpack(
    storage_run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "msgpack", None)
    with pytest.raises(DataReplayError, match="msgpack"):
        async for _ in ReplayerImpl().iter_frames(storage_run_root, "telemetry/imu"):
            pass
