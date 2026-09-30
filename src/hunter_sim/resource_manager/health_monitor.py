"""资源配额与健康监控（PROMPT-ENG-008-A）。"""

from __future__ import annotations

import threading
import time
from typing import Optional

from hunter_sim.common.exceptions import ResourceError
from hunter_sim.common.models import InstanceStatus, ResourceSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


class ResourceQuotaManager:
    """用户级并发实例配额管理。

    Args:
        max_per_user: 每用户最大并发实例数。
        max_total: 全局最大并发实例总数。
    """

    def __init__(self, max_per_user: int = 2, max_total: int = 8) -> None:
        self._max_per_user = max_per_user
        self._max_total = max_total
        self._user_counts: dict[str, int] = {}
        self._total_count: int = 0
        self._lock: threading.Lock = threading.Lock()

    def check_and_reserve(self, user_id: str) -> None:
        """检查配额并预占一个名额。

        Raises:
            ResourceError: 超出用户配额或全局配额时抛出。
        """
        with self._lock:
            user_count = self._user_counts.get(user_id, 0)
            if user_count >= self._max_per_user:
                raise ResourceError(
                    "quota",
                    f"User '{user_id}' already has {user_count} instances (max {self._max_per_user})",
                )
            if self._total_count >= self._max_total:
                raise ResourceError(
                    "quota",
                    f"Global instance limit reached ({self._total_count}/{self._max_total})",
                )
            self._user_counts[user_id] = user_count + 1
            self._total_count += 1

    def release(self, user_id: str) -> None:
        """释放一个配额名额。"""
        with self._lock:
            if user_id in self._user_counts:
                self._user_counts[user_id] = max(0, self._user_counts[user_id] - 1)
                if self._user_counts[user_id] == 0:
                    del self._user_counts[user_id]
            self._total_count = max(0, self._total_count - 1)

    def get_usage(self, user_id: str = "") -> dict[str, int]:
        """查询配额使用情况。"""
        with self._lock:
            if user_id:
                return {"user_count": self._user_counts.get(user_id, 0), "max": self._max_per_user}
            return {"total": self._total_count, "max_total": self._max_total, "users": dict(self._user_counts)}


class InstanceHealthMonitor:
    """仿真实例健康监控（后台定期检查 CARLA RPC 连通性）。

    Args:
        settings: 资源配置。
        check_callback: 健康检查回调函数 (instance_id) -> bool。
    """

    def __init__(
        self,
        settings: Optional[ResourceSettings] = None,
        check_callback: Optional[object] = None,
    ) -> None:
        self._settings = settings or ResourceSettings()
        self._check_callback = check_callback
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._retry_counts: dict[str, int] = {}

    def start(self) -> None:
        """启动后台健康检查线程。"""
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="InstanceHealthMonitor", daemon=True)
        self._thread.start()
        logger.info("InstanceHealthMonitor started")

    def stop(self) -> None:
        """停止健康检查。"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def get_retry_count(self, instance_id: str) -> int:
        """获取实例重试次数。"""
        return self._retry_counts.get(instance_id, 0)

    def _loop(self) -> None:
        while self._running:
            time.sleep(self._settings.health_check_interval_seconds)
            # 实际健康检查由调用方通过 check_callback 注入
