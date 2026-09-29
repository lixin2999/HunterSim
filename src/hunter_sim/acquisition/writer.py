"""模块 2.3：数据持久化写入器。

- 按类型分派编码：相机→图像(``npy``/``png``)、激光雷达/毫米波→数组(``pcd``/``npy``/
  ``hdf5``)、IMU/GNSS/车辆状态→时序(``json``/``msgpack``)。
- 阻塞 IO 经 ``asyncio.to_thread`` 移出事件循环；**原子写**（临时文件 + ``os.replace``）；
  ``OSError`` 有界重试。
- 目录结构：``<base_dir>/<run_id>/<sensor_id>/<frame_id>.<ext>``，元数据落 ``metadata.json``。

默认编码仅依赖 numpy（免安装额外库即可验证）；PNG/JPEG 需 ``pillow``，HDF5 需 ``h5py``、
msgpack 需 ``msgpack``（均属可选 ``storage``/``imaging`` extras，缺失时在编码点抛
:class:`DataWriteError` 优雅降级）。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from hunter_sim.core.contracts import (
    CameraFrame,
    GnssFrame,
    ImuFrame,
    LidarFrame,
    RadarFrame,
    VehicleState,
)
from hunter_sim.core.exceptions import DataWriteError
from hunter_sim.core.logging import logger

#: HDF5 内点云数据集名（与 :mod:`hunter_sim.evaluation.replay` 对称）。
HDF5_POINT_DATASET = "points"


class DiskDataWriterImpl:
    """满足 :class:`~hunter_sim.acquisition.protocols.DataWriter` 契约。"""

    def __init__(
        self,
        *,
        run_id: str,
        base_dir: str | os.PathLike[str],
        image_format: str = "npy",
        pointcloud_format: str = "pcd",
        telemetry_format: str = "json",
        max_retries: int = 3,
        retry_delay: float = 0.05,
    ) -> None:
        """初始化写入器。

        Args:
            run_id: 采集运行 ID（作为一级目录）。
            base_dir: 输出根目录。
            image_format: 相机编码，``npy`` 或 ``png``/``jpeg``（后者需 pillow）。
            pointcloud_format: 点云编码，``pcd`` / ``npy`` / ``hdf5``（后者需 h5py）。
            telemetry_format: 遥测编码，``json`` 或 ``msgpack``（后者需 msgpack）。
            max_retries: 落盘 IO 失败重试次数。
            retry_delay: 重试基础间隔秒数（线性退避）。
        """
        self._run_id = run_id
        self._root = Path(base_dir) / run_id
        self._image_format = image_format
        self._pointcloud_format = pointcloud_format
        self._telemetry_format = telemetry_format
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._closed = False

    @property
    def root(self) -> Path:
        """本次运行的输出根目录。"""
        return self._root

    async def write(self, sensor_id: str, item: Any) -> Path:
        """写入单帧数据，返回落盘路径。"""
        if self._closed:
            raise DataWriteError("写入器已关闭")
        path, data = self._encode(sensor_id, item)
        await self._atomic_write(path, data)
        return path

    async def write_batch(self, sensor_id: str, items: list[Any]) -> list[Path]:
        """批量写入多帧，逐帧落盘并返回路径列表。"""
        return [await self.write(sensor_id, item) for item in items]

    async def write_metadata(self, payload: dict[str, Any]) -> Path:
        """写入运行元数据到 ``metadata.json``。"""
        path = self._root / "metadata.json"
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        await self._atomic_write(path, data)
        return path

    async def flush(self) -> None:
        """无操作占位：当前每帧即时落盘，无内部缓冲队列。"""
        return None

    async def close(self) -> None:
        """关闭写入器（幂等）。"""
        self._closed = True

    # -- 编码分派 ---------------------------------------------------------

    def _encode(self, sensor_id: str, item: Any) -> tuple[Path, bytes]:
        """将数据对象编码为 ``(目标路径, 字节)``（纯计算，无 IO）。"""
        if isinstance(item, CameraFrame):
            return self._sensor_path(
                sensor_id, item.frame_id, self._image_ext()
            ), self._encode_image(item.image)
        if isinstance(item, LidarFrame):
            return self._sensor_path(
                sensor_id, item.frame_id, self._pcd_ext()
            ), self._encode_pointcloud_dispatch(item.points)
        if isinstance(item, RadarFrame):
            # 毫米波检测矩阵按数组落盘
            return self._sensor_path(sensor_id, item.frame_id, "npy"), self._encode_npy(
                item.detections
            )
        if isinstance(item, ImuFrame | GnssFrame | VehicleState):
            return self._telemetry_path(sensor_id, item.frame_id), self._encode_telemetry(item)
        raise DataWriteError(f"不支持的数据类型: {type(item).__name__}")

    def _image_ext(self) -> str:
        return "npy" if self._image_format == "npy" else "png"

    def _pcd_ext(self) -> str:
        return "h5" if self._pointcloud_format == "hdf5" else self._pointcloud_format

    def _telemetry_ext(self) -> str:
        return "msgpack" if self._telemetry_format == "msgpack" else "json"

    def _sensor_path(self, sensor_id: str, frame_id: int, ext: str) -> Path:
        return self._root / sensor_id / f"{frame_id:08d}.{ext}"

    def _telemetry_path(self, sensor_id: str, frame_id: int) -> Path:
        return self._root / "telemetry" / sensor_id / f"{frame_id:08d}.{self._telemetry_ext()}"

    @staticmethod
    def _encode_npy(array: np.ndarray) -> bytes:
        buf = io.BytesIO()
        np.save(buf, array, allow_pickle=False)
        return buf.getvalue()

    def _encode_image(self, array: np.ndarray) -> bytes:
        if self._image_format == "npy":
            return self._encode_npy(array)
        return self._encode_png(array)

    @staticmethod
    def _encode_png(array: np.ndarray) -> bytes:
        """PNG 编码（惰性依赖 pillow）。"""
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise DataWriteError(f"图像格式 png 需要 pillow: {exc}") from exc
        image = Image.fromarray(array.astype("uint8"))
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def _encode_pointcloud(array: np.ndarray) -> bytes:
        """ASCII PCD 编码（首 4 列视为 x y z intensity）。"""
        if array.ndim != 2 or array.shape[1] < 3:
            raise DataWriteError(f"非法点云形状: {array.shape}")
        n = array.shape[0]
        fields = ["x", "y", "z", "intensity"][: min(array.shape[1], 4)]
        header = [
            "VERSION .7",
            f"FIELDS {' '.join(fields)}",
            "SIZE " + " ".join(["4"] * len(fields)),
            "TYPE " + " ".join(["F"] * len(fields)),
            "COUNT " + " ".join(["1"] * len(fields)),
            f"WIDTH {n}",
            "HEIGHT 1",
            "VIEWPOINT 0 0 0 1 0 0 0",
            f"POINTS {n}",
            "DATA ascii",
        ]
        cols = array[:, : len(fields)]
        lines = (
            [" ".join(f"{value:.6f}" for value in row) for row in cols.reshape(-1, len(fields))]
            if n
            else []
        )
        text = "\n".join(header + lines) + "\n"
        return text.encode("ascii")

    def _encode_pointcloud_dispatch(self, array: np.ndarray) -> bytes:
        if self._pointcloud_format == "npy":
            return self._encode_npy(array)
        if self._pointcloud_format == "hdf5":
            return self._encode_pointcloud_hdf5(array)
        return self._encode_pointcloud(array)

    def _encode_pointcloud_hdf5(self, array: np.ndarray) -> bytes:
        """将点云写入内存 HDF5（惰性依赖 h5py，数据集名 ``HDF5_POINT_DATASET``）。"""
        try:
            import h5py
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise DataWriteError(f"点云格式 hdf5 需要 h5py: {exc}") from exc
        if array.ndim != 2 or array.shape[1] < 3:
            raise DataWriteError(f"非法点云形状: {array.shape}")
        buf = io.BytesIO()
        with h5py.File(buf, "w") as handle:
            handle.create_dataset(HDF5_POINT_DATASET, data=array)
        return buf.getvalue()

    def _encode_telemetry(self, item: Any) -> bytes:
        """时序遥测编码：按 ``telemetry_format`` 分派 ``json`` / ``msgpack``。"""
        payload = item.model_dump(mode="json")
        if self._telemetry_format == "msgpack":
            return self._encode_msgpack(payload)
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _encode_msgpack(payload: dict[str, Any]) -> bytes:
        """MessagePack 编码（惰性依赖 msgpack）。"""
        try:
            import msgpack
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise DataWriteError(f"遥测格式 msgpack 需要 msgpack: {exc}") from exc
        packed: bytes = msgpack.packb(payload, use_bin_type=True)
        return packed

    # -- 原子落盘 ---------------------------------------------------------

    async def _atomic_write(self, path: Path, data: bytes) -> None:
        """在子线程内原子写文件，``OSError`` 线性退避重试。"""
        for attempt in range(1, self._max_retries + 1):
            try:
                await asyncio.to_thread(_write_bytes_atomic, path, data)
                return
            except OSError as exc:
                if attempt >= self._max_retries:
                    raise DataWriteError(f"落盘失败 {path}: {exc}") from exc
                logger.bind(component="writer", path=str(path)).warning(
                    "落盘第 {} 次失败，重试: {}", attempt, exc
                )
                await asyncio.sleep(self._retry_delay * attempt)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """同步原子写：写临时文件后 ``os.replace`` 原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


__all__ = ["DiskDataWriterImpl"]
