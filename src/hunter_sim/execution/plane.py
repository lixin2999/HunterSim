"""场景执行平面（V2.4.0 执行面分离）。

``ExecutionPlane`` 是执行面的唯一入口：为实例租用 CARLA 引擎、加载地图、
切换同步模式并创建 ``SceneRunnerService``；释放时销毁运行器并归还引擎租约。
控制面（``api/runtime.py``）只做 HTTP 错误映射与实例状态流转，不再直接
操作 CARLA 世界。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from hunter_sim.common.utils import get_logger
from hunter_sim.execution.engine_pool import EngineEndpoint, EnginePool
from hunter_sim.scene_runner.scene_runner_service import SceneRunnerService

logger = get_logger(__name__)

# 场景运行循环帧间隔（50Hz）
_DEFAULT_FIXED_DELTA_SECONDS = 0.02

# MapManager 解析器：按引擎端点返回 MapManager（None 表示该端点不可用）
MapManagerResolver = Callable[[EngineEndpoint], Optional[Any]]


class ExecutionUnavailable(RuntimeError):
    """执行面不可用（引擎被占用、端点无客户端或地图加载失败）。"""


def _world_map_id(world: Any, fallback: str) -> str:
    """读取世界实际地图 ID（CARLA 返回 ``/Game/Maps/Town03`` 取末段）。

    Mock/受限 world 无 ``get_map`` 时回退到请求地图，不阻断装配。
    """
    try:
        name = str(world.get_map().name)
    except Exception:  # noqa: BLE001  地图名不可读不影响主流程
        return fallback
    tail = name.rstrip("/").split("/")[-1] if name else ""
    return tail or fallback


class ExecutionPlane:
    """场景执行平面。

    V2.6.0 锁粒度拆分：注册表互斥（``lock``，与 API 层共享）只保护字典
    读写与租约领取；耗时的 ``load_world``/同步模式切换/运行器销毁在
    **引擎级锁**（按端点 host:rpc_port 懒创建）下执行，不同引擎的实例
    装配可并行，同一端点的 CARLA 操作仍串行为单线程（CARLA Client 非
    线程安全）。

    Args:
        pool: 引擎租约池。
        map_manager_resolver: 端点 → MapManager 解析器（测试/多引擎实现注入点）。
        runner_factory: 运行器工厂（默认 SceneRunnerService）。
        lock: 注册表互斥锁（与 API 层共享同一把锁，保护运行器注册表）。
        fixed_delta_seconds: 同步模式固定步长。
        runners: 复用的运行器注册表（与 ``app.state.scene_runners`` 同对象）。
    """

    def __init__(
        self,
        pool: EnginePool,
        map_manager_resolver: MapManagerResolver,
        runner_factory: Callable[..., Any] = SceneRunnerService,
        lock: Optional[threading.RLock] = None,
        fixed_delta_seconds: float = _DEFAULT_FIXED_DELTA_SECONDS,
        runners: Optional[dict[str, Any]] = None,
    ) -> None:
        self._pool = pool
        self._resolve_map_manager = map_manager_resolver
        self._runner_factory = runner_factory
        self._registry_lock = lock or threading.RLock()
        self._fixed_delta_seconds = fixed_delta_seconds
        self._runners: dict[str, Any] = runners if runners is not None else {}
        self._runner_engines: dict[str, str] = {}
        # 端点 key → 引擎级装配/销毁锁（在注册表锁内懒创建，避免建锁竞态）
        self._engine_locks: dict[str, threading.RLock] = {}
        # V2.9.0：在途装配计数与取消标记（解决“装配窗口内销毁实例”的泄漏竞态）
        self._assembling: dict[str, int] = {}
        self._cancelled: set[str] = set()

    @property
    def pool(self) -> EnginePool:
        """引擎租约池（供快照/排障）。"""
        return self._pool

    @property
    def runners(self) -> dict[str, Any]:
        """实例 → 场景运行器注册表（与 ``app.state.scene_runners`` 同对象）。"""
        return self._runners

    @property
    def active_engine_count(self) -> int:
        """当前被占用的引擎端点数。"""
        return self._pool.active_count

    def ensure_runner(
        self,
        instance: Any,
        on_state_change: Optional[Callable[..., None]] = None,
        on_event: Optional[Callable[..., None]] = None,
    ) -> Any:
        """确保实例已装配运行器并返回（可能触发地图加载，阻塞）。

        锁分层（V2.6.0）：注册表锁内完成重复检查与租约领取（短临界区），
        随后仅持有该端点的引擎锁执行慢速 CARLA 操作；多引擎部署时
        不同端点的装配互不阻塞。

        Args:
            instance: 仿真实例（需含 sim_instance_id/map_id/carla_* 字段）。
            on_state_change: 场景状态变化回调（持久化/推送）。
            on_event: 场景事件回调。

        Returns:
            SceneRunnerService（或注入的运行器）。

        Raises:
            ExecutionUnavailable: 引擎被占用、端点无客户端或地图加载失败。
        """
        instance_id = instance.sim_instance_id
        with self._registry_lock:
            existing = self._runners.get(instance_id)
            if existing is not None:
                return existing

            endpoint = self._endpoint_of(instance)
            try:
                lease = self._pool.acquire(instance_id, endpoint, instance.map_id)
            except Exception as exc:  # EngineUnavailable 等 → 统一为执行面不可用
                raise ExecutionUnavailable(str(exc)) from exc
            engine_lock = self._engine_lock(lease.endpoint.key)
            # V2.9.0：登记在途装配，让并发 release()（实例销毁）能取消本次慢装配
            self._assembling[instance_id] = self._assembling.get(instance_id, 0) + 1
            self._cancelled.discard(instance_id)

        # 慢操作（load_world 可达 10~30s）在注册表锁外、引擎锁内执行
        try:
            with engine_lock:
                # 同实例并发调用在等锁期间可能已被先者装配：事后再查一次避免重复 load
                with self._registry_lock:
                    raced = self._runners.get(instance_id)
                    if raced is not None:
                        # 早退也要释放装配名额（否则 release() 会误判仍有在途装配）
                        self._release_assembly_slot(instance_id)
                if raced is not None:
                    return raced
                map_manager = self._resolve_map_manager(lease.endpoint)
                if map_manager is None:
                    raise ExecutionUnavailable(
                        f"No CARLA client available for engine '{lease.endpoint.key}' "
                        f"(target {lease.endpoint.host}:{lease.endpoint.rpc_port})"
                    )
                world = map_manager.load_map(instance.map_id)
                self._configure_world(world)
                # P2-14：以事实加载的地图登记端点地图，下次调度才能命中亲和
                self._pool.note_loaded_map(
                    lease.endpoint.key, _world_map_id(world, instance.map_id)
                )
                runner = self._runner_factory(
                    world,
                    on_state_change=on_state_change,
                    on_event=on_event,
                )
        except ExecutionUnavailable:
            with self._registry_lock:
                self._release_assembly_slot(instance_id)
                self._cancelled.discard(instance_id)
            self._pool.release(instance_id)
            raise
        except Exception as exc:
            with self._registry_lock:
                self._release_assembly_slot(instance_id)
                self._cancelled.discard(instance_id)
            self._pool.release(instance_id)
            raise ExecutionUnavailable(
                f"Failed to assemble scene runtime on engine "
                f"'{lease.endpoint.key}' for map '{instance.map_id}': {exc}"
            ) from exc

        cancelled = False
        with self._registry_lock:
            # 递减与“查取消/登记运行器”必须同一临界区，否则存在毫秒级竞态窗口
            self._release_assembly_slot(instance_id)
            if instance_id in self._cancelled:
                self._cancelled.discard(instance_id)
                cancelled = True
            else:
                self._runners[instance_id] = runner
                self._runner_engines[instance_id] = lease.endpoint.key

        if cancelled:
            # V2.9.0：装配期间实例已被销毁/释放——丢弃刚建好的运行器与租约，
            # 避免“实例已不存在但仍占引擎/仍在 tick”的泄漏
            self._pool.release(instance_id)
            with engine_lock:
                try:
                    runner.destroy()
                except Exception as exc:  # noqa: BLE001  取消路径的销毁失败不阻断
                    logger.warning(f"Cancelled runner destroy failed for {instance_id}: {exc}")
            logger.info(f"Execution plane assembly cancelled: instance={instance_id}")
            raise ExecutionUnavailable(
                f"Scene assembly cancelled: instance '{instance_id}' was released while loading"
            )

        logger.info(
            f"Execution plane assembled: instance={instance_id}, "
            f"engine={lease.endpoint.key}, map={instance.map_id}"
        )
        return runner

    def release(self, instance_id: str) -> bool:
        """释放实例运行器并归还引擎租约，返回是否确实释放。"""
        with self._registry_lock:
            # V2.9.0：若有在途装配，登记取消标记；装配完成时自弃运行器与租约
            if self._assembling.get(instance_id):
                self._cancelled.add(instance_id)
            runner = self._runners.pop(instance_id, None)
            engine_key = self._runner_engines.pop(instance_id, None)
            released = self._pool.release(instance_id)
            engine_lock = self._engine_locks.get(engine_key) if engine_key else None
        if runner is None:
            return released

        # 销毁场景 Actor 同样是 CARLA 操作：在同一引擎锁下与后续装配串行，
        # 避免“旧运行器还在销毁、新实例已在同端点 load_world”的并发
        def _destroy() -> None:
            try:
                runner.destroy()
            except Exception as exc:  # noqa: BLE001  释放失败不应阻塞实例销毁
                logger.warning(f"Scene runtime release failed for instance {instance_id}: {exc}")

        if engine_lock is not None:
            with engine_lock:
                _destroy()
        else:
            _destroy()
        logger.info(f"Execution plane released: instance={instance_id}")
        return True

    def release_all(self) -> int:
        """释放全部运行器（服务关停），返回释放数量。"""
        with self._registry_lock:
            instance_ids = list(self._runners)
        released = 0
        for instance_id in instance_ids:
            if self.release(instance_id):
                released += 1
        return released

    def get_runner(self, instance_id: str) -> Any | None:
        """查询已装配运行器（未装配返回 None）。"""
        with self._registry_lock:
            return self._runners.get(instance_id)

    def heartbeat(self, instance_id: str) -> bool:
        """为存活运行器续一次引擎心跳（V2.7.0 P1-7）。

        仅当运行器存在且 ``is_running`` 为真时才续租；线程已死的僵尸运行器
        不会刷新心跳，租约因而逐渐变旧并被 ``/execution`` 标为 ``stale``。
        不支持 ``is_running`` 的运行器（测试桩）按存活处理，避免误伤。
        """
        with self._registry_lock:
            runner = self._runners.get(instance_id)
        if runner is None:
            return False
        alive = getattr(runner, "is_running", True)
        try:
            alive = bool(alive)
        except Exception:  # noqa: BLE001  is_running 不应抛；防御性按存活处理
            alive = True
        return self._pool.renew(instance_id) if alive else False

    def renew_live_leases(self) -> int:
        """对当前全部存活运行器批量续租（健康巡检周期调用），返回续租数。"""
        with self._registry_lock:
            instance_ids = list(self._runners)
        return sum(1 for iid in instance_ids if self.heartbeat(iid))

    def stale_instance_ids(self) -> list[str]:
        """返回僵尸租约对应的实例 ID（心跳超时且无存活运行器）。"""
        return [lease.instance_id for lease in self._pool.stale_leases()]

    def endpoint_key_of(self, instance_id: str) -> str:
        """实例当前占用的端点 key（无租约时返回空串，P2-14）。"""
        with self._registry_lock:
            cached = self._runner_engines.get(instance_id)
        if cached:
            return str(cached)
        lease = self._pool.get(instance_id)
        if lease is not None:
            return lease.endpoint.key
        return ""

    def map_conflict(
        self, endpoint_key: str, holder_instance_id: str, target_map_id: str
    ) -> tuple[str, str]:
        """端点地图冲突检查（P2-14：从全局单地图下沉到端点级）。

        仅当端点被**其他**实例活跃占用且已加载地图与目标地图不同时返回冲突。

        Returns:
            ``(冲突占用者实例 ID, 端点已加载地图)``；无冲突时为 ``("", "")``。
        """
        if not endpoint_key:
            return "", ""
        lease = self._pool.lease_of(endpoint_key)
        if lease is None or lease.instance_id == holder_instance_id:
            return "", ""
        loaded = self._pool.loaded_map_of(endpoint_key)
        if loaded and loaded != target_map_id:
            return lease.instance_id, loaded
        return "", ""

    def force_release(self, instance_id: str) -> bool:
        """管理强制回收：释放运行器与引擎租约（用于僵尸持有者，P1-7）。

        与 ``release`` 的区别：即使运行器对象不存在也强制归还租约（僵尸清理）。
        """
        released = self.release(instance_id)
        pool_released = self._pool.release(instance_id)
        return released or pool_released

    def lease_of(self, instance_id: str) -> Any | None:
        """实例当前引擎租约（无租约返回 None，V2.9.0 回收前判定）。"""
        return self._pool.get(instance_id)

    def is_stale_lease(self, instance_id: str) -> bool:
        """实例租约是否已心跳超时（僵尸持有者；无租约或未启用心跳时为 False）。"""
        return bool(self._pool.is_stale(instance_id))

    def _release_assembly_slot(self, instance_id: str) -> None:
        """递减在途装配计数（调用方必须持注册表锁）。"""
        pending = self._assembling.get(instance_id, 0) - 1
        if pending > 0:
            self._assembling[instance_id] = pending
        else:
            self._assembling.pop(instance_id, None)

    def snapshot(self) -> dict[str, Any]:
        """执行面快照（引擎、租约、运行器）。"""
        payload = self._pool.snapshot()
        with self._registry_lock:
            payload["runners"] = [
                {
                    "instance_id": instance_id,
                    "engine": self._runner_engines.get(instance_id),
                }
                for instance_id in self._runners
            ]
        return payload

    def _engine_lock(self, endpoint_key: str) -> threading.RLock:
        """获取（必要时创建）端点对应的引擎级锁（调用方已持注册表锁）。"""
        return self._engine_locks.setdefault(endpoint_key, threading.RLock())

    @staticmethod
    def _endpoint_of(instance: Any) -> EngineEndpoint:
        """从实例字段构造引擎端点（缺省回退本地 2000）。"""
        rpc_port = int(getattr(instance, "carla_rpc_port", 2000))
        stream_port = int(getattr(instance, "carla_stream_port", rpc_port + 1))
        tm_port = int(getattr(instance, "carla_tm_port", 8000))
        return EngineEndpoint(
            host=str(getattr(instance, "carla_host", "127.0.0.1")),
            rpc_port=rpc_port,
            stream_port=stream_port,
            tm_port=tm_port,
        )

    def _configure_world(self, world: Any) -> None:
        """把世界切换到同步模式（固定 50Hz），与场景循环帧率保持一致。"""
        try:
            settings = world.get_settings()
        except Exception as exc:  # noqa: BLE001  Mock/受限 world 无设置接口
            logger.warning(f"Cannot read world settings: {exc}")
            return
        if settings is None:
            return
        try:
            settings.synchronous_mode = True
            settings.fixed_delta_seconds = self._fixed_delta_seconds
            world.apply_settings(settings)
            logger.info(
                f"World switched to synchronous mode ({1.0 / self._fixed_delta_seconds:.0f}Hz)"
            )
        except Exception as exc:  # noqa: BLE001  同步模式失败时退回异步推进
            logger.warning(f"Cannot enable synchronous mode: {exc}")


__all__ = ["ExecutionPlane", "ExecutionUnavailable", "MapManagerResolver"]
