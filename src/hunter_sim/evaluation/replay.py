"""模块 4.3：数据回放（读取 L2 writer 输出目录）。

与 :mod:`hunter_sim.acquisition.writer` 对称：writer 写 → replayer 读。
支持 ``.npy``（数组）、``.pcd``（ASCII 点云）、``.json``（遥测/元数据）、``.h5``/``.hdf5``
（HDF5 点云，需 ``h5py``）与 ``.msgpack``（遥测，需 ``msgpack``）。
所有磁盘 IO 经 ``asyncio.to_thread`` 移出事件循环。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from hunter_sim.core.exceptions import DataReplayError
from hunter_sim.evaluation.models import ReplayFrame

_FRAME_EXTS = {".npy", ".pcd", ".json", ".h5", ".hdf5", ".msgpack"}
_METADATA_FILE = "metadata.json"
#: 与 :mod:`hunter_sim.acquisition.writer` 对称的 HDF5 点云数据集名。
_HDF5_POINT_DATASET = "points"


class ReplayerImpl:
    """满足 :class:`~hunter_sim.evaluation.protocols.Replayer` 契约。"""

    def load_metadata(self, run_root: Path) -> dict[str, Any]:
        """读取 ``metadata.json``，缺失时抛 :class:`DataReplayError`。"""
        path = run_root / _METADATA_FILE
        if not path.is_file():
            raise DataReplayError(f"元数据文件不存在: {path}")
        metadata: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return metadata

    def list_sensors(self, run_root: Path) -> list[str]:
        """列出含帧数据的传感器目录相对路径（不含根目录 metadata）。"""
        if not run_root.is_dir():
            raise DataReplayError(f"运行目录不存在: {run_root}")
        result: set[str] = set()
        for p in run_root.rglob("*"):
            if not p.is_file() or p.suffix not in _FRAME_EXTS:
                continue
            rel = p.parent.relative_to(run_root)
            if str(rel) == ".":
                continue
            key = str(rel).replace("\\", "/")
            result.add(key)
        return sorted(result)

    async def iter_frames(self, run_root: Path, sensor_rel: str) -> AsyncIterator[ReplayFrame]:
        """按 frame_id 升序迭代指定传感器的回放帧（IO 异步）。"""
        d = run_root / sensor_rel
        if not d.is_dir():
            raise DataReplayError(f"传感器目录不存在: {d}")
        files = sorted(
            [p for p in d.iterdir() if p.is_file() and p.suffix in _FRAME_EXTS],
            key=lambda p: p.stem,
        )
        for p in files:
            try:
                frame_id = int(p.stem)
            except ValueError:
                continue
            payload = await asyncio.to_thread(_load_file, p)
            yield ReplayFrame(frame_id=frame_id, path=p, payload=payload)


def _load_file(path: Path) -> Any:
    """按扩展名加载文件内容。"""
    if path.suffix == ".npy":
        return np.load(path, allow_pickle=False)
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix == ".pcd":
        return _parse_pcd(path)
    if path.suffix in (".h5", ".hdf5"):
        return _load_hdf5(path)
    if path.suffix == ".msgpack":
        return _load_msgpack(path)
    raise DataReplayError(f"不支持的文件格式: {path.suffix}")


def _load_hdf5(path: Path) -> NDArray[np.float64]:
    """读取内存 HDF5 中的点云数据集（惰性依赖 h5py）。"""
    try:
        import h5py
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise DataReplayError(f"读取 hdf5 需要 h5py: {exc}") from exc
    with h5py.File(path, "r") as handle:
        if _HDF5_POINT_DATASET not in handle:
            raise DataReplayError(f"HDF5 缺少数据集 {_HDF5_POINT_DATASET}: {path}")
        points: NDArray[np.float64] = handle[_HDF5_POINT_DATASET][:].astype(np.float64)
        return points


def _load_msgpack(path: Path) -> dict[str, Any]:
    """读取 msgpack 遥测帧为字典（惰性依赖 msgpack）。"""
    try:
        import msgpack
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise DataReplayError(f"读取 msgpack 需要 msgpack: {exc}") from exc
    unpacked: dict[str, Any] = msgpack.unpackb(path.read_bytes(), raw=False)
    return unpacked


def _parse_pcd(path: Path) -> NDArray[np.float64]:
    """解析 ASCII PCD 文件为 ``(N, num_fields)`` float64 数组。"""
    lines = path.read_text(encoding="ascii").splitlines()
    # 头部固定 10 行（见 writer.py _encode_pointcloud）
    if len(lines) < 10:
        raise DataReplayError(f"PCD 文件头不完整: {path}")
    data_lines = lines[10:]
    rows = [[float(v) for v in line.split()] for line in data_lines if line.strip()]
    if not rows:
        return np.empty((0, 4), dtype=np.float64)
    return np.array(rows, dtype=np.float64)


__all__ = ["ReplayerImpl"]
