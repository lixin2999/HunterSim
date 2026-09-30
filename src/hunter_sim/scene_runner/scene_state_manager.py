"""场景状态机管理（PROMPT-ENG-003-B）。

场景状态机：CREATED → LOADING → READY → RUNNING → COMPLETED/FAILED
支持 PAUSED 中间状态（仅从 RUNNING 可达）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from hunter_sim.common.exceptions import InstanceStateError
from hunter_sim.common.models import SceneStatus
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 合法状态转换矩阵
_TRANSITIONS: dict[SceneStatus, set[SceneStatus]] = {
    SceneStatus.CREATED:   {SceneStatus.LOADING, SceneStatus.FAILED},
    SceneStatus.LOADING:   {SceneStatus.READY, SceneStatus.FAILED},
    SceneStatus.READY:     {SceneStatus.RUNNING, SceneStatus.FAILED},
    SceneStatus.RUNNING:   {SceneStatus.PAUSED, SceneStatus.COMPLETED, SceneStatus.FAILED},
    SceneStatus.PAUSED:    {SceneStatus.RUNNING, SceneStatus.FAILED, SceneStatus.COMPLETED},
    SceneStatus.COMPLETED: set(),
    SceneStatus.FAILED:    set(),
}


@dataclass
class SceneStateSnapshot:
    """场景状态快照（供 WebSocket 推送和查询）。

    Attributes:
        scene_id: 场景 ID。
        status: 当前状态。
        elapsed_seconds: 已运行时间（秒）。
        progress: 完成进度 (0.0 ~ 1.0)。
        status_timestamp: 状态时间戳。
        error_message: 失败时的错误信息。
        extra: 扩展数据字典。
    """

    scene_id: str
    status: SceneStatus
    elapsed_seconds: float = 0.0
    progress: float = 0.0
    status_timestamp: float = field(default_factory=time.time)
    error_message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class SceneStateManager:
    """场景状态机管理器。

    线程安全，状态变更时触发注册的回调函数（用于 WebSocket 推送）。

    Args:
        scene_id: 场景唯一 ID。
        on_state_change: 状态变化回调函数，签名为 (snapshot) -> None。
    """

    def __init__(
        self,
        scene_id: str,
        on_state_change: Optional[Callable[[SceneStateSnapshot], None]] = None,
    ) -> None:
        self._scene_id = scene_id
        self._status: SceneStatus = SceneStatus.CREATED
        self._lock: threading.Lock = threading.Lock()
        self._start_time: float = 0.0
        self._pause_time: float = 0.0
        self._total_paused: float = 0.0
        self._error_message: str = ""
        self._duration: float = 60.0
        self._on_state_change = on_state_change

    @property
    def status(self) -> SceneStatus:
        """当前状态（只读）。"""
        with self._lock:
            return self._status

    def set_duration(self, duration_seconds: float) -> None:
        """设置场景总时长（用于进度计算）。"""
        with self._lock:
            self._duration = max(0.1, duration_seconds)

    def transition_to(self, new_status: SceneStatus, error_msg: str = "") -> None:
        """执行状态转换。

        Args:
            new_status: 目标状态。
            error_msg: 失败状态时的错误信息。

        Raises:
            InstanceStateError: 非法状态转换时抛出。
        """
        with self._lock:
            allowed = _TRANSITIONS.get(self._status, set())
            if new_status not in allowed:
                raise InstanceStateError(
                    instance_id=self._scene_id,
                    current_state=self._status.value,
                    target_state=new_status.value,
                )

            old_status = self._status
            self._status = new_status

            if new_status == SceneStatus.RUNNING and old_status != SceneStatus.PAUSED:
                self._start_time = time.perf_counter()
                self._total_paused = 0.0
            elif new_status == SceneStatus.PAUSED:
                self._pause_time = time.perf_counter()
            elif new_status == SceneStatus.RUNNING and old_status == SceneStatus.PAUSED:
                self._total_paused += time.perf_counter() - self._pause_time
            elif new_status == SceneStatus.FAILED:
                self._error_message = error_msg

            logger.info(f"Scene '{self._scene_id}' state: {old_status.value} → {new_status.value}")

        snapshot = self.get_snapshot()
        if self._on_state_change:
            try:
                self._on_state_change(snapshot)
            except Exception as exc:
                logger.warning(f"State change callback error: {exc}")

    def get_snapshot(self) -> SceneStateSnapshot:
        """获取当前状态快照。"""
        with self._lock:
            elapsed = self._get_elapsed_locked()
            progress = min(1.0, elapsed / self._duration) if self._duration > 0 else 0.0
            return SceneStateSnapshot(
                scene_id=self._scene_id,
                status=self._status,
                elapsed_seconds=elapsed,
                progress=progress,
                status_timestamp=time.time(),
                error_message=self._error_message,
            )

    def _get_elapsed_locked(self) -> float:
        """获取已运行时间（需在锁内调用）。"""
        if self._start_time == 0.0:
            return 0.0
        if self._status == SceneStatus.PAUSED:
            return self._pause_time - self._start_time - self._total_paused
        return time.perf_counter() - self._start_time - self._total_paused
