"""资源配额与健康监控（PROMPT-ENG-008-A）。

设计文档 §14.1 仿真安全：
- 资源配额：单用户最大并发实例数默认 5 个（§14.2）
- 超时保护：实例超过最大运行时间（默认 2 小时）自动销毁（§15.5 僵尸实例清理）
- 异常恢复：CARLA 服务崩溃自动重启，最多重试 3 次
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from hunter_sim.common.exceptions import ResourceError
from hunter_sim.common.models import InstanceStatus, ResourceSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


class ResourceQuotaManager:
    """用户级并发实例配额管理（设计文档 §14.2）。

    Args:
        max_per_user: 每用户最大并发实例数（文档默认 5）。
        max_total: 全局最大并发实例总数。
    """

    def __init__(self, max_per_user: int = 5, max_total: int = 50) -> None:
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
    """仿真实例健康监控（设计文档 §14.1/§15.5）。

    后台周期执行：
    1. 对已注册实例执行健康探针，失败时调用 restart_callback 自动重启，
       最多重试 max_retry_count 次（默认 3），超出后标记为不可恢复；
    2. 调用 expired_cleanup_callback 销毁超时（默认 2 小时）僵尸实例。

    Args:
        settings: 资源配置。
        check_callback: 健康检查回调 (instance_id) -> bool。
        restart_callback: 异常重启回调 (instance_id) -> None。
        expired_cleanup_callback: 超时实例清理回调 -> list[str]。
    """

    def __init__(
        self,
        settings: Optional[ResourceSettings] = None,
        check_callback: Optional[object] = None,
        restart_callback: Optional[object] = None,
        expired_cleanup_callback: Optional[object] = None,
    ) -> None:
        self._settings = settings or ResourceSettings()
        self._check_callback = check_callback
        self._restart_callback = restart_callback
        self._expired_cleanup_callback = expired_cleanup_callback
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._retry_counts: dict[str, int] = {}
        self._unrecoverable: set[str] = set()
        self._watched: set[str] = set()

    def register_instance(self, instance_id: str) -> None:
        """注册需要监控的实例。"""
        self._watched.add(instance_id)

    def unregister_instance(self, instance_id: str) -> None:
        """取消监控（销毁实例时调用，清理重试计数）。"""
        self._watched.discard(instance_id)
        self._retry_counts.pop(instance_id, None)
        self._unrecoverable.discard(instance_id)

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

    def is_unrecoverable(self, instance_id: str) -> bool:
        """实例是否已超过最大重试次数被标记为不可恢复。"""
        return instance_id in self._unrecoverable

    def run_check_cycle(self) -> dict[str, list[str]]:
        """执行一轮检查：健康探针 + 重启重试 + 超时清理。

        Returns:
            含 failed / restarted / exhausted / expired_destroyed 四类实例 ID 的字典。
        """
        result: dict[str, list[str]] = {
            "failed": [], "restarted": [], "exhausted": [], "expired_destroyed": [],
        }
        for instance_id in sorted(self._watched):
            if callable(self._check_callback):
                try:
                    healthy = bool(self._check_callback(instance_id))  # type: ignore[operator]
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Health check error for '{instance_id}': {exc}")
                    healthy = False
            else:
                healthy = True
            if healthy:
                # 恢复正常后重置重试计数
                self._retry_counts[instance_id] = 0
                continue
            result["failed"].append(instance_id)
            if instance_id in self._unrecoverable:
                continue
            count = self._retry_counts.get(instance_id, 0) + 1
            self._retry_counts[instance_id] = count
            if count > self._settings.max_retry_count:
                # 异常恢复：最多重试 3 次，超出后不再重启（§14.1）
                self._unrecoverable.add(instance_id)
                result["exhausted"].append(instance_id)
                logger.error(
                    f"Instance '{instance_id}' exceeded max retries ({self._settings.max_retry_count}), "
                    "marked unrecoverable"
                )
            elif callable(self._restart_callback):
                try:
                    self._restart_callback(instance_id)  # type: ignore[operator]
                    result["restarted"].append(instance_id)
                    logger.warning(f"Instance '{instance_id}' restarted (attempt {count})")
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"Restart callback failed for '{instance_id}': {exc}")

        if callable(self._expired_cleanup_callback):
            try:
                expired = self._expired_cleanup_callback() or []  # type: ignore[operator]
                result["expired_destroyed"] = list(expired)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Expired instance cleanup failed: {exc}")
        return result

    def _loop(self) -> None:
        while self._running:
            time.sleep(self._settings.health_check_interval_seconds)
            cycle = self.run_check_cycle()
            if cycle["failed"] or cycle["expired_destroyed"]:
                logger.info(
                    f"Health cycle: failed={cycle['failed']} "
                    f"restarted={cycle['restarted']} expired={cycle['expired_destroyed']}"
                )
