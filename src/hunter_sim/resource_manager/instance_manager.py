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
from hunter_sim.common.metrics import (
    INSTANCE_LIFECYCLE_TOTAL,
    SCHEDULING_DECISIONS_TOTAL,
)
from hunter_sim.common.models import (
    CarlaSettings,
    InstanceStatus,
    PlatformSettings,
    QualityLevel,
    ResourceSettings,
    SimMode,
)
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 重启恢复时执行面已丢失：运行中/加载中的实例退回到可重新装配的状态
_RECOVERABLE_LOADING = (InstanceStatus.LOADING,)
_RECOVERABLE_RUNNING = (InstanceStatus.RUNNING, InstanceStatus.PAUSED)

# 合法状态转换
_TRANSITIONS: dict[InstanceStatus, set[InstanceStatus]] = {
    InstanceStatus.CREATED:   {InstanceStatus.LOADING, InstanceStatus.DESTROYED},
    InstanceStatus.LOADING:   {InstanceStatus.READY, InstanceStatus.FAILED},
    InstanceStatus.READY:     {InstanceStatus.RUNNING, InstanceStatus.DESTROYED, InstanceStatus.FAILED},
    InstanceStatus.RUNNING:   {InstanceStatus.PAUSED, InstanceStatus.COMPLETED, InstanceStatus.FAILED},
    InstanceStatus.PAUSED:    {InstanceStatus.RUNNING, InstanceStatus.DESTROYED, InstanceStatus.FAILED},
    InstanceStatus.COMPLETED: {InstanceStatus.DESTROYED},
    InstanceStatus.FAILED:    {InstanceStatus.DESTROYED},
    InstanceStatus.DESTROYED: set(),
}

# 终态实例不再占用引擎端点（其端点可被新实例复用）
_TERMINAL_STATUSES = (
    InstanceStatus.COMPLETED,
    InstanceStatus.FAILED,
    InstanceStatus.DESTROYED,
)


def _endpoint_addr(endpoint: Any) -> tuple[str, int]:
    """把引擎端点投影为 ``(host, rpc_port)``，与控制面已分配地址对齐比对。"""
    return str(getattr(endpoint, "host", "")), int(getattr(endpoint, "rpc_port", 0))


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
        tenant: 所属租户 ID（V2.5.0 平台化隔离）。
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
    tenant: str = "default"
    create_time: float = field(default_factory=time.time)
    start_time: float = 0.0
    docker_container_id: str = ""
    resource_limits: dict[str, str] = field(
        default_factory=lambda: {"cpu_limit": "4", "memory_limit": "8Gi", "gpu_limit": "1"}
    )
    max_lifetime_s: float = 7200.0
    retry_count: int = 0
    error_message: str = ""
    restored: bool = False
    """是否由启动恢复流程从持久化存储重建（执行面需重新装配）。"""

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
            "tenant": self.tenant,
            "create_time": _iso(self.create_time),
            "start_time": _iso(self.start_time),
            "gpu_id": self.gpu_id,
            "resources": dict(self.resource_limits),
            "restored": self.restored,
        }

    def to_record(self) -> dict[str, Any]:
        """序列化为持久化记录（与 ``from_record`` 配对，枚举存字符串）。"""
        return {
            "sim_instance_id": self.sim_instance_id,
            "status": self.status.value,
            "mode": self.mode.value,
            "map_id": self.map_id,
            "quality": self.quality.value,
            "gpu_id": self.gpu_id,
            "carla_host": self.carla_host,
            "carla_rpc_port": self.carla_rpc_port,
            "carla_stream_port": self.carla_stream_port,
            "scene_id": self.scene_id,
            "vehicle_id": self.vehicle_id,
            "vehicle_model": self.vehicle_model,
            "replay_config": dict(self.replay_config),
            "user_id": self.user_id,
            "tenant": self.tenant,
            "create_time": self.create_time,
            "start_time": self.start_time,
            "docker_container_id": self.docker_container_id,
            "resource_limits": dict(self.resource_limits),
            "max_lifetime_s": self.max_lifetime_s,
            "retry_count": self.retry_count,
            "error_message": self.error_message,
            "restored": self.restored,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "SimInstance":
        """从持久化记录重建实例（字段缺失/枚举非法时回退默认值）。"""

        def _status() -> InstanceStatus:
            try:
                status = InstanceStatus(record.get("status", "created"))
            except ValueError:
                return InstanceStatus.CREATED
            # 执行面不跨进程存活：LOADING → CREATED；RUNNING/PAUSED → READY
            if status in _RECOVERABLE_LOADING:
                return InstanceStatus.CREATED
            if status in _RECOVERABLE_RUNNING:
                return InstanceStatus.READY
            return status

        try:
            mode = SimMode(record.get("mode", "sil"))
        except ValueError:
            mode = SimMode.SIL
        try:
            quality = QualityLevel(record.get("quality", "medium"))
        except ValueError:
            quality = QualityLevel.MEDIUM

        return cls(
            sim_instance_id=str(record["sim_instance_id"]),
            status=_status(),
            mode=mode,
            map_id=str(record.get("map_id", "Town03")),
            quality=quality,
            # GPU 资源池不持久化，重建实例不持有旧 GPU 名额
            gpu_id=-1,
            carla_host=str(record.get("carla_host", "127.0.0.1")),
            carla_rpc_port=int(record.get("carla_rpc_port", 2000)),
            carla_stream_port=int(record.get("carla_stream_port", 2001)),
            scene_id=str(record.get("scene_id", "")),
            vehicle_id=str(record.get("vehicle_id", "")),
            vehicle_model=str(record.get("vehicle_model", "hunter.se")),
            replay_config=dict(record.get("replay_config") or {}),
            user_id=str(record.get("user_id", "")),
            tenant=str(record.get("tenant", "default")),
            create_time=float(record.get("create_time") or time.time()),
            start_time=0.0,
            docker_container_id=str(record.get("docker_container_id", "")),
            resource_limits=dict(record.get("resource_limits") or {}),
            max_lifetime_s=float(record.get("max_lifetime_s") or 7200.0),
            retry_count=int(record.get("retry_count", 0)),
            error_message=str(record.get("error_message", "")),
            restored=True,
        )


class SimInstanceManager:
    """仿真实例生命周期管理器。

    Args:
        settings: 资源管理配置。
        gpu_pool: GPU 资源池实例。
        quota_manager: 用户配额管理器（可选）。注入后配额释放在
            destroy_instance() 内统一收敛，覆盖 API 销毁与超时自动回收两条路径（§14.2）。
        carla: CARLA 连接配置（可选）。提供实例地址与基准端口；
            未声明引擎库存时，所有实例共享该单引擎端点（V2.4.0）。
        engine_endpoints: 引擎端点库存（可选）。提供后实例创建时独占分配
            一个未占用端点，支持多引擎并行；耗尽时抛 ResourceError。
        engine_pool: 引擎租约池（可选，V2.8.0 P2-14）。注入后调度可感知
            端点占用与已加载地图，同地图实例优先复用同一引擎（免重复 load_world）。
        state_store: 状态持久化存储（可选）。提供后实例创建/流转/销毁同步落盘，
            可通过 restore() 在服务重启后重建实例表。
        platform: 平台化配置（可选）。提供租户级并发上限与默认租户（V2.5.0）。
    """

    def __init__(
        self,
        settings: Optional[ResourceSettings] = None,
        gpu_pool: Optional[Any] = None,
        quota_manager: Optional[Any] = None,
        carla: Optional[CarlaSettings] = None,
        engine_endpoints: Optional[list[Any]] = None,
        state_store: Optional[Any] = None,
        platform: Optional[PlatformSettings] = None,
        engine_pool: Optional[Any] = None,
    ) -> None:
        self._settings = settings or ResourceSettings()
        self._gpu_pool = gpu_pool
        self._quota_manager = quota_manager
        self._carla = carla or CarlaSettings()
        self._engine_endpoints: list[Any] = list(engine_endpoints or [])
        self._state_store = state_store
        self._platform = platform or PlatformSettings()
        self._engine_pool = engine_pool
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
        tenant: str = "",
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
            tenant: 租户 ID（V2.5.0）；为空时取平台默认租户。

        Returns:
            创建的 SimInstance。

        Raises:
            ResourceError: GPU 资源不足或超出租户并发配额。
        """
        tenant = tenant or self._platform.default_tenant
        # 租户配额先行校验：必须在 GPU 分配前拒绝，避免分配后失败泄漏 GPU 名额
        self._check_tenant_quota(tenant)
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
            tenant=tenant,
            vehicle_id=vehicle_id,
            scene_id=scene_id,
            vehicle_model=vehicle_model,
            replay_config=dict(replay_config or {}),
            max_lifetime_s=self._settings.instance_max_lifetime_seconds,
        )

        with self._lock:
            # 引擎端点分配（V2.4.0）：声明库存时独占分配，耗尽即拒绝创建；
            # 未声明库存时所有实例共享单引擎，由执行面保证单场景约束
            # V2.8.0 P2-14：分配时带地图亲和，同地图空闲端点优先复用
            instance.carla_host, instance.carla_rpc_port, instance.carla_stream_port = (
                self._assign_endpoint(map_id)
            )
            self._instances[instance_id] = instance

        self._persist(instance)
        INSTANCE_LIFECYCLE_TOTAL.labels(event="create").inc()
        logger.info(
            f"Instance created: {instance_id} (tenant={tenant}, gpu={gpu_id}, "
            f"quality={quality.value}, "
            f"carla={instance.carla_host}:{instance.carla_rpc_port}/{instance.carla_stream_port})"
        )
        return instance

    def _check_tenant_quota(self, tenant: str) -> None:
        """校验租户并发实例配额（不计已终态实例）。

        Raises:
            ResourceError: 超出 ``PLATFORM_MAX_INSTANCES_PER_TENANT``。
        """
        limit = self._platform.max_instances_per_tenant
        with self._lock:
            count = sum(
                1
                for i in self._instances.values()
                if i.tenant == tenant
                and i.status not in (InstanceStatus.COMPLETED, InstanceStatus.FAILED)
            )
        if count >= limit:
            raise ResourceError(
                "quota",
                f"Tenant '{tenant}' already has {count} instances (max {limit}); "
                "destroy an idle instance or raise PLATFORM_MAX_INSTANCES_PER_TENANT",
            )

    def _assign_endpoint(self, map_id: str = "") -> tuple[str, int, int]:
        """分配引擎端点（已持有 self._lock）。

        - 未声明 ``engine_endpoints``：返回配置的单引擎端点；
        - 已声明且注入 ``engine_pool``（P2-14）：按地图亲和选空闲端点，
          同地图的已加载端点优先（省一次 ``load_world``）；
        - 已声明但无池（旧调用路径/单测）：按活跃实例占用表选首个空闲端点。

        Raises:
            ResourceError: 库存端点已全部占用。
        """
        if not self._engine_endpoints:
            return self._carla.host, self._carla.rpc_port, self._carla.stream_port
        if self._engine_pool is not None:
            # 双重占用防护：控制面创建实例时引擎租约尚未被执行面领取（租约在
            # 场景装配时才产生），故本管理器必须自行排除已分配给活跃实例的端点，
            # 否则同批多个实例会被调度到同一引擎，装配期才以 EngineUnavailable 失败。
            reserved = {
                (instance.carla_host, instance.carla_rpc_port)
                for instance in self._instances.values()
                if instance.status not in _TERMINAL_STATUSES
            }
            candidates = [ep for ep in self._engine_endpoints if _endpoint_addr(ep) not in reserved]
            endpoint = self._engine_pool.pick_free_endpoint(candidates, prefer_map_id=map_id)
            if endpoint is None:
                raise ResourceError(
                    "engine",
                    f"No CARLA engine endpoint available (all {len(self._engine_endpoints)} in use). "
                    "Destroy an idle instance or add endpoints via EXECUTION_ENGINE_ENDPOINTS",
                )
            loaded = str(self._engine_pool.loaded_map_of(endpoint.key) or "")
            if loaded and loaded == map_id:
                kind = "affinity_reuse"
            elif not loaded:
                kind = "fresh_endpoint"
            else:
                kind = "fallback"
            SCHEDULING_DECISIONS_TOTAL.labels(kind=kind).inc()
            logger.info(
                f"Engine scheduled for map '{map_id}': {endpoint.key} "
                f"(loaded_map={loaded or 'none'}, decision={kind})"
            )
            return endpoint.host, endpoint.rpc_port, endpoint.stream_port
        used = {
            (i.carla_host, i.carla_rpc_port)
            for i in self._instances.values()
            if i.status not in _TERMINAL_STATUSES
        }
        for endpoint in self._engine_endpoints:
            host = str(getattr(endpoint, "host", self._carla.host))
            rpc_port = int(getattr(endpoint, "rpc_port", self._carla.rpc_port))
            stream_port = int(getattr(endpoint, "stream_port", rpc_port + 1))
            if (host, rpc_port) not in used:
                SCHEDULING_DECISIONS_TOTAL.labels(kind="fallback").inc()
                return host, rpc_port, stream_port
        raise ResourceError(
            "engine",
            f"No CARLA engine endpoint available (all {len(self._engine_endpoints)} in use). "
            "Destroy an idle instance or add endpoints via EXECUTION_ENGINE_ENDPOINTS",
        )

    def get_instance(self, instance_id: str) -> Optional[SimInstance]:
        """查询实例信息。"""
        with self._lock:
            return self._instances.get(instance_id)

    def list_instances(self, user_id: str = "", tenant: str = "") -> list[SimInstance]:
        """列出实例（可按用户 / 租户过滤）。"""
        with self._lock:
            instances = list(self._instances.values())
        if user_id:
            instances = [i for i in instances if i.user_id == user_id]
        if tenant:
            instances = [i for i in instances if i.tenant == tenant]
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

        self._persist(instance)
        logger.info(f"Instance '{instance_id}' status: {new_status.value}")

    def destroy_instance(self, instance_id: str) -> None:
        """销毁实例并释放 GPU 资源与用户配额（§14.2 唯一释放出口）。

        API 销毁（DELETE /instances/{id}）与超时自动回收
        （check_expired_instances）均经由此方法，保证配额不泄漏。
        """
        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None:
                return
            if instance.status not in (InstanceStatus.DESTROYED,):
                instance.status = InstanceStatus.DESTROYED
            if self._gpu_pool is not None and instance.gpu_id >= 0:
                self._gpu_pool.release(instance.gpu_id, instance.quality)
            del self._instances[instance_id]
        # 配额释放在锁外执行（锁顺序：instance_manager → quota_manager，无反向路径，无死锁环）
        if self._quota_manager is not None and instance.user_id:
            self._quota_manager.release(instance.user_id)
        # 销毁同时删除持久化记录与场景绑定（唯一释放出口，防残留）
        if self._state_store is not None:
            try:
                self._state_store.delete_instance(instance_id)
            except Exception as exc:  # noqa: BLE001  持久化失败不阻塞销毁
                logger.warning(f"Persist delete failed for instance {instance_id}: {exc}")
        logger.info(f"Instance destroyed: {instance_id}")
        INSTANCE_LIFECYCLE_TOTAL.labels(event="destroy").inc()

    def check_expired_instances(self) -> list[str]:
        """检查并销毁超时的运行实例，返回被销毁的实例 ID 列表。"""
        expired: list[str] = []
        with self._lock:
            for inst in list(self._instances.values()):
                if inst.status == InstanceStatus.RUNNING and inst.is_expired():
                    expired.append(inst.sim_instance_id)
        for iid in expired:
            logger.warning(f"Instance '{iid}' expired, destroying")
            try:
                self.transition(iid, InstanceStatus.COMPLETED)
            except InstanceStateError as exc:
                # 巡检线程与用户并发操作竞争时状态可能已变更，继续回收资源即可
                logger.debug(f"Instance '{iid}' already moved out of RUNNING: {exc}")
            self.destroy_instance(iid)
        return expired

    # ─── 持久化 / 重启恢复 ───────────────────────────────────────────────────

    def restore(self) -> int:
        """从持久化存储重建实例表，返回恢复数量。

        重启后执行面（world/tick 线程）已丢失，恢复语义：
        - LOADING → CREATED（可重新下发场景）；
        - RUNNING/PAUSED → READY（场景定义若已持久化，下一次 start/下发时自动装配）；
        - gpu_id 置 -1（GPU 池不持久化，不持有旧名额）；
        - 非终态实例重新计入用户配额，避免重启后并发额度被绕过。
        """
        if self._state_store is None:
            return 0
        try:
            records = self._state_store.load_instances()
        except Exception as exc:  # noqa: BLE001  读库失败不阻断服务启动
            logger.error(f"Load persisted instances failed: {exc}")
            return 0

        restored = 0
        quota_reservations: dict[str, int] = {}
        with self._lock:
            for record in records:
                try:
                    instance = SimInstance.from_record(record)
                except Exception as exc:  # noqa: BLE001  单条损坏不影响其他实例
                    logger.warning(f"Skip corrupted instance record: {exc}")
                    continue
                if instance.sim_instance_id in self._instances:
                    continue
                # 旧库记录无 tenant 字段时回落到平台默认租户
                if not str(record.get("tenant", "")).strip():
                    instance.tenant = self._platform.default_tenant
                self._instances[instance.sim_instance_id] = instance
                restored += 1
                if instance.user_id and instance.status not in (
                    InstanceStatus.COMPLETED,
                    InstanceStatus.FAILED,
                ):
                    quota_reservations[instance.user_id] = (
                        quota_reservations.get(instance.user_id, 0) + 1
                    )
                # 回写归一化后的状态，保证下次重启语义一致
                self._persist(instance)

        if self._quota_manager is not None and hasattr(self._quota_manager, "restore_existing"):
            for user_id, count in quota_reservations.items():
                self._quota_manager.restore_existing(user_id, count)
        if restored:
            logger.info(f"Restored {restored} instance(s) from state store")
        return restored

    def _persist(self, instance: SimInstance) -> None:
        """尽力持久化实例记录（失败只告警，不影响主流程）。"""
        if self._state_store is None:
            return
        try:
            self._state_store.save_instance(instance.to_record())
        except Exception as exc:  # noqa: BLE001  存储故障降级为内存态
            logger.warning(f"Persist instance {instance.sim_instance_id} failed: {exc}")
