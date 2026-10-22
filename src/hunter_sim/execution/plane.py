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


class ExecutionPlane:
    """场景执行平面。

    Args:
        pool: 引擎租约池。
        map_manager_resolver: 端点 → MapManager 解析器（测试/多引擎实现注入点）。
        runner_factory: 运行器工厂（默认 SceneRunnerService）。
        lock: 装配互斥锁（与 API 层共享同一把锁，防止并发重复 load_world）。
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
        self._lock = lock or threading.RLock()
        self._fixed_delta_seconds = fixed_delta_seconds
        self._runners: dict[str, Any] = runners if runners is not None else {}
        self._runner_engines: dict[str, str] = {}

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
        with self._lock:
            existing = self._runners.get(instance_id)
            if existing is not None:
                return existing

            endpoint = self._endpoint_of(instance)
            try:
                lease = self._pool.acquire(instance_id, endpoint, instance.map_id)
            except Exception as exc:  # EngineUnavailable 等 → 统一为执行面不可用
                raise ExecutionUnavailable(str(exc)) from exc

            map_manager = self._resolve_map_manager(lease.endpoint)
            if map_manager is None:
                self._pool.release(instance_id)
                raise ExecutionUnavailable(
                    f"No CARLA client available for engine '{lease.endpoint.key}' "
                    f"(target {lease.endpoint.host}:{lease.endpoint.rpc_port})"
                )

            try:
                world = map_manager.load_map(instance.map_id)
                self._configure_world(world)
                runner = self._runner_factory(
                    world,
                    on_state_change=on_state_change,
                    on_event=on_event,
                )
            except Exception as exc:
                self._pool.release(instance_id)
                raise ExecutionUnavailable(
                    f"Failed to assemble scene runtime on engine "
                    f"'{lease.endpoint.key}' for map '{instance.map_id}': {exc}"
                ) from exc

            self._runners[instance_id] = runner
            self._runner_engines[instance_id] = lease.endpoint.key

        logger.info(
            f"Execution plane assembled: instance={instance_id}, "
            f"engine={lease.endpoint.key}, map={instance.map_id}"
        )
        return runner

    def release(self, instance_id: str) -> bool:
        """释放实例运行器并归还引擎租约，返回是否确实释放。"""
        with self._lock:
            runner = self._runners.pop(instance_id, None)
            self._runner_engines.pop(instance_id, None)
            released = self._pool.release(instance_id)
        if runner is None:
            return released
        try:
            runner.destroy()
        except Exception as exc:  # noqa: BLE001  释放失败不应阻塞实例销毁
            logger.warning(f"Scene runtime release failed for instance {instance_id}: {exc}")
        logger.info(f"Execution plane released: instance={instance_id}")
        return True

    def release_all(self) -> int:
        """释放全部运行器（服务关停），返回释放数量。"""
        with self._lock:
            instance_ids = list(self._runners)
        released = 0
        for instance_id in instance_ids:
            if self.release(instance_id):
                released += 1
        return released

    def get_runner(self, instance_id: str) -> Any | None:
        """查询已装配运行器（未装配返回 None）。"""
        return self._runners.get(instance_id)

    def snapshot(self) -> dict[str, Any]:
        """执行面快照（引擎、租约、运行器）。"""
        payload = self._pool.snapshot()
        with self._lock:
            payload["runners"] = [
                {
                    "instance_id": instance_id,
                    "engine": self._runner_engines.get(instance_id),
                }
                for instance_id in self._runners
            ]
        return payload

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
