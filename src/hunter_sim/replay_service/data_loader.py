"""数据加载器（PROMPT-ENG-006-A）。

从 ROS Bag / TimescaleDB / MinIO 等数据源加载历史轨迹数据，
供 ReplayEngine 回放使用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


class DataLoader:
    """历史数据加载器。

    支持数据源：
    - ROS Bag（mcap/sqlite3 格式）
    - 本地 JSON 轨迹文件

    Args:
        data_source: 数据源类型 ("rosbag" | "json")。
    """

    def __init__(self, data_source: str = "rosbag") -> None:
        self._source = data_source

    def load_trajectory(
        self,
        path: Path,
        vehicle_id: str = "",
        start_ts: float = 0.0,
        end_ts: float = 0.0,
    ) -> list[dict[str, Any]]:
        """加载轨迹帧列表。

        Args:
            path: 数据文件或目录路径。
            vehicle_id: 过滤车辆 ID（可选）。
            start_ts: 起始时间戳（Unix 秒），0 表示不限制。
            end_ts: 结束时间戳，0 表示不限制。

        Returns:
            轨迹帧列表（每帧为 dict，含 timestamp/position/rotation/velocity 字段）。

        Raises:
            ConfigurationError: 数据源不存在或格式不支持。
        """
        if self._source == "rosbag":
            return self._load_from_rosbag(path, start_ts, end_ts)
        elif self._source == "json":
            return self._load_from_json(path)
        else:
            raise ConfigurationError("DataLoader", f"Unsupported source: {self._source}")

    def _load_from_rosbag(
        self, path: Path, start_ts: float, end_ts: float
    ) -> list[dict[str, Any]]:
        """从 ROS Bag 加载 /localization/odom 话题数据。"""
        try:
            from rosbag2_py import ConverterOptions, SequentialReader, StorageOptions  # noqa: PLC0415
            from rclpy.serialization import deserialize_message  # noqa: PLC0415
            from nav_msgs.msg import Odometry  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(
                "DataLoader._load_from_rosbag",
                f"rosbag2_py not available: {exc}",
            ) from exc

        frames: list[dict[str, Any]] = []
        try:
            reader = SequentialReader()
            storage_options = StorageOptions(uri=str(path), storage_id="mcap")
            converter_options = ConverterOptions("", "")
            reader.open(storage_options, converter_options)

            while reader.has_next():
                topic, data, timestamp_ns = reader.read_next()
                if topic != "/localization/odom":
                    continue
                ts = timestamp_ns / 1e9
                if start_ts > 0 and ts < start_ts:
                    continue
                if end_ts > 0 and ts > end_ts:
                    break
                msg = deserialize_message(data, Odometry)
                frames.append({
                    "timestamp": ts,
                    "position": {
                        "x": msg.pose.pose.position.x,
                        "y": msg.pose.pose.position.y,
                        "z": msg.pose.pose.position.z,
                    },
                    "rotation": {
                        "yaw": _quat_to_yaw(msg.pose.pose.orientation.z, msg.pose.pose.orientation.w),
                        "pitch": 0.0,
                    },
                    "velocity": {
                        "vx": msg.twist.twist.linear.x,
                        "vy": msg.twist.twist.linear.y,
                        "speed": (msg.twist.twist.linear.x ** 2 + msg.twist.twist.linear.y ** 2) ** 0.5,
                    },
                })
            reader.close_buffer()

        except Exception as exc:
            logger.error(f"ROS Bag load failed: {exc}")
            raise ConfigurationError("DataLoader._load_from_rosbag", str(exc)) from exc

        logger.info(f"Loaded {len(frames)} trajectory frames from rosbag: {path}")
        return frames

    def _load_from_json(self, path: Path) -> list[dict[str, Any]]:
        """从本地 JSON 文件加载轨迹数据。"""
        import json
        if not path.exists():
            raise ConfigurationError("DataLoader._load_from_json", f"File not found: {path}")
        data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        logger.info(f"Loaded {len(data)} frames from {path}")
        return data


def _quat_to_yaw(qz: float, qw: float) -> float:
    """将四元数 (z, w) 转为 yaw 角（度）。"""
    import math
    return math.degrees(math.atan2(2.0 * (qw * qz), 1.0 - 2.0 * qz * qz))
