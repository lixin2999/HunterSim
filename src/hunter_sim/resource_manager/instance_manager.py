"""仿真实例管理器（PROMPT-ENG-008-A）。

SimInstanceManager 管理仿真实例完整生命周期，
包括创建/启动/停止/销毁，以及超时自动回收。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from hunter_sim.common.exceptions import InstanceStateError, ResourceError
from hunter_sim.common.models import (
    InstanceStatus,
    QualityLevel,
    ResourceSettings,
    SimMode,
)
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 合法状态转换
_TRANSITIONS: dict[InstanceStatus, set[InstanceStatus]] = {
    InstanceStatus.CREATED:   {InstanceStatus.LOADING, InstanceStatus.DESTROYED},
    InstanceStatus.LOADING:   {InstanceStatus.READY, InstanceStatus.FAILED},
    InstanceStatus.READY:     {InstanceStatus.RUNNING, InstanceStatus.DESTROYED},
    InstanceStatus.RUNNING:   {InstanceStatus.PAUSED, InstanceStatus.COMPLETED, InstanceStatus.FAILED},
    InstanceStatus.PAUSED:    {InstanceStatus.RUNNING, InstanceStatus.DESTROYED, InstanceStatus.FAILED},
    InstanceStatus.COMPLETED: {InstanceStatus.DESTROYED},
    InstanceStatus.FAILED:    {InstanceStatus.DESTROYED},
    InstanceStatus.DESTROYED: set(),
}


@dataclass
class SimInstance:
    """仿真实例数据模型（设计文档 §10.2.2）。

    Attributes:
        sim_instance_id: 实例唯一 ID。
        status: 生命周期状态。
        mode: 仿真模式（vil/sil/replay）。
        map_id: 当前地图。
        quality: 渲染画质（过载降级后为实际画质）。
        gpu_id: 分配到的 GPU 编号。
        carla_host: CARLA RPC 主机地址。
        carla_rpc_port: RPC 端口（默认 2000）。
        carla_stream_port: 视频流端口（默认 2001）。
        scene_id: 关联场景 ID。
        vehicle_id: 关联实车 ID。
        user_id: 操作用户 ID。
        create_time: 创建时间戳（epoch 秒）。
        start_time: 启动时间戳（未启动为 0）。
        docker_container_id: 容器 ID（§10.2.3 容器化部署）。
        resource_limits: 容器资源限额（cpu/memory/gpu limit）。
        max_lifetime_s: 最大运行时长。
        retry_count: 异常重试次数。
        error_message: 失败原因。
    """

    sim_instance_id: str
    status: InstanceStatus
    mode: SimMode
    map_id: str
    quality: QualityLevel
    gpu_id: int
    carla_host: str = "127.0.0.1"
    carla_rpc_port: int = 2000
    carla_stream_port: int = 2001
    scene_id: str = ""
    vehicle_id: str = ""
    vehicle_model: str = "hunter.se"
    replay_config: dict[str, Any] = field(default_factory=dict)
    user_id: str = ""
    create_time: float = field(default_factory=time.time)
    start_time: float = 0.0
    docker_container_id: str = ""
    resource_limits: dict[str, str] = field(
        default_factory=lambda: {"cpu_limit": "4", "memory_limit": "8Gi", "gpu_limit": "1"}
    )
    max_lifetime_s: float = 7200.0
    retry_count: int = 0
    error_message: str = ""

    def is_expired(self) -> bool:
        """检查实例是否超过最大运行时间。"""
        if self.start_time == 0.0:
            return False
        return (time.time() - self.start_time) > self.max_lifetime_s

    def to_dict(self) -> dict[str, Any]:
        """按设计文档 §10.2.2 实例数据模型序列化。"""

        def _iso(ts: float) -> Optional[str]:
            """epoch 秒转 ISO8601（UTC），未设置时返回 None。"""
            if ts <= 0:
                return None
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        return {
            "sim_instance_id": self.sim_instance_id,
            "status": self.status.value,
            "carla_server": {
                "host": self.carla_host,
                "rpc_port": self.carla_rpc_port,
                "stream_port": self.carla_stream_port,
                "container_id": self.docker_container_id,
            },
            "map": self.map_id,
            "scene_id": self.scene_id,
            "mode": self.mode.value,
            "vehicle_id": self.vehicle_id,
            "create_time": _iso(self.create_time),
            "start_time": _iso(self.start_time),
            "gpu_id": self.gpu_id,
            "resources": dict(self.resource_limits),
        }


class SimInstanceManager:
    """仿真实例生命周期管理器。

    Args:
        settings: 资源管理配置。
        gpu_pool: GPU 资源池实例。
    """

    def __init__(
        self,
        settings: Optional[ResourceSettings] = None,
        gpu_pool: Optional[Any] = None,
    ) -> None:
        self._settings = settings or ResourceSettings()
        self._gpu_pool = gpu_pool
        self._instances: dict[str, SimInstance] = {}
        self._lock: threading.RLock = threading.RLock()
        logger.info("SimInstanceManager initialized")

    def create_instance(
        self,
        mode: SimMode,
        map_id: str,
        quality: QualityLevel,
        user_id: str = "",
        vehicle_id: str = "",
        scene_id: str = "",
        vehicle_model: str = "hunter.se",
        replay_config: Optional[dict[str, Any]] = None,
    ) -> SimInstance:
        """创建新仿真实例。

        Args:
            mode: 仿真模式。
            map_id: 地图 ID。
            quality: 画质等级。
            user_id: 操作用户 ID。
            vehicle_id: 关联实车 ID（VIL 模式）。
            scene_id: 关联场景 ID。
            vehicle_model: 自车模型 ID（文档 §12.2）。
            replay_config: replay 模式回放配置（文档 §12.2）。

        Returns:
            创建的 SimInstance。

        Raises:
            ResourceError: GPU 资源不足。
        """
        instance_id = str(uuid.uuid4())
        gpu_id: int = -1

        if self._gpu_pool is not None:
            # 过载降级：支持降低画质以提高并发数（文档 §10.3.1）
            allocate_ex = getattr(self._gpu_pool, "allocate_ex", None)
            if (
                self._settings.gpu_allow_overload_downgrade
                and callable(allocate_ex)
            ):
                gpu_id, quality = allocate_ex(quality, allow_downgrade=True)
            else:
                gpu_id = self._gpu_pool.allocate(quality)
            if gpu_id < 0:
                raise ResourceError(
                    "gpu",
                    f"No GPU available for quality={quality.value}. "
                    "All GPUs at capacity.",
                )

        instance = SimInstance(
            sim_instance_id=instance_id,
            status=InstanceStatus.CREATED,
            mode=mode,
            map_id=map_id,
            quality=quality,
            gpu_id=gpu_id,
            user_id=user_id,
            vehicle_id=vehicle_id,
            scene_id=scene_id,
            vehicle_model=vehicle_model,
            replay_config=dict(replay_config or {}),
            max_lifetime_s=self._settings.instance_max_lifetime_seconds,
        )

        with self._lock:
            self._instances[instance_id] = instance

        logger.info(f"Instance created: {instance_id} (gpu={gpu_id}, quality={quality.value})")
        return instance

    def get_instance(self, instance_id: str) -> Optional[SimInstance]:
        """查询实例信息。"""
        with self._lock:
            return self._instances.get(instance_id)

    def list_instances(self, user_id: str = "") -> list[SimInstance]:
        """列出所有实例（可按用户过滤）。"""
        with self._lock:
            instances = list(self._instances.values())
        if user_id:
            instances = [i for i in instances if i.user_id == user_id]
        return instances

    def transition(self, instance_id: str, new_status: InstanceStatus, error_msg: str = "") -> None:
        """执行实例状态转换。

        Args:
            instance_id: 实例 ID。
            new_status: 目标状态。
            error_msg: 失败时的错误信息。

        Raises:
            InstanceStateError: 实例不存在或状态转换非法。
        """
        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None:
                raise InstanceStateError(instance_id, "not_found", new_status.value)
            allowed = _TRANSITIONS.get(instance.status, set())
            if new_status not in allowed:
                raise InstanceStateError(instance_id, instance.status.value, new_status.value)
            instance.status = new_status
            if new_status == InstanceStatus.RUNNING:
                instance.start_time = time.time()
            elif new_status == InstanceStatus.FAILED:
                instance.error_message = error_msg

        logger.info(f"Instance '{instance_id}' status: {new_status.value}")

    def destroy_instance(self, instance_id: str) -> None:
        """销毁实例并释放 GPU 资源。"""
        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None:
                return
            if instance.status not in (InstanceStatus.DESTROYED,):
                instance.status = InstanceStatus.DESTROYED
            if self._gpu_pool is not None and instance.gpu_id >= 0:
                self._gpu_pool.release(instance.gpu_id, instance.quality)
            del self._instances[instance_id]
        logger.info(f"Instance destroyed: {instance_id}")

    def check_expired_instances(self) -> list[str]:
        """检查并销毁超时的运行实例，返回被销毁的实例 ID 列表。"""
        expired: list[str] = []
        with self._lock:
            for inst in list(self._instances.values()):
                if inst.status == InstanceStatus.RUNNING and inst.is_expired():
                    expired.append(inst.sim_instance_id)
        for iid in expired:
            logger.warning(f"Instance '{iid}' expired, destroying")
            self.transition(iid, InstanceStatus.COMPLETED)
            self.destroy_instance(iid)
        return expired
