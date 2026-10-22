"""批量场景测试路由（设计文档 §12.1、§9.5、§11.2；V2.5.0 持久化）。

路由组：/scenarios
- POST /scenarios/batch          创建批量场景测试任务（场景集）
- GET  /scenarios               列出本租户批量任务（任务中心）
- GET  /scenarios/{task_id}/report  获取批量测试汇总报告

V2.5.0 起任务双写状态库（``batch_tasks``）：内存保留热数据，服务重启后
可从库中读回报告（未完成任务在启动时归一为 failed）。
批量执行由 app.state.scenario_batch_executor 注入的可执行器完成
（callable(task: dict) -> None，回写 task["status"] 与 task["results"]），
未注入时任务保持 pending 状态，便于调度器异步领取执行。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status

from hunter_sim.api.deps import Principal, get_principal, require_operator
from hunter_sim.api.middleware.logging import audit_log
from hunter_sim.api.models import ApiResponse, BatchScenarioRequest
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()

# 内存任务热缓存（状态库为权威存储，V2.5.0）
_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()

# 审查项 P：任务表有界，防止长期运行内存单调增长
_MAX_TASKS = 1000


def _evict_oldest_finished_locked() -> Optional[str]:
    """【需在 _tasks_lock 内调用】按插入顺序淘汰一个非 running 任务，返回被淘汰的 task_id。"""
    for tid, t in _tasks.items():
        if t.get("status") != "running":
            del _tasks[tid]
            return tid
    return None


def get_batch_tasks() -> dict[str, dict[str, Any]]:
    """返回批量任务内存缓存（供执行器与测试访问）。"""
    return _tasks


def _persist_task(store: Any, task: dict[str, Any]) -> None:
    """尽力写入状态库（失败只告警，不影响任务执行）。"""
    if store is None:
        return
    try:
        store.save_batch_task(task)
    except Exception as exc:  # noqa: BLE001  存储故障降级为内存态
        logger.warning(f"Persist batch task {task.get('task_id')} failed: {exc}")


def _run_executor(executor: Any, task: dict[str, Any], store: Any) -> None:
    """执行批量任务并在结束时回写状态库（后台线程）。"""
    try:
        executor(task)
    finally:
        _persist_task(store, task)


def _get_task(request: Request, task_id: str) -> Optional[dict[str, Any]]:
    """读取任务：内存优先，未命中时从状态库读回（重启后报告不丢失）。"""
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is not None:
        return task
    store = getattr(request.app.state, "state_store", None)
    if store is None:
        return None
    persisted: Optional[dict[str, Any]] = store.load_batch_task(task_id)
    if persisted is None:
        return None
    with _tasks_lock:
        if len(_tasks) < _MAX_TASKS:
            _tasks[task_id] = persisted
    return persisted


def _tenant_of(task: dict[str, Any], fallback: str) -> str:
    """任务租户（旧内存任务无 tenant 字段时回落到调用方租户）。"""
    return str(task.get("tenant") or fallback)


@router.post("/batch", response_model=ApiResponse, summary="创建批量场景测试任务")
async def create_batch_task(
    body: BatchScenarioRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_operator),
) -> ApiResponse:
    """POST /api/v1/sim/scenarios/batch（文档 §11.2 SIL 批量测试流程）

    将多个场景组合为测试集并注册执行任务；若已注入批量执行器，
    任务在后台依次执行场景并回填评估结果。任务同步写入状态库。
    """
    user_id = principal.user_id
    task_id = body.task_id or f"batch_{uuid.uuid4().hex[:12]}"
    task: dict[str, Any] = {
        "task_id": task_id,
        "instance_id": body.instance_id,
        "status": "pending",
        "scenes": body.scenes,
        "results": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        # 审查项 P：记录任务归属，报告端点仅允许创建者查看
        "owner": user_id,
        "tenant": principal.tenant,
    }
    with _tasks_lock:
        if task_id in _tasks or _get_persisted_exists(request, task_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Batch task '{task_id}' already exists",
            )
        if len(_tasks) >= _MAX_TASKS and _evict_oldest_finished_locked() is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Batch task registry full ({_MAX_TASKS} running tasks)",
            )
        executor = getattr(request.app.state, "scenario_batch_executor", None)
        if callable(executor):
            # 锁内写状态：与报告端点的锁内读快照互斥，避免竞态下状态/进度不一致
            task["status"] = "running"
        _tasks[task_id] = task

    store = getattr(request.app.state, "state_store", None)
    _persist_task(store, task)
    if callable(executor):
        background_tasks.add_task(_run_executor, executor, task, store)
    audit_log(
        "scenario.batch.create",
        user_id,
        task_id,
        detail=f"scenes={len(body.scenes)} instance={body.instance_id}",
        tenant=principal.tenant,
        auth_type=principal.auth_type,
    )
    logger.info(f"Batch task registered: {task_id} ({len(body.scenes)} scenes)")

    return ApiResponse(
        data={"task_id": task_id, "status": task["status"], "total_scenes": len(body.scenes)}
    )


def _get_persisted_exists(request: Request, task_id: str) -> bool:
    """状态库中是否已存在同名任务（重启后内存为空时防重复）。"""
    store = getattr(request.app.state, "state_store", None)
    if store is None:
        return False
    try:
        return bool(store.load_batch_task(task_id) is not None)
    except Exception as exc:  # noqa: BLE001  读库失败时退化为仅内存查重
        logger.warning(f"Check persisted batch task {task_id} failed: {exc}")
        return False


@router.get("", response_model=ApiResponse, summary="列出批量测试任务")
async def list_batch_tasks(
    request: Request,
    principal: Principal = Depends(get_principal),
    limit: int = Query(50, ge=1, le=200, description="最多返回条数（新→旧）"),
) -> ApiResponse:
    """GET /api/v1/sim/scenarios

    合并状态库与内存缓存；非 admin 仅返回本人任务，实现租户内任务中心。
    """
    store = getattr(request.app.state, "state_store", None)
    merged: dict[str, dict[str, Any]] = {}
    if store is not None:
        for task in store.load_batch_tasks(principal.tenant):
            tid = str(task.get("task_id", ""))
            if tid:
                merged[tid] = task
    with _tasks_lock:
        for tid, task in _tasks.items():
            if _tenant_of(task, principal.tenant) == principal.tenant:
                merged[tid] = task
    tasks = [t for t in merged.values() if _tenant_of(t, principal.tenant) == principal.tenant]
    if not principal.is_admin:
        tasks = [t for t in tasks if t.get("owner") == principal.user_id]
    tasks.sort(key=lambda t: str(t.get("created_at", "")), reverse=True)
    items = [
        {
            "task_id": t.get("task_id", ""),
            "status": t.get("status", "pending"),
            "instance_id": t.get("instance_id", ""),
            "total_scenes": len(t.get("scenes", [])),
            "evaluated": len(t.get("results", [])),
            "created_at": t.get("created_at", ""),
            "owner": t.get("owner", ""),
            "error_message": t.get("error_message", ""),
        }
        for t in tasks[:limit]
    ]
    return ApiResponse(data={"items": items, "total": len(tasks), "limit": limit})


@router.get("/{task_id}/report", response_model=ApiResponse, summary="获取批量测试报告")
async def get_batch_report(
    task_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> ApiResponse:
    """GET /api/v1/sim/scenarios/{task_id}/report（文档 §9.5 汇总报告）

    返回任务状态、整体通过率与逐场景结果；未完成评估时仅返回任务进度。
    审查项 P：非任务创建者返回 404（避免探测他人任务存在性）；
    V2.5.0：服务重启后从状态库读回，并写回最新快照。
    """
    task = _get_task(request, task_id)
    if (
        task is None
        or _tenant_of(task, principal.tenant) != principal.tenant
        or (task.get("owner") != principal.user_id and not principal.is_admin)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch task '{task_id}' not found",
        )
    with _tasks_lock:
        # 锁内取字段快照，执行器锁外回写不干扰本次读取一致性
        status_value = task.get("status", "pending")
        created_at = task.get("created_at", "")
        total_scenes = len(task.get("scenes", []))
        results = list(task.get("results", []))
        error_message = task.get("error_message", "")

    # write-back：执行器可能只改了内存对象，报告读取顺带回写快照
    _persist_task(getattr(request.app.state, "state_store", None), task)

    data: dict[str, Any] = {
        "task_id": task_id,
        "status": status_value,
        "created_at": created_at,
        "total_scenes": total_scenes,
        "evaluated": len(results),
        "results": results,
    }
    if error_message:
        data["error_message"] = error_message
    if results:
        passed = sum(1 for r in results if r.get("passed"))
        data["pass_rate"] = round(passed / len(results), 4)
        data["failed_scenes"] = [
            r.get("scene_id", "") for r in results if not r.get("passed")
        ]
    return ApiResponse(data=data)
