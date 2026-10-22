"""场景运行时装配（V2.4.0：执行面分离 + 状态持久化）。

控制面与执行面的边界：

- 控制面（本模块 + 路由）：鉴权、归属校验、实例状态机、HTTP 错误映射；
- 执行面（``execution.ExecutionPlane``）：引擎租约、地图加载、同步模式、
  ``SceneRunnerService`` 的创建/销毁。

装配策略：
- 惰性：不在 ``POST /instances`` 里加载地图，首次下发场景或启动时才装配；
- 引擎独占：一个 CARLA 引擎同一时刻只承载一个运行器；多引擎端点可并行；
- 只降级不中断：CARLA 不可用时抛 ``SceneRuntimeUnavailable``，路由转 503；
- 可恢复：场景下发成功后写入 ``StateStore``；服务重启后实例恢复为 READY，
  再次装配时自动载入持久化场景定义（``EXECUTION_RESTORE_SCENE_ON_ASSEMBLE``）。
"""

from __future__ import annotations

import threading
from typing import Any, Optional, cast

from fastapi import HTTPException, Request, status

from hunter_sim.api.deps import get_user_role
from hunter_sim.common.models import CarlaSettings, InstanceStatus
from hunter_sim.common.utils import get_logger
from hunter_sim.engine.map_manager import MapManager
from hunter_sim.execution.engine_pool import EngineEndpoint, EnginePool, parse_engine_endpoints
from hunter_sim.execution.plane import ExecutionPlane, ExecutionUnavailable

logger = get_logger(__name__)

# 管理员角色名（与 deps._ADMIN_ROLE 保持一致）
_ADMIN_ROLE = "admin"

# 场景运行循环帧间隔（50Hz，与 SceneRunnerService 内部 delta 对齐）
_FIXED_DELTA_SECONDS = 0.02


class SceneRuntimeUnavailable(RuntimeError):
    """CARLA 运行时不可用（SDK 缺失、连接失败或引擎被占用）。"""


class InstanceNotFound(LookupError):
    """实例不存在。"""


# ─── 注册表 / 执行平面访问 ───────────────────────────────────────────────────


def _runners(app: Any) -> dict[str, Any]:
    """获取实例 → 场景运行器注册表（不存在时惰性创建）。"""
    runners = getattr(app.state, "scene_runners", None)
    if runners is None:
        runners = {}
        app.state.scene_runners = runners
    return runners


def _runtime_lock(app: Any) -> threading.RLock:
    """装配互斥锁（可重入：运行时与执行平面共用）。"""
    lock = getattr(app.state, "scene_runtime_lock", None)
    if lock is None:
        lock = threading.RLock()
        app.state.scene_runtime_lock = lock
    return lock


def _resolve_map_manager(app: Any, endpoint: EngineEndpoint) -> Any | None:
    """按端点解析 MapManager。

    优先级：
    1. ``app.state.map_managers``（测试/多引擎注入，键为 endpoint.key 或 host）；
    2. ``app.state.map_manager``（单引擎启动期装配的共享管理器）；
    3. 惰性创建：复用启动期 ``carla_client`` 或按端点新建 ``carla.Client``。
    """
    managers = getattr(app.state, "map_managers", None)
    if isinstance(managers, dict):
        for key in (endpoint.key, endpoint.host, f"{endpoint.host}:{endpoint.rpc_port}"):
            manager = managers.get(key)
            if manager is not None:
                return manager
        return None

    shared = getattr(app.state, "map_manager", None)
    if shared is not None:
        return shared

    cache = getattr(app.state, "execution_map_managers", None)
    if not isinstance(cache, dict):
        cache = {}
        app.state.execution_map_managers = cache
    cached = cache.get(endpoint.key)
    if cached is not None:
        return cached

    settings = getattr(app.state, "settings", None)
    try:
        import carla  # noqa: PLC0415  惰性导入：仅真实 CARLA 环境可用
    except ImportError:
        return None

    try:
        base_client = getattr(app.state, "carla_client", None)
        is_base_endpoint = (
            settings is not None
            and endpoint.host == settings.carla.host
            and endpoint.rpc_port == settings.carla.rpc_port
        )
        if is_base_endpoint and base_client is not None:
            client = base_client
        else:
            client = carla.Client(endpoint.host, endpoint.rpc_port)
            timeout = settings.carla.timeout_seconds if settings is not None else 10.0
            client.set_timeout(timeout)
        manager = MapManager(client)
        cache[endpoint.key] = manager
        return manager
    except Exception as exc:  # noqa: BLE001  端点不可达 → 交由上层转 503
        logger.warning(f"Map manager unavailable for engine {endpoint.key}: {exc}")
        return None


def _execution_plane(app: Any) -> ExecutionPlane:
    """获取（必要时创建）执行平面，绑定到 app.state。"""
    existing = getattr(app.state, "execution_plane", None)
    if existing is not None:
        return cast(ExecutionPlane, existing)

    with _runtime_lock(app):
        existing = getattr(app.state, "execution_plane", None)
        if existing is not None:
            return cast(ExecutionPlane, existing)

        settings = getattr(app.state, "settings", None)
        base = settings.carla if settings is not None else CarlaSettings()
        endpoints = getattr(app.state, "execution_endpoints", None)
        if not isinstance(endpoints, list) or not endpoints:
            spec = settings.execution.engine_endpoints if settings is not None else ""
            max_engines = settings.execution.max_engines if settings is not None else 8
            endpoints = parse_engine_endpoints(spec, base, max_engines)
            app.state.execution_endpoints = endpoints
        delta = getattr(base, "fixed_delta_seconds", _FIXED_DELTA_SECONDS)

        plane = ExecutionPlane(
            EnginePool(endpoints),
            lambda endpoint: _resolve_map_manager(app, endpoint),
            lock=_runtime_lock(app),
            fixed_delta_seconds=delta,
            runners=_runners(app),
        )
        app.state.execution_plane = plane
        logger.info(
            f"Execution plane initialized with {len(endpoints)} engine endpoint(s): "
            f"{', '.join(ep.key for ep in endpoints)}"
        )
        return plane


def get_scene_runner(app: Any, instance_id: str) -> Any | None:
    """查询已装配的场景运行器（未装配返回 None）。"""
    plane = getattr(app.state, "execution_plane", None)
    if plane is not None:
        return plane.get_runner(instance_id)
    return _runners(app).get(instance_id)


# ─── 装配 / 释放 ─────────────────────────────────────────────────────────────


def ensure_scene_runner(app: Any, instance_id: str) -> Any:
    """确保实例已装配场景运行器，返回运行器实例（阻塞：可能触发地图加载）。

    Raises:
        InstanceNotFound: 实例不存在。
        SceneRuntimeUnavailable: CARLA 运行时不可用或引擎已被占用。
    """
    runners = _runners(app)
    runner = get_scene_runner(app, instance_id)
    if runner is not None:
        return runner

    with _runtime_lock(app):
        runner = get_scene_runner(app, instance_id)
        if runner is not None:
            return runner

        manager = getattr(app.state, "instance_manager", None)
        if manager is None:
            raise SceneRuntimeUnavailable("实例管理器未初始化（instance_manager）")
        instance = manager.get_instance(instance_id)
        if instance is None:
            raise InstanceNotFound(instance_id)

        has_manager = (
            getattr(app.state, "map_manager", None) is not None
            or isinstance(getattr(app.state, "map_managers", None), dict)
        )
        if (
            not has_manager
            and getattr(app.state, "carla_client", None) is None
            and not getattr(app.state, "carla_sdk_installed", False)
        ):
            reason = getattr(app.state, "carla_probe_error", None) or "CARLA 客户端不可用"
            raise SceneRuntimeUnavailable(str(reason))

        # 兼容单引擎地图约束（V2.3.0 语义）：引擎已加载其他地图时拒绝装配
        loaded_map = getattr(app.state, "scene_world_map_id", None)
        if loaded_map is not None and loaded_map != instance.map_id and not runners:
            raise SceneRuntimeUnavailable(
                f"当前 CARLA 引擎已加载地图 '{loaded_map}'，无法再加载 '{instance.map_id}'"
                "（单引擎单地图约束）；请先销毁占用实例，或为实例部署独立 CARLA 引擎"
            )

        _enter_loading(manager, instance)
        plane = _execution_plane(app)
        try:
            runner = plane.ensure_runner(
                instance,
                on_state_change=_scene_state_callback(app, instance_id),
            )
            if plane.active_engine_count <= 1:
                app.state.scene_world_map_id = instance.map_id
            _enter_ready(manager, instance)
        except ExecutionUnavailable as exc:
            _mark_failed(manager, instance_id, exc)
            logger.error(f"Scene runtime assembly failed for instance {instance_id}: {exc}")
            raise SceneRuntimeUnavailable(str(exc)) from exc
        except Exception as exc:
            _mark_failed(manager, instance_id, exc)
            logger.error(f"Scene runtime assembly failed for instance {instance_id}: {exc}")
            raise

        _restore_persisted_scene(app, instance_id, runner)

        logger.info(
            f"Scene runtime assembled: instance={instance_id}, map={instance.map_id}, "
            f"carla={getattr(instance, 'carla_host', '?')}:"
            f"{getattr(instance, 'carla_rpc_port', '?')}"
        )
        return runner


def release_scene_runner(app: Any, instance_id: str) -> bool:
    """释放实例的场景运行器（停止循环、销毁场景 Actor、归还引擎租约）。"""
    plane = getattr(app.state, "execution_plane", None)
    if plane is not None:
        released = bool(plane.release(instance_id))
        if plane.active_engine_count == 0:
            app.state.scene_world_map_id = None
        return released

    runners = _runners(app)
    runner = runners.pop(instance_id, None)
    if runner is None:
        return False
    try:
        runner.destroy()
    except Exception as exc:  # noqa: BLE001  释放失败不应阻塞实例销毁
        logger.warning(f"Scene runtime release failed for instance {instance_id}: {exc}")
    if not runners:
        app.state.scene_world_map_id = None
    logger.info(f"Scene runtime released: instance={instance_id}")
    return True


def release_all_scene_runners(app: Any) -> int:
    """释放全部场景运行器（服务关停时调用），返回释放数量。"""
    plane = getattr(app.state, "execution_plane", None)
    if plane is not None:
        released = int(plane.release_all())
        app.state.scene_world_map_id = None
        return released
    released = 0
    for instance_id in list(_runners(app)):
        if release_scene_runner(app, instance_id):
            released += 1
    return released


# ─── 场景持久化 / 状态查询 ───────────────────────────────────────────────────


def persist_scene(app: Any, instance_id: str, config: Any) -> None:
    """持久化场景绑定（下发成功后调用，失败只告警）。"""
    store = getattr(app.state, "state_store", None)
    if store is None:
        return
    try:
        payload = config.model_dump(mode="json") if hasattr(config, "model_dump") else dict(config)
        store.save_scene(
            instance_id=instance_id,
            scene_id=str(payload.get("scene_id", "")),
            map_id=str(payload.get("map_id", "")),
            config=payload,
        )
    except Exception as exc:  # noqa: BLE001  持久化失败不阻塞场景下发
        logger.warning(f"Persist scene for instance {instance_id} failed: {exc}")


def persisted_scene_state(app: Any, instance_id: str) -> dict[str, Any] | None:
    """读取持久化的场景快照（无运行器时用于 status 查询）。"""
    store = getattr(app.state, "state_store", None)
    if store is None:
        return None
    try:
        binding = store.load_scene(instance_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Load persisted scene for instance {instance_id} failed: {exc}")
        return None
    if binding is None:
        return None
    return {
        "scene_id": binding["scene_id"],
        "status": binding["status"],
        "elapsed_seconds": round(float(binding["elapsed_seconds"]), 3),
        "progress": round(float(binding["progress"]), 4),
        "error_message": binding["error_message"] or None,
        "detached": True,
    }


def describe_execution(app: Any) -> dict[str, Any]:
    """执行面快照：引擎库存、租约、运行器与持久化状态。"""
    plane = getattr(app.state, "execution_plane", None)
    if plane is not None:
        payload = cast(dict[str, Any], plane.snapshot())
    else:
        settings = getattr(app.state, "settings", None)
        base = settings.carla if settings is not None else CarlaSettings()
        endpoints = getattr(app.state, "execution_endpoints", None)
        if not isinstance(endpoints, list) or not endpoints:
            spec = settings.execution.engine_endpoints if settings is not None else ""
            max_engines = settings.execution.max_engines if settings is not None else 8
            endpoints = parse_engine_endpoints(spec, base, max_engines)
        payload = {
            "engines": [
                {**ep.to_dict(), "holder": None, "map_id": None, "acquired_at": None}
                for ep in endpoints
            ],
            "capacity": len(endpoints),
            "active_leases": 0,
            "leases": [],
            "runners": [],
        }
    store = getattr(app.state, "state_store", None)
    payload["persistence"] = {
        "enabled": store is not None,
        "db_path": getattr(store, "path", None),
        "restored_instances": int(getattr(app.state, "restored_instance_count", 0)),
    }
    return payload


def _restore_persisted_scene(app: Any, instance_id: str, runner: Any) -> None:
    """装配完成后按需恢复已持久化的场景定义（重启恢复）。"""
    store = getattr(app.state, "state_store", None)
    if store is None:
        return
    settings = getattr(app.state, "settings", None)
    if settings is not None and not settings.execution.restore_scene_on_assemble:
        return
    if getattr(runner, "has_scene", True):
        return
    try:
        binding = store.load_scene(instance_id)
    except Exception as exc:  # noqa: BLE001  读库失败不影响新场景下发
        logger.warning(f"Load persisted scene for instance {instance_id} failed: {exc}")
        return
    if not binding:
        return
    try:
        from hunter_sim.scene_runner.scene_config import SceneConfig  # noqa: PLC0415

        runner.load_scene(SceneConfig(**binding["config"]))
        logger.info(
            f"Restored persisted scene '{binding['scene_id']}' for instance {instance_id} "
            "during runtime assembly"
        )
    except Exception as exc:  # noqa: BLE001  场景恢复失败不阻断装配，等待重新下发
        logger.warning(f"Restore persisted scene for instance {instance_id} failed: {exc}")


def _scene_state_callback(app: Any, instance_id: str) -> Any:
    """场景状态变化 → 更新持久化快照。"""

    def _callback(snapshot: Any) -> None:
        store = getattr(app.state, "state_store", None)
        if store is None:
            return
        try:
            store.update_scene_snapshot(
                instance_id=instance_id,
                status=snapshot.status.value,
                progress=float(getattr(snapshot, "progress", 0.0)),
                elapsed_seconds=float(getattr(snapshot, "elapsed_seconds", 0.0)),
                error_message=str(getattr(snapshot, "error_message", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001  持久化失败只影响快照新鲜度
            logger.debug(f"Persist scene snapshot for instance {instance_id} failed: {exc}")

    return _callback


# ─── 路由辅助 ─────────────────────────────────────────────────────────────────


def ensure_scene_runner_or_http(app: Any, instance_id: str) -> Any:
    """``ensure_scene_runner`` 的 HTTP 适配：不可用 → 503，不存在 → 404。"""
    try:
        return ensure_scene_runner(app, instance_id)
    except InstanceNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance '{instance_id}' not found",
        ) from exc
    except SceneRuntimeUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"CARLA runtime unavailable: {exc}. "
                "请检查 /api/v1/sim/health 的 carla_sdk_installed / carla_probe_error，"
                "或参考 docs/单机部署指南.md 安装 carla==0.9.16 并启动 CARLA 服务端"
            ),
        ) from exc


def assert_instance_access(
    request: Request,
    instance_id: str,
    user_id: str,
    require_exists: bool = True,
) -> Any | None:
    """校验实例归属：非本人实例仅管理员可访问（修复实例级 IDOR）。"""
    manager = getattr(request.app.state, "instance_manager", None)
    if manager is None or not user_id:
        return None
    instance = manager.get_instance(instance_id)
    if instance is None:
        if not require_exists:
            return None
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance '{instance_id}' not found",
        )
    if instance.user_id and instance.user_id != user_id:
        payload = getattr(request.state, "token_payload", None) or {}
        if get_user_role(payload) != _ADMIN_ROLE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Instance '{instance_id}' belongs to another user",
            )
    return instance


# ─── 内部实现 ─────────────────────────────────────────────────────────────────


def _enter_loading(manager: Any, instance: Any) -> None:
    """实例 CREATED → LOADING（其他状态保持不变，交由状态机约束）。"""
    if instance.status == InstanceStatus.CREATED:
        manager.transition(instance.sim_instance_id, InstanceStatus.LOADING)


def _enter_ready(manager: Any, instance: Any) -> None:
    """实例 LOADING → READY。"""
    if instance.status == InstanceStatus.LOADING:
        manager.transition(instance.sim_instance_id, InstanceStatus.READY)


def _mark_failed(manager: Any, instance_id: str, exc: Exception) -> None:
    """装配失败：尽力把实例置为 FAILED（状态机不允许时忽略）。"""
    try:
        manager.transition(instance_id, InstanceStatus.FAILED, str(exc))
    except Exception:  # noqa: BLE001  状态机拒绝（已终态）时仅记录日志
        logger.debug(f"Instance {instance_id} could not transition to FAILED")


__all__ = [
    "InstanceNotFound",
    "SceneRuntimeUnavailable",
    "assert_instance_access",
    "describe_execution",
    "ensure_scene_runner",
    "ensure_scene_runner_or_http",
    "get_scene_runner",
    "persist_scene",
    "persisted_scene_state",
    "release_all_scene_runners",
    "release_scene_runner",
]
