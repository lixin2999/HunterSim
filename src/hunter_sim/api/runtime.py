"""场景运行时装配（V2.3.0）。

把「实例」与「场景运行器」真正接起来：实例首次下发场景/启动时，
按实例地图加载 CARLA 世界、切换同步模式、创建 ``SceneRunnerService``，
并登记到 ``app.state.scene_runners``（供 scenes/instances 路由查询）。

装配策略：
- 惰性：不在 ``POST /instances`` 里加载地图（避免创建接口被 10~30s 地图加载拖住），
  首次下发场景或启动时才装配；
- 单引擎单地图：一个 CARLA server 只能加载一个地图，已加载其他地图时拒绝装配，
  避免 ``load_world`` 把其他实例的世界重置掉；
- 只降级不中断：CARLA SDK 缺失/不可达时抛 ``SceneRuntimeUnavailable``，
  路由层转换为 503 并给出可诊断原因（对应 ``/health.carla_probe_error``）。
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import HTTPException, Request, status

from hunter_sim.api.deps import get_user_role
from hunter_sim.common.models import InstanceStatus
from hunter_sim.common.utils import get_logger
from hunter_sim.scene_runner.scene_runner_service import SceneRunnerService

logger = get_logger(__name__)

# 管理员角色名（与 deps._ADMIN_ROLE 保持一致）
_ADMIN_ROLE = "admin"

# 场景运行循环帧间隔（50Hz，与 SceneRunnerService 内部 delta 对齐）
_FIXED_DELTA_SECONDS = 0.02


class SceneRuntimeUnavailable(RuntimeError):
    """CARLA 运行时不可用（SDK 缺失、连接失败或引擎地图冲突）。"""


class InstanceNotFound(LookupError):
    """实例不存在。"""


# ─── 注册表访问 ───────────────────────────────────────────────────────────────


def _runners(app: Any) -> dict[str, Any]:
    """获取实例 → 场景运行器注册表（不存在时惰性创建）。"""
    runners = getattr(app.state, "scene_runners", None)
    if runners is None:
        runners = {}
        app.state.scene_runners = runners
    return runners


def _runtime_lock(app: Any) -> threading.Lock:
    """装配互斥锁：防止并发请求重复 load_world / 重复创建运行器。"""
    lock = getattr(app.state, "scene_runtime_lock", None)
    if lock is None:
        lock = threading.Lock()
        app.state.scene_runtime_lock = lock
    return lock


def get_scene_runner(app: Any, instance_id: str) -> Any | None:
    """查询已装配的场景运行器（未装配返回 None）。"""
    return _runners(app).get(instance_id)


# ─── 装配 / 释放 ─────────────────────────────────────────────────────────────


def ensure_scene_runner(app: Any, instance_id: str) -> Any:
    """确保实例已装配场景运行器，返回运行器实例（阻塞：可能触发地图加载）。

    Args:
        app: FastAPI 应用（读取 app.state）。
        instance_id: 仿真实例 ID。

    Returns:
        已装配的 SceneRunnerService。

    Raises:
        InstanceNotFound: 实例不存在。
        SceneRuntimeUnavailable: CARLA 运行时不可用或引擎地图冲突。
    """
    runners = _runners(app)
    runner = runners.get(instance_id)
    if runner is not None:
        return runner

    with _runtime_lock(app):
        runner = runners.get(instance_id)
        if runner is not None:
            return runner

        manager = getattr(app.state, "instance_manager", None)
        if manager is None:
            raise SceneRuntimeUnavailable("实例管理器未初始化（instance_manager）")
        instance = manager.get_instance(instance_id)
        if instance is None:
            raise InstanceNotFound(instance_id)

        client = getattr(app.state, "carla_client", None)
        map_manager = getattr(app.state, "map_manager", None)
        if client is None or map_manager is None:
            reason = getattr(app.state, "carla_probe_error", None) or "CARLA 客户端不可用"
            raise SceneRuntimeUnavailable(str(reason))

        # 单引擎单场景：同一 CARLA 引擎同一时刻只能有一个运行器，
        # 否则两个 tick 线程会互相推进同一 world（帧同步错乱）+ load_world 重置他人 Actor
        occupied = next(iter(runners), None)
        if occupied is not None and occupied != instance_id:
            raise SceneRuntimeUnavailable(
                f"当前 CARLA 引擎已被实例 '{occupied}' 占用（单引擎同一时刻只能运行一个场景）；"
                "请先停止并销毁该实例，或为实例部署独立 CARLA 引擎"
            )

        loaded_map = getattr(app.state, "scene_world_map_id", None)
        if loaded_map is not None and loaded_map != instance.map_id:
            raise SceneRuntimeUnavailable(
                f"当前 CARLA 引擎已加载地图 '{loaded_map}'，无法再加载 '{instance.map_id}'"
                "（单引擎单地图约束）；请先销毁占用实例，或为实例部署独立 CARLA 引擎"
            )

        _enter_loading(manager, instance)
        try:
            world = map_manager.load_map(instance.map_id)
            _configure_world(world)
            runner = SceneRunnerService(world)
            runners[instance_id] = runner
            app.state.scene_world_map_id = instance.map_id
            _enter_ready(manager, instance)
        except Exception as exc:
            _mark_failed(manager, instance_id, exc)
            logger.error(f"Scene runtime assembly failed for instance {instance_id}: {exc}")
            raise

        logger.info(
            f"Scene runtime assembled: instance={instance_id}, map={instance.map_id}, "
            f"carla={instance.carla_host}:{instance.carla_rpc_port}"
        )
        return runner


def release_scene_runner(app: Any, instance_id: str) -> bool:
    """释放实例的场景运行器（停止循环、销毁场景 Actor）。

    Returns:
        True 表示确实释放了一个运行器。
    """
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
    released = 0
    for instance_id in list(_runners(app)):
        if release_scene_runner(app, instance_id):
            released += 1
    return released


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
    """校验实例归属：非本人实例仅管理员可访问（修复实例级 IDOR）。

    Args:
        request: FastAPI 请求（读取 app.state）。
        instance_id: 仿真实例 ID。
        user_id: 当前登录用户。
        require_exists: True 时实例不存在返回 404；False 时跳过（调用方已确认资源存在，
            例如已注入运行器的单测/内部路径）。

    Returns:
        实例对象；实例管理器未就绪、未登录或实例不存在且不强制存在时返回 None。
    """
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


def _configure_world(world: Any) -> None:
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
        settings.fixed_delta_seconds = _FIXED_DELTA_SECONDS
        world.apply_settings(settings)
        logger.info(f"World switched to synchronous mode ({1.0 / _FIXED_DELTA_SECONDS:.0f}Hz)")
    except Exception as exc:  # noqa: BLE001  同步模式失败时退回异步推进
        logger.warning(f"Cannot enable synchronous mode: {exc}")


__all__ = [
    "InstanceNotFound",
    "SceneRuntimeUnavailable",
    "assert_instance_access",
    "ensure_scene_runner",
    "ensure_scene_runner_or_http",
    "get_scene_runner",
    "release_all_scene_runners",
    "release_scene_runner",
]
