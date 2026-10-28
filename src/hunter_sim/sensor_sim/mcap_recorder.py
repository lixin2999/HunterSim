"""MCAP 传感器录制器（V3.0.0）。

把场景运行期间的传感器帧落成 ``.mcap`` 文件，供云端 MinIO 归档、离线回放与
孪生重建使用。相比已删除的 ``DataRecorder``（``ros2 bag record`` 子进程），
本实现：

- **不依赖 ROS2 运行时**：``mcap`` + ``mcap_ros2.writer`` 均为纯 Python，
  消息体用「鸭子类型」轻量对象构造，由 ``mcap_ros2`` 按 msgdef 动态编 CDR；
- **进程内直写**：无子进程、无管道、无会话目录，退出即得单文件；
- **产出真实统计**：``RecordStats`` 的时长/帧数/主题/字节数用于填充
  ``sensor_file`` 消息（旧实现这三项恒为 0）。

主题与 schema：

    /carla/lidar_points               sensor_msgs/msg/PointCloud2
    /carla/camera/color/image_raw     sensor_msgs/msg/Image
    /carla/camera/depth/image_rect_raw sensor_msgs/msg/Image
    /carla/imu/data                   sensor_msgs/msg/Imu

依赖缺失或写入异常时降级为 no-op 并记 warning，绝不阻断场景运行
（对齐项目「可选依赖惰性导入降级」规范）。
"""

from __future__ import annotations

import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from hunter_sim.common.metrics import SENSOR_RECORD_BYTES_TOTAL
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# ─── 主题常量（与 sensor_sim/frame_hub.py 的 stream 一一对应） ────────────────

TOPIC_LIDAR = "/carla/lidar_points"
TOPIC_RGB = "/carla/camera/color/image_raw"
TOPIC_DEPTH = "/carla/camera/depth/image_rect_raw"
TOPIC_IMU = "/carla/imu/data"

STREAM_TO_TOPIC: dict[str, str] = {
    "lidar": TOPIC_LIDAR,
    "rgb": TOPIC_RGB,
    "depth": TOPIC_DEPTH,
    "imu": TOPIC_IMU,
}

# ─── ROS2 msgdef（mcap_ros2 用其动态生成 CDR 编解码器） ──────────────────────

_SEP = "=" * 80

_HEADER_DEPS = (
    f"{_SEP}\n"
    "MSG: std_msgs/Header\n"
    "builtin_interfaces/Time stamp\n"
    "string frame_id\n"
    f"{_SEP}\n"
    "MSG: builtin_interfaces/Time\n"
    "int32 sec\n"
    "uint32 nanosec\n"
)

IMAGE_MSGDEF = (
    "std_msgs/Header header\n"
    "uint32 height\n"
    "uint32 width\n"
    "string encoding\n"
    "uint8 is_bigendian\n"
    "uint32 step\n"
    "uint8[] data\n" + _HEADER_DEPS
)

POINTCLOUD2_MSGDEF = (
    "std_msgs/Header header\n"
    "uint32 height\n"
    "uint32 width\n"
    "PointField[] fields\n"
    "bool is_bigendian\n"
    "uint32 point_step\n"
    "uint32 row_step\n"
    "uint8[] data\n"
    "bool is_dense\n"
    f"{_SEP}\n"
    "MSG: sensor_msgs/PointField\n"
    "string name\n"
    "uint32 offset\n"
    "uint8 datatype\n"
    "uint32 count\n" + _HEADER_DEPS
)

IMU_MSGDEF = (
    "std_msgs/Header header\n"
    "geometry_msgs/Quaternion orientation\n"
    "float64[9] orientation_covariance\n"
    "geometry_msgs/Vector3 angular_velocity\n"
    "float64[9] angular_velocity_covariance\n"
    "geometry_msgs/Vector3 linear_acceleration\n"
    "float64[9] linear_acceleration_covariance\n"
    f"{_SEP}\n"
    "MSG: geometry_msgs/Quaternion\n"
    "float64 x\n"
    "float64 y\n"
    "float64 z\n"
    "float64 w\n"
    f"{_SEP}\n"
    "MSG: geometry_msgs/Vector3\n"
    "float64 x\n"
    "float64 y\n"
    "float64 z\n" + _HEADER_DEPS
)

# 主题 → msgdef（schema 名由主题决定）
TOPIC_SCHEMAS: dict[str, tuple[str, str]] = {
    TOPIC_LIDAR: ("sensor_msgs/msg/PointCloud2", POINTCLOUD2_MSGDEF),
    TOPIC_RGB: ("sensor_msgs/msg/Image", IMAGE_MSGDEF),
    TOPIC_DEPTH: ("sensor_msgs/msg/Image", IMAGE_MSGDEF),
    TOPIC_IMU: ("sensor_msgs/msg/Imu", IMU_MSGDEF),
}

# PointField datatype：FLOAT32 = 7（sensor_msgs/PointField 常量）
_POINT_FIELD_FLOAT32 = 7


@dataclass
class RecordStats:
    """一次录制的统计结果（填充 ``sensor_file`` 消息）。

    Attributes:
        start_time: 首个消息写入时刻（Unix 秒）。
        end_time: 录制结束时刻（Unix 秒）。
        frame_count: 写入的消息总数。
        topics: 实际写入的主题名元组。
        size_bytes: 文件大小（字节）。
        path: 录制文件路径（为空表示未生成文件）。
    """

    start_time: float = 0.0
    end_time: float = 0.0
    frame_count: int = 0
    topics: tuple[str, ...] = field(default_factory=tuple)
    size_bytes: int = 0
    path: str = ""

    @property
    def duration_s(self) -> float:
        """录制时长（秒）。"""
        return max(0.0, self.end_time - self.start_time)


def _stamp_ns(log_time_ns: int) -> types.SimpleNamespace:
    """把纳秒时间戳拆成 ``builtin_interfaces/Time``。"""
    return types.SimpleNamespace(sec=log_time_ns // 1_000_000_000, nanosec=log_time_ns % 1_000_000_000)


def _header(frame_id: str, log_time_ns: int) -> types.SimpleNamespace:
    """构造 ``std_msgs/Header``。"""
    return types.SimpleNamespace(stamp=_stamp_ns(log_time_ns), frame_id=frame_id)


def build_image_message(
    data: bytes,
    width: int,
    height: int,
    encoding: str,
    frame_id: str,
    log_time_ns: int,
) -> types.SimpleNamespace:
    """构造 ``sensor_msgs/Image`` 轻量消息（无需 ROS2 运行时）。

    Args:
        data: 像素字节。
        width: 图像宽度。
        height: 图像高度。
        encoding: 编码（``bgra8`` / ``rgb8`` / ``32FC1``）。
        frame_id: 坐标系 ID。
        log_time_ns: 时间戳（纳秒）。

    Returns:
        可被 ``mcap_ros2.Writer`` 序列化的消息对象。
    """
    bytes_per_pixel = 4 if encoding in ("bgra8", "32FC1", "rgba8") else 3
    return types.SimpleNamespace(
        header=_header(frame_id, log_time_ns),
        height=height,
        width=width,
        encoding=encoding,
        is_bigendian=0,
        step=width * bytes_per_pixel,
        data=data,
    )


def build_pointcloud2_message(
    data: bytes,
    point_count: int,
    frame_id: str,
    log_time_ns: int,
) -> types.SimpleNamespace:
    """构造 ``sensor_msgs/PointCloud2``（xyz + intensity，每点 16 字节）。"""
    fields = [
        types.SimpleNamespace(name="x", offset=0, datatype=_POINT_FIELD_FLOAT32, count=1),
        types.SimpleNamespace(name="y", offset=4, datatype=_POINT_FIELD_FLOAT32, count=1),
        types.SimpleNamespace(name="z", offset=8, datatype=_POINT_FIELD_FLOAT32, count=1),
        types.SimpleNamespace(
            name="intensity", offset=12, datatype=_POINT_FIELD_FLOAT32, count=1
        ),
    ]
    point_step = 16
    return types.SimpleNamespace(
        header=_header(frame_id, log_time_ns),
        height=1,
        width=point_count,
        fields=fields,
        is_bigendian=False,
        point_step=point_step,
        row_step=point_count * point_step,
        data=data,
        is_dense=True,
    )


def build_imu_message(
    accel: tuple[float, float, float],
    gyro: tuple[float, float, float],
    frame_id: str,
    log_time_ns: int,
) -> types.SimpleNamespace:
    """构造 ``sensor_msgs/Imu``（协方差留 0，与 CARLA 输出一致）。"""
    zero9 = [0.0] * 9
    return types.SimpleNamespace(
        header=_header(frame_id, log_time_ns),
        orientation=types.SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
        orientation_covariance=zero9,
        angular_velocity=types.SimpleNamespace(x=gyro[0], y=gyro[1], z=gyro[2]),
        angular_velocity_covariance=zero9,
        linear_acceleration=types.SimpleNamespace(x=accel[0], y=accel[1], z=accel[2]),
        linear_acceleration_covariance=zero9,
    )


class McapRecorder:
    """MCAP 录制器（单实例/单场景，线程安全）。

    Args:
        compress: 是否启用 zstd 分块压缩（``mcap`` 内置，无需额外依赖）。

    Notes:
        ``mcap`` / ``mcap_ros2`` 缺失时构造成功但 ``open()`` 返回 False，
        所有 ``push()`` 为 no-op —— 调用方无需额外判空。
    """

    def __init__(self, compress: bool = True) -> None:
        self._lock = threading.Lock()
        self._compress = compress
        self._writer: Any = None
        self._handles: Any = None
        self._path: Optional[Path] = None
        self._schemas: dict[str, Any] = {}
        self._topics_written: set[str] = set()
        self._frame_count = 0
        self._bytes = 0
        self._start_time = 0.0
        self._end_time = 0.0
        self._degraded = False

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def open(self, path: Path) -> bool:
        """打开录制文件。

        Args:
            path: 输出 ``.mcap`` 路径（父目录自动创建）。

        Returns:
            True 表示可录制；False 表示降级为 no-op。
        """
        with self._lock:
            if self._writer is not None:
                logger.warning("McapRecorder already open, ignoring re-open")
                return True
            try:
                from mcap.writer import CompressionType  # noqa: PLC0415
                from mcap_ros2.writer import Writer  # noqa: PLC0415
            except ImportError as exc:
                self._degraded = True
                logger.warning(f"mcap 依赖不可用，传感器录制降级为 no-op: {exc}")
                return False

            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("wb")
                writer = Writer(
                    handle,
                    compression=CompressionType.ZSTD if self._compress else CompressionType.NONE,
                )
            except Exception as exc:  # noqa: BLE001  磁盘/权限问题不得阻断场景
                self._degraded = True
                logger.warning(f"mcap 文件创建失败，录制降级为 no-op: {exc}")
                return False

            self._handles = handle
            self._writer = writer
            self._path = path
            self._schemas = {}
            self._topics_written = set()
            self._frame_count = 0
            self._bytes = 0
            self._start_time = 0.0
            self._end_time = 0.0
            self._degraded = False
            logger.info(f"McapRecorder opened: {path}")
            return True

    def push(self, topic: str, log_time_ns: int, message: Any) -> bool:
        """写入一条消息。

        Args:
            topic: 主题（须在 ``TOPIC_SCHEMAS`` 内）。
            log_time_ns: 记录时间（纳秒 Unix 时间戳）。
            message: 可被 CDR 序列化的消息对象。

        Returns:
            True 表示写入成功。
        """
        with self._lock:
            if self._writer is None:
                return False
            entry = TOPIC_SCHEMAS.get(topic)
            if entry is None:
                logger.debug(f"McapRecorder ignored unknown topic: {topic}")
                return False
            try:
                schema = self._schema_locked(topic, entry)
                self._writer.write_message(topic, schema, message, log_time=log_time_ns)
            except Exception as exc:  # noqa: BLE001  单帧写入失败不影响整段录制
                logger.warning(f"mcap 写入失败（topic={topic}）: {exc}")
                return False

            self._frame_count += 1
            self._topics_written.add(topic)
            changed = len(getattr(message, "data", b"") or b"")
            self._bytes += changed
            if changed:
                SENSOR_RECORD_BYTES_TOTAL.inc(changed)
            stamp = log_time_ns / 1_000_000_000
            if not self._start_time:
                self._start_time = stamp
            self._end_time = stamp
            return True

    def push_stream(self, stream: str, log_time_ns: int, message: Any) -> bool:
        """按流名写入（``rgb`` / ``depth`` / ``lidar`` / ``imu``）。"""
        topic = STREAM_TO_TOPIC.get(stream.strip().lower())
        if topic is None:
            logger.debug(f"McapRecorder ignored unknown stream: {stream}")
            return False
        return self.push(topic, log_time_ns, message)

    def close(self) -> RecordStats:
        """结束录制并返回统计（幂等）；未打开时返回空统计。"""
        with self._lock:
            if self._writer is None:
                return RecordStats()
            path = self._path
            try:
                self._writer.finish()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"mcap finish 失败: {exc}")
            try:
                if self._handles is not None:
                    self._handles.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"mcap 文件句柄关闭失败: {exc}")
            size = 0
            if path is not None:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
            stats = RecordStats(
                start_time=self._start_time,
                end_time=self._end_time,
                frame_count=self._frame_count,
                topics=tuple(sorted(self._topics_written)),
                size_bytes=size,
                path=str(path) if path is not None else "",
            )
            self._writer = None
            self._handles = None
            self._path = None
            self._schemas = {}
            logger.info(
                f"McapRecorder closed: {stats.path}, frames={stats.frame_count}, "
                f"topics={len(stats.topics)}, size={stats.size_bytes}B"
            )
            return stats

    # ── 属性 ──────────────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        """是否正在录制。"""
        with self._lock:
            return self._writer is not None

    @property
    def degraded(self) -> bool:
        """是否因依赖/磁盘问题降级为 no-op。"""
        return self._degraded

    @property
    def frame_count(self) -> int:
        """已写入消息数。"""
        with self._lock:
            return self._frame_count

    # ── 内部实现 ──────────────────────────────────────────────────────────

    def _schema_locked(self, topic: str, entry: tuple[str, str]) -> Any:
        """按主题惰性注册 schema（调用方已持锁）。"""
        cached = self._schemas.get(topic)
        if cached is not None:
            return cached
        name, msgdef = entry
        schema = self._writer.register_msgdef(name, msgdef)
        self._schemas[topic] = schema
        return schema


__all__ = [
    "IMAGE_MSGDEF",
    "IMU_MSGDEF",
    "McapRecorder",
    "POINTCLOUD2_MSGDEF",
    "RecordStats",
    "STREAM_TO_TOPIC",
    "TOPIC_DEPTH",
    "TOPIC_IMU",
    "TOPIC_LIDAR",
    "TOPIC_RGB",
    "TOPIC_SCHEMAS",
    "build_image_message",
    "build_imu_message",
    "build_pointcloud2_message",
]
