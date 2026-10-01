"""数据回放引擎主控制器（PROMPT-ENG-006-A）。

ReplayEngine 管理完整回放生命周期：
数据加载 → 时间轴初始化 → 播放/暂停/变速/单步/拖拽 → 完成。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.common.models import VehicleState
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


class ReplayStatus(str, Enum):
    """回放状态。"""

    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ReplayConfig:
    """回放任务配置。

    Attributes:
        replay_id: 回放任务唯一 ID。
        vehicle_id: 目标实车 ID。
        start_timestamp: 回放起始时间（Unix 秒）。
        end_timestamp: 回放结束时间（Unix 秒）。
        map_id: 仿真地图 ID。
        time_factor: 播放速度因子 (0.25/0.5/1.0/2.0/4.0/8.0)。
        loop: 是否循环播放。
        data_source: 数据源类型字符串 ("rosbag" / "db")。
        rosbag_path: ROS Bag 文件路径（data_source="rosbag" 时必填）。
    """

    replay_id: str
    vehicle_id: str = ""
    start_timestamp: float = 0.0
    end_timestamp: float = 0.0
    map_id: str = "Town03"
    time_factor: float = 1.0
    loop: bool = False
    data_source: str = "rosbag"
    rosbag_path: Optional[Path] = None


@dataclass
class ReplayState:
    """当前回放状态快照。

    Attributes:
        status: 当前回放状态。
        current_timestamp: 当前播放时间戳（Unix 秒）。
        progress: 播放进度 (0.0 ~ 1.0)。
        time_factor: 当前速度因子。
        total_frames: 总帧数。
        current_frame: 当前帧序号。
        vehicle_state: 当前帧车辆状态（回放数据插值后）。
    """

    status: ReplayStatus
    current_timestamp: float = 0.0
    progress: float = 0.0
    time_factor: float = 1.0
    total_frames: int = 0
    current_frame: int = 0
    vehicle_state: Optional[VehicleState] = None


class ReplayEngine:
    """数据回放引擎主控制器。

    Args:
        config: 回放任务配置。
        world: CARLA World 对象。
        vehicle_actor: 回放虚拟车辆 Actor。
    """

    def __init__(
        self,
        config: ReplayConfig,
        world: Any,
        vehicle_actor: Any,
    ) -> None:
        self._config = config
        self._world = world
        self._vehicle = vehicle_actor
        self._status: ReplayStatus = ReplayStatus.IDLE
        self._trajectory: list[dict[str, Any]] = []
        self._current_index: int = 0
        self._time_offset: float = 0.0
        self._lock: threading.Lock = threading.Lock()
        self._stop_event: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        logger.info(f"ReplayEngine created: {config.replay_id}")

    @property
    def status(self) -> ReplayStatus:
        """当前回放状态。"""
        return self._status

    def get_state(self) -> ReplayState:
        """获取当前回放状态快照。"""
        with self._lock:
            total = len(self._trajectory)
            progress = self._current_index / total if total > 0 else 0.0
            current_ts = (
                self._trajectory[self._current_index].get("timestamp", 0.0)
                if self._current_index < total
                else self._config.start_timestamp
            )
            return ReplayState(
                status=self._status,
                current_timestamp=current_ts,
                progress=progress,
                time_factor=self._config.time_factor,
                total_frames=total,
                current_frame=self._current_index,
            )

    def load_trajectory(self, trajectory: list[dict[str, Any]]) -> None:
        """加载已解析的历史轨迹数据。

        Args:
            trajectory: 轨迹帧列表，每帧需含 timestamp/position/velocity 字段。
        """
        self._trajectory = sorted(trajectory, key=lambda f: f.get("timestamp", 0.0))
        self._current_index = 0
        self._status = ReplayStatus.LOADING
        logger.info(f"ReplayEngine loaded {len(self._trajectory)} trajectory frames")

    def play(self) -> None:
        """开始回放（非阻塞，启动后台线程）。"""
        if self._status == ReplayStatus.PLAYING:
            return
        if not self._trajectory:
            raise ConfigurationError("ReplayEngine.play", "No trajectory loaded")
        self._status = ReplayStatus.PLAYING
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._play_loop,
            name=f"Replay-{self._config.replay_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Replay started: {self._config.replay_id}")

    def pause(self) -> None:
        """暂停回放。"""
        if self._status == ReplayStatus.PLAYING:
            self._status = ReplayStatus.PAUSED

    def resume(self) -> None:
        """恢复回放。"""
        if self._status == ReplayStatus.PAUSED:
            self._status = ReplayStatus.PLAYING

    def set_time_factor(self, factor: float) -> None:
        """更新播放速度因子。

        Args:
            factor: 速度因子，支持 0.25/0.5/1.0/2.0/4.0/8.0。
        """
        valid = {0.25, 0.5, 1.0, 2.0, 4.0, 8.0}
        if factor not in valid:
            raise ConfigurationError("set_time_factor", f"Invalid factor {factor}, must be in {valid}")
        self._config.time_factor = factor

    def step_forward(self) -> None:
        """单步前进一帧（20ms 仿真步长）。"""
        with self._lock:
            if self._current_index < len(self._trajectory):
                self._apply_frame(self._trajectory[self._current_index])
                self._current_index += 1

    def seek(self, timestamp: float) -> None:
        """跳转到指定时间戳（拖拽功能）。

        Args:
            timestamp: 目标 Unix 时间戳（秒）。
        """
        with self._lock:
            for i, frame in enumerate(self._trajectory):
                if frame.get("timestamp", 0.0) >= timestamp:
                    self._current_index = i
                    self._apply_frame(frame)
                    return
            self._current_index = len(self._trajectory) - 1
            if self._trajectory:
                self._apply_frame(self._trajectory[-1])

    def stop(self) -> None:
        """停止回放并等待线程退出。"""
        self._stop_event.set()
        self._status = ReplayStatus.IDLE
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _play_loop(self) -> None:
        """回放主循环（后台线程执行）。"""
        delta_real_s = 0.02 / self._config.time_factor  # 仿真 20ms 对应的实际时间

        while not self._stop_event.is_set():
            if self._status == ReplayStatus.PAUSED:
                time.sleep(0.05)
                continue

            with self._lock:
                if self._current_index >= len(self._trajectory):
                    if self._config.loop:
                        self._current_index = 0
                    else:
                        self._status = ReplayStatus.COMPLETED
                        return

                frame = self._trajectory[self._current_index]
                self._apply_frame(frame)
                self._current_index += 1

            time.sleep(delta_real_s)

    def _apply_frame(self, frame: dict[str, Any]) -> None:
        """将一帧轨迹数据应用到 CARLA 车辆 Actor。"""
        try:
            import carla  # noqa: PLC0415
            pos = frame.get("position", {})
            rot = frame.get("rotation", {})
            tf = carla.Transform(
                carla.Location(x=float(pos.get("x", 0.0)), y=float(pos.get("y", 0.0)), z=float(pos.get("z", 0.0))),
                carla.Rotation(pitch=float(rot.get("pitch", 0.0)), yaw=float(rot.get("yaw", 0.0))),
            )
            self._vehicle.set_transform(tf)
        except Exception as exc:
            logger.debug(f"Apply frame error: {exc}")
