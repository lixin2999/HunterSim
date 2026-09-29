"""模块 2.3 数据写入器的单元测试（npy / pcd / json 免依赖编码 + hdf5 / msgpack 可选编码）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from hunter_sim.acquisition import writer as writer_mod
from hunter_sim.acquisition.writer import DiskDataWriterImpl
from hunter_sim.core.contracts import (
    CameraFrame,
    GnssFrame,
    ImuFrame,
    LidarFrame,
    RadarFrame,
)
from hunter_sim.core.exceptions import DataWriteError


@pytest.fixture()
def camera_frame(sample_image: np.ndarray) -> CameraFrame:
    return CameraFrame(
        timestamp=1.0,
        frame_id=5,
        sensor_id="cam_front",
        image=sample_image,
        width=8,
        height=8,
        fov=90.0,
    )


@pytest.fixture()
def lidar_frame(sample_points: np.ndarray) -> LidarFrame:
    return LidarFrame(
        timestamp=1.0, frame_id=3, sensor_id="lidar_top", points=sample_points, point_count=16
    )


async def test_write_camera_npy(tmp_path: Path, camera_frame: CameraFrame) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, image_format="npy")
    path = await w.write("cam_front", camera_frame)
    assert path.suffix == ".npy"
    assert path.exists()
    assert (np.load(path) == camera_frame.image).all()


async def test_write_lidar_pcd(tmp_path: Path, lidar_frame: LidarFrame) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path)
    path = await w.write("lidar_top", lidar_frame)
    assert path.suffix == ".pcd"
    lines = path.read_text().splitlines()
    assert lines[0] == "VERSION .7"
    assert "POINTS 16" in lines
    assert "DATA ascii" in lines
    assert len(lines) == 10 + 16  # 头部 10 行 + 16 点


async def test_write_lidar_npy(tmp_path: Path, lidar_frame: LidarFrame) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, pointcloud_format="npy")
    path = await w.write("lidar_top", lidar_frame)
    assert path.suffix == ".npy"
    assert np.load(path).shape == (16, 4)


async def test_write_radar_npy(tmp_path: Path) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path)
    detections = np.zeros((4, 4), dtype=np.float64)
    radar = RadarFrame(
        timestamp=1.0,
        frame_id=1,
        sensor_id="radar_front",
        detections=detections,
        detection_count=4,
    )
    path = await w.write("radar_front", radar)
    assert path.suffix == ".npy"
    assert np.load(path).shape == (4, 4)


async def test_write_telemetry_json(tmp_path: Path) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path)
    imu = ImuFrame(
        timestamp=1.0,
        frame_id=2,
        sensor_id="imu",
        accelerometer=(1.0, 2.0, 3.0),
        gyroscope=(0.0, 0.0, 0.0),
        compass=90.0,
    )
    path = await w.write("imu", imu)
    assert "telemetry" in path.parts
    assert path.suffix == ".json"
    payload = json.loads(path.read_text())
    assert payload["frame_id"] == 2
    assert payload["compass"] == 90.0


async def test_write_gnss_json(tmp_path: Path) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path)
    gnss = GnssFrame(
        timestamp=1.0, frame_id=4, sensor_id="gnss", latitude=10.0, longitude=20.0, altitude=30.0
    )
    path = await w.write("gnss", gnss)
    assert json.loads(path.read_text())["latitude"] == 10.0


async def test_write_vehicle_state_json(tmp_path: Path, sample_vehicle_state: object) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path)
    path = await w.write("ego", sample_vehicle_state)
    assert json.loads(path.read_text())["yaw"] == 90.0


async def test_write_batch(tmp_path: Path, lidar_frame: LidarFrame) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, pointcloud_format="npy")
    paths = await w.write_batch("lidar_top", [lidar_frame, lidar_frame])
    assert len(paths) == 2
    assert all(p.exists() for p in paths)


async def test_write_metadata(tmp_path: Path) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path)
    path = await w.write_metadata({"run_id": "run1", "status": "completed"})
    assert path.name == "metadata.json"
    assert json.loads(path.read_text())["status"] == "completed"


async def test_unsupported_type(tmp_path: Path) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path)
    with pytest.raises(DataWriteError):
        await w.write("x", object())


async def test_write_after_close_raises(tmp_path: Path, lidar_frame: LidarFrame) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path)
    await w.close()
    with pytest.raises(DataWriteError):
        await w.write("lidar_top", lidar_frame)


async def test_atomic_leaves_no_tmp(tmp_path: Path, lidar_frame: LidarFrame) -> None:
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path)
    await w.write("lidar_top", lidar_frame)
    assert not any(p.name.endswith(".tmp") for p in tmp_path.rglob("*"))


async def test_retry_on_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    original = writer_mod._write_bytes_atomic

    def flaky(path: Path, data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("disk busy")
        original(path, data)

    monkeypatch.setattr(writer_mod, "_write_bytes_atomic", flaky)
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, max_retries=3, retry_delay=0.0)
    imu = ImuFrame(
        timestamp=1.0,
        frame_id=9,
        sensor_id="imu",
        accelerometer=(1.0, 2.0, 3.0),
        gyroscope=(0.0, 0.0, 0.0),
        compass=0.0,
    )
    path = await w.write("imu", imu)
    assert path.exists()
    assert calls["n"] == 2


async def test_retry_exhausted_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def always_fail(path: Path, data: bytes) -> None:
        raise OSError("no space")

    monkeypatch.setattr(writer_mod, "_write_bytes_atomic", always_fail)
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, max_retries=2, retry_delay=0.0)
    imu = ImuFrame(
        timestamp=1.0,
        frame_id=9,
        sensor_id="imu",
        accelerometer=(1.0, 2.0, 3.0),
        gyroscope=(0.0, 0.0, 0.0),
        compass=0.0,
    )
    with pytest.raises(DataWriteError):
        await w.write("imu", imu)


# -- HDF5 点云（可选依赖 h5py）---------------------------------------------


async def test_write_lidar_hdf5(tmp_path: Path, lidar_frame: LidarFrame) -> None:
    h5py = pytest.importorskip("h5py")
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, pointcloud_format="hdf5")
    path = await w.write("lidar_top", lidar_frame)
    assert path.suffix == ".h5"
    with h5py.File(path, "r") as handle:
        loaded = handle[writer_mod.HDF5_POINT_DATASET][:]
    assert loaded.shape == (16, 4)
    assert np.allclose(loaded, lidar_frame.points)


async def test_write_lidar_hdf5_bad_shape(tmp_path: Path) -> None:
    pytest.importorskip("h5py")
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, pointcloud_format="hdf5")
    bad = LidarFrame(
        timestamp=1.0,
        frame_id=0,
        sensor_id="lidar_top",
        points=np.zeros((3,), dtype=np.float64),
        point_count=3,
    )
    with pytest.raises(DataWriteError, match="形状"):
        await w.write("lidar_top", bad)


async def test_write_lidar_hdf5_requires_h5py(
    tmp_path: Path, lidar_frame: LidarFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "h5py", None)  # 强制 import h5py 抛 ImportError
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, pointcloud_format="hdf5")
    with pytest.raises(DataWriteError, match="h5py"):
        await w.write("lidar_top", lidar_frame)


# -- msgpack 遥测（可选依赖 msgpack）--------------------------------------


async def test_write_telemetry_msgpack(tmp_path: Path) -> None:
    msgpack = pytest.importorskip("msgpack")
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, telemetry_format="msgpack")
    imu = ImuFrame(
        timestamp=1.0,
        frame_id=2,
        sensor_id="imu",
        accelerometer=(1.0, 2.0, 3.0),
        gyroscope=(0.0, 0.0, 0.0),
        compass=90.0,
    )
    path = await w.write("imu", imu)
    assert "telemetry" in path.parts
    assert path.suffix == ".msgpack"
    payload = msgpack.unpackb(path.read_bytes(), raw=False)
    assert payload["frame_id"] == 2
    assert payload["compass"] == 90.0


async def test_write_telemetry_msgpack_requires_msgpack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "msgpack", None)  # 强制 import msgpack 抛 ImportError
    w = DiskDataWriterImpl(run_id="run1", base_dir=tmp_path, telemetry_format="msgpack")
    imu = ImuFrame(
        timestamp=1.0,
        frame_id=2,
        sensor_id="imu",
        accelerometer=(1.0, 2.0, 3.0),
        gyroscope=(0.0, 0.0, 0.0),
        compass=90.0,
    )
    with pytest.raises(DataWriteError, match="msgpack"):
        await w.write("imu", imu)
