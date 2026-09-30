"""批量场景测试路由（设计文档 §12.1、§9.5、§11.2）。

路由组：/scenarios
- POST /scenarios/batch          创建批量场景测试任务（场景集）
- GET  /scenarios/{task_id}/report  获取批量测试汇总报告

批量执行由 app.state.scenario_batch_executor 注入的可执行器完成
（callable(task: dict) -> None，回写 task["status"] 与 task["results"]），
未注入时任务保持 pending 状态，便于调度器异步领取执行。
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status

from hunter_sim.api.deps import get_current_user
from hunter_sim.api.models import ApiResponse, BatchScenarioRequest
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()

# 内存任务注册表（生产环境可替换为数据库/队列存储）
_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()


def get_batch_tasks() -> dict[str, dict[str, Any]]:
    """返回批量任务注册表（供执行器与测试访问）。"""
    return _tasks


@router.post("/batch", response_model=ApiResponse, summary="创建批量场景测试任务")
async def create_batch_task(
    body: BatchScenarioRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """POST /api/v1/sim/scenarios/batch（文档 §11.2 SIL 批量测试流程）

    将多个场景组合为测试集并注册执行任务；若已注入批量执行器，
    任务在后台依次执行场景并回填评估结果。
    """
    task_id = body.task_id or f"batch_{uuid.uuid4().hex[:12]}"
    task: dict[str, Any] = {
        "task_id": task_id,
        "instance_id": body.instance_id,
        "status": "pending",
        "scenes": body.scenes,
        "results": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _tasks_lock:
        if task_id in _tasks:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Batch task '{task_id}' already exists",
            )
        _tasks[task_id] = task

    executor = getattr(request.app.state, "scenario_batch_executor", None)
    if callable(executor):
        task["status"] = "running"
        background_tasks.add_task(executor, task)
    logger.info(f"Batch task registered: {task_id} ({len(body.scenes)} scenes)")

    return ApiResponse(
        data={"task_id": task_id, "status": task["status"], "total_scenes": len(body.scenes)}
    )


@router.get("/{task_id}/report", response_model=ApiResponse, summary="获取批量测试报告")
async def get_batch_report(
    task_id: str,
    _: str = Depends(get_current_user),
) -> ApiResponse:
    """GET /api/v1/sim/scenarios/{task_id}/report（文档 §9.5 汇总报告）

    返回任务状态、整体通过率与逐场景结果；未完成评估时仅返回任务进度。
    """
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch task '{task_id}' not found",
        )

    results = task.get("results", [])
    data: dict[str, Any] = {
        "task_id": task_id,
        "status": task["status"],
        "created_at": task["created_at"],
        "total_scenes": len(task["scenes"]),
        "evaluated": len(results),
        "results": results,
    }
    if results:
        passed = sum(1 for r in results if r.get("passed"))
        data["pass_rate"] = round(passed / len(results), 4)
        data["failed_scenes"] = [
            r.get("scene_id", "") for r in results if not r.get("passed")
        ]
    return ApiResponse(data=data)
