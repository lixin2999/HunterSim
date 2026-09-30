"""ROS2 Bag 数据录制器（PROMPT-ENG-004-B）。

封装 ros2 bag record 命令，在 VIL/SIL 运行期间录制仿真传感器话题数据，
供后续回放分析和孪生重建使用。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from hunter_sim.common.exceptions import SensorSimulationError
from hunter_sim.common.models import ROS2_TOPIC_MAP
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

_DEFAULT_OUTPUT_DIR = Path("data/rosbags")


class DataRecorder:
    """ros2 bag record 封装管理器。

    在后台进程中调用 ``ros2 bag record`` 命令，录制所有仿真传感器话题。

    Args:
        output_dir: rosbag 输出目录。
        topics: 要录制的话题列表（默认使用全部实车话题映射表）。
        bag_format: rosbag2 存储格式（sqlite3 / mcap）。
    """

    def __init__(
        self,
        output_dir: Path = _DEFAULT_OUTPUT_DIR,
        topics: Optional[list[str]] = None,
        bag_format: str = "mcap",
    ) -> None:
        self._output_dir = output_dir
        self._topics: list[str] = topics or [m.real_topic for m in ROS2_TOPIC_MAP]
        self._bag_format = bag_format
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._lock: threading.Lock = threading.Lock()
        self._recording: bool = False
        self._start_time: float = 0.0
        self._session_id: str = ""

    def start(self, session_id: str = "") -> None:
        """启动 rosbag 录制后台进程。

        Args:
            session_id: 录制会话 ID（用于输出目录命名，空则自动生成时间戳）。
        """
        with self._lock:
            if self._recording:
                logger.warning("DataRecorder already recording, stop() first")
                return

            self._session_id = session_id or f"rec_{int(time.time())}"
            bag_path = self._output_dir / self._session_id
            bag_path.mkdir(parents=True, exist_ok=True)

            cmd: list[str] = [
                "ros2", "bag", "record",
                "-o", str(bag_path),
                "--storage", self._bag_format,
                *self._topics,
            ]

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self._recording = True
                self._start_time = time.perf_counter()
                logger.info(
                    f"rosbag recording started: session={self._session_id}, "
                    f"topics={self._topics}"
                )
            except FileNotFoundError as exc:
                raise SensorSimulationError(
                    "recorder", "start",
                    "ros2 command not found. Ensure ROS2 Humble is sourced.",
                ) from exc

    def stop(self, timeout_s: float = 5.0) -> Optional[Path]:
        """停止录制并返回输出目录路径。

        Args:
            timeout_s: 等待进程退出超时时间（秒）。

        Returns:
            rosbag 输出目录 Path，未录制时返回 None。
        """
        with self._lock:
            if not self._recording or self._process is None:
                return None

            duration = time.perf_counter() - self._start_time
            self._process.terminate()
            try:
                self._process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self._process.kill()
                logger.warning("rosbag record process killed after timeout")

            self._recording = False
            self._process = None
            bag_path = self._output_dir / self._session_id
            logger.info(
                f"rosbag recording stopped: session={self._session_id}, "
                f"duration={duration:.1f}s, output={bag_path}"
            )
            return bag_path

    @property
    def is_recording(self) -> bool:
        """是否正在录制。"""
        return self._recording

    @property
    def recording_duration_s(self) -> float:
        """当前/最后录制时长（秒）。"""
        if self._recording:
            return time.perf_counter() - self._start_time
        return 0.0
