"""数据加载器（PROMPT-ENG-006-A）。

从 ROS Bag / TimescaleDB / PostgreSQL / MinIO 等数据源加载历史数据，
供 ReplayEngine 回放与 SceneReconstructor 场景重建使用。

支持的数据类型（设计文档 §8.2 回放数据源表）：
- 车辆轨迹：TimescaleDB / ROS Bag（/localization/odom）
- 感知结果：ROS Bag（/perception/objects, DetectedObjectArray）
- 规划轨迹：ROS Bag（/planning/trajectory, Trajectory）
- 控制指令：ROS Bag（/chassis/command, ChassisCommand）
- 事件数据：PostgreSQL（scene_events 表）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 实车话题约定（与 ROS2_TOPIC_MAP 命名风格一致）
_ODOM_TOPIC = "/localization/odom"
_PERCEPTION_TOPIC = "/perception/objects"
_PLANNING_TOPIC = "/planning/trajectory"
_CHASSIS_COMMAND_TOPIC = "/chassis/command"


class DataLoader:
    """历史数据加载器。

    轨迹数据源：
    - ROS Bag（mcap/sqlite3 格式）
    - 本地 JSON 轨迹文件
    - TimescaleDB / PostgreSQL（见 load_trajectory_from_db / load_events_from_db）

    Args:
        data_source: 轨迹数据源类型 ("rosbag" | "json")。
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

    def _open_rosbag_reader(self, path: Path) -> Any:
        """打开 ROS Bag 顺序读取器（惰性导入，非 ROS 环境转 ConfigurationError）。"""
        try:
            from rosbag2_py import ConverterOptions, SequentialReader, StorageOptions  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(
                "DataLoader._open_rosbag_reader",
                f"rosbag2_py not available: {exc}",
            ) from exc
        reader = SequentialReader()
        reader.open(StorageOptions(uri=str(path), storage_id="mcap"), ConverterOptions("", ""))
        return reader

    def _load_from_rosbag(
        self, path: Path, start_ts: float, end_ts: float
    ) -> list[dict[str, Any]]:
        """从 ROS Bag 加载 /localization/odom 话题数据。"""
        reader = self._open_rosbag_reader(path)
        try:
            from rclpy.serialization import deserialize_message  # noqa: PLC0415
            from nav_msgs.msg import Odometry  # noqa: PLC0415
        except ImportError as exc:
            reader.close_buffer()
            raise ConfigurationError(
                "DataLoader._load_from_rosbag",
                f"rclpy/nav_msgs not available: {exc}",
            ) from exc

        frames: list[dict[str, Any]] = []
        try:
            while reader.has_next():
                topic, data, timestamp_ns = reader.read_next()
                if topic != _ODOM_TOPIC:
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

    # ─── 感知结果 / 规划轨迹 / 控制指令加载（文档 §8.2） ──────────────────

    def load_perception_frames(
        self, path: Path, start_ts: float = 0.0, end_ts: float = 0.0
    ) -> list[dict[str, Any]]:
        """从 ROS Bag 加载感知结果（DetectedObjectArray → /perception/objects）。

        Args:
            path: ROS Bag 目录路径。
            start_ts: 起始时间戳（Unix 秒），0 表示不限制。
            end_ts: 结束时间戳，0 表示不限制。

        Returns:
            感知帧列表，每帧含 timestamp 与 objects（字段对齐文档 §4.4.3）。
        """
        reader = self._open_rosbag_reader(path)
        try:
            from rclpy.serialization import deserialize_message  # noqa: PLC0415
            from custom_msgs.msg import DetectedObjectArray  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(
                "DataLoader.load_perception_frames",
                f"rclpy/custom_msgs not available: {exc}",
            ) from exc

        frames: list[dict[str, Any]] = []
        try:
            while reader.has_next():
                topic, data, timestamp_ns = reader.read_next()
                if topic != _PERCEPTION_TOPIC:
                    continue
                ts = timestamp_ns / 1e9
                if start_ts > 0 and ts < start_ts:
                    continue
                if end_ts > 0 and ts > end_ts:
                    break
                msg = deserialize_message(data, DetectedObjectArray)
                frames.append({
                    "timestamp": ts,
                    "objects": [
                        {
                            "id": int(o.id),
                            "type": str(o.type),
                            "x": float(o.x), "y": float(o.y), "z": float(o.z),
                            "heading": float(o.heading),
                            "length": float(o.length), "width": float(o.width),
                            "vx": float(o.vx), "vy": float(o.vy),
                        }
                        for o in msg.objects
                    ],
                })
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError("DataLoader.load_perception_frames", str(exc)) from exc
        finally:
            reader.close_buffer()
        logger.info(f"Loaded {len(frames)} perception frames from {path}")
        return frames

    def load_planned_trajectory(
        self, path: Path, start_ts: float = 0.0, end_ts: float = 0.0
    ) -> list[dict[str, Any]]:
        """从 ROS Bag 加载规划轨迹（Trajectory → /planning/trajectory）。"""
        reader = self._open_rosbag_reader(path)
        try:
            from rclpy.serialization import deserialize_message  # noqa: PLC0415
            from custom_msgs.msg import Trajectory  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(
                "DataLoader.load_planned_trajectory",
                f"rclpy/custom_msgs not available: {exc}",
            ) from exc

        frames: list[dict[str, Any]] = []
        try:
            while reader.has_next():
                topic, data, timestamp_ns = reader.read_next()
                if topic != _PLANNING_TOPIC:
                    continue
                ts = timestamp_ns / 1e9
                if start_ts > 0 and ts < start_ts:
                    continue
                if end_ts > 0 and ts > end_ts:
                    break
                msg = deserialize_message(data, Trajectory)
                frames.append({
                    "timestamp": ts,
                    "points": [
                        {"x": float(p.x), "y": float(p.y), "yaw": float(p.yaw), "v": float(p.v)}
                        for p in msg.points
                    ],
                })
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError("DataLoader.load_planned_trajectory", str(exc)) from exc
        finally:
            reader.close_buffer()
        logger.info(f"Loaded {len(frames)} planned trajectory frames from {path}")
        return frames

    def load_control_commands(
        self, path: Path, start_ts: float = 0.0, end_ts: float = 0.0
    ) -> list[dict[str, Any]]:
        """从 ROS Bag 加载控制指令（ChassisCommand → /chassis/command）。"""
        reader = self._open_rosbag_reader(path)
        try:
            from rclpy.serialization import deserialize_message  # noqa: PLC0415
            from custom_msgs.msg import ChassisCommand  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(
                "DataLoader.load_control_commands",
                f"rclpy/custom_msgs not available: {exc}",
            ) from exc

        frames: list[dict[str, Any]] = []
        try:
            while reader.has_next():
                topic, data, timestamp_ns = reader.read_next()
                if topic != _CHASSIS_COMMAND_TOPIC:
                    continue
                ts = timestamp_ns / 1e9
                if start_ts > 0 and ts < start_ts:
                    continue
                if end_ts > 0 and ts > end_ts:
                    break
                msg = deserialize_message(data, ChassisCommand)
                frames.append({
                    "timestamp": ts,
                    "throttle": float(msg.throttle),
                    "brake": float(msg.brake),
                    "steer": float(msg.steer),
                    "target_speed": float(msg.target_speed),
                })
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError("DataLoader.load_control_commands", str(exc)) from exc
        finally:
            reader.close_buffer()
        logger.info(f"Loaded {len(frames)} control command frames from {path}")
        return frames

    # ─── TimescaleDB / PostgreSQL 数据源（文档 §8.2） ────────────────────

    @staticmethod
    def load_trajectory_from_db(
        dsn: str,
        vehicle_id: str,
        start_ts: float,
        end_ts: float,
        table: str = "vehicle_telemetry",
    ) -> list[dict[str, Any]]:
        """从 TimescaleDB 加载车辆轨迹时间序列。

        Args:
            dsn: PostgreSQL 兼容连接串（psycopg2 DSN）。
            vehicle_id: 车辆 ID。
            start_ts: 起始时间戳（Unix 秒）。
            end_ts: 结束时间戳（Unix 秒）。
            table: 遥测数据表名。

        Returns:
            轨迹帧列表（与 load_trajectory 格式一致）。

        Raises:
            ConfigurationError: 数据库驱动不可用或查询失败。
        """
        try:
            import psycopg2  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(
                "DataLoader.load_trajectory_from_db",
                f"psycopg2 not available: {exc}",
            ) from exc

        sql = (
            f"SELECT ts, x, y, z, yaw, speed FROM {table} "
            "WHERE vehicle_id = %s AND ts >= %s AND ts <= %s ORDER BY ts"
        )
        frames: list[dict[str, Any]] = []
        try:
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(sql, (vehicle_id, start_ts, end_ts))
                for ts, x, y, z, yaw, speed in cur.fetchall():
                    frames.append({
                        "timestamp": float(ts),
                        "position": {"x": float(x), "y": float(y), "z": float(z)},
                        "rotation": {"yaw": float(yaw), "pitch": 0.0},
                        "velocity": {"vx": 0.0, "vy": 0.0, "speed": float(speed)},
                    })
        except Exception as exc:
            raise ConfigurationError("DataLoader.load_trajectory_from_db", str(exc)) from exc
        logger.info(f"Loaded {len(frames)} trajectory frames from DB: {table}")
        return frames

    @staticmethod
    def load_events_from_db(
        dsn: str,
        vehicle_id: str,
        start_ts: float,
        end_ts: float,
        table: str = "scene_events",
    ) -> list[dict[str, Any]]:
        """从 PostgreSQL 加载事件数据列表（文档 §8.2 事件数据源）。"""
        try:
            import psycopg2  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(
                "DataLoader.load_events_from_db",
                f"psycopg2 not available: {exc}",
            ) from exc

        sql = (
            f"SELECT ts, event_type, description FROM {table} "
            "WHERE vehicle_id = %s AND ts >= %s AND ts <= %s ORDER BY ts"
        )
        events: list[dict[str, Any]] = []
        try:
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(sql, (vehicle_id, start_ts, end_ts))
                for ts, event_type, description in cur.fetchall():
                    events.append({
                        "timestamp": float(ts),
                        "event_type": str(event_type),
                        "description": str(description),
                    })
        except Exception as exc:
            raise ConfigurationError("DataLoader.load_events_from_db", str(exc)) from exc
        logger.info(f"Loaded {len(events)} events from DB: {table}")
        return events


def _quat_to_yaw(qz: float, qw: float) -> float:
    """将四元数 (z, w) 转为 yaw 角（度）。"""
    import math
    return math.degrees(math.atan2(2.0 * (qw * qz), 1.0 - 2.0 * qz * qz))
