"""平台概览路由（V2.5.0 平台化）。

``GET /api/v1/sim/platform/overview`` 汇总租户视角的平台运行态：
实例与配额、场景库、批量任务、服务账号、执行面与持久化状态。仅管理员可见。
``tenant=*`` 表示跨租户全局统计。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from hunter_sim.api.deps import current_principal, get_settings, require_admin
from hunter_sim.api.models import ApiResponse
from hunter_sim.api.runtime import describe_execution
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.get("/platform/overview", response_model=ApiResponse, summary="平台概览")
async def platform_overview(
    request: Request,
    _: str = Depends(require_admin),
    tenant: str = Query("", description="租户 ID（* 表示全部租户）"),
) -> ApiResponse:
    """GET /api/v1/sim/platform/overview"""
    settings = get_settings()
    platform = settings.platform
    caller = current_principal(request)
    scope = "" if tenant == "*" else (tenant or caller.tenant)

    state = request.app.state
    mgr: Any = getattr(state, "instance_manager", None)
    store: Any = getattr(state, "state_store", None)
    quota_mgr: Any = getattr(state, "quota_manager", None)

    instances = mgr.list_instances(tenant=scope) if mgr is not None else []
    instance_status = Counter(i.status.value for i in instances)
    instance_tenants = Counter(i.tenant for i in instances)

    library_items = (
        store.list_library_scenes(scope, include_archived=False) if store is not None else []
    )
    library_tenants = Counter(item["tenant"] for item in library_items)

    tasks = store.load_batch_tasks(scope) if store is not None else []
    task_status = Counter(str(t.get("status", "pending")) for t in tasks)

    keys = store.list_api_keys(scope) if store is not None else []
    active_keys = [k for k in keys if float(k.get("revoked_at", 0.0)) <= 0]

    quota: dict[str, Any] = (
        quota_mgr.get_usage()
        if quota_mgr is not None
        else {"total": 0, "max_total": settings.resource.max_instances_total, "users": {}}
    )

    data: dict[str, Any] = {
        "tenant": scope or "*",
        "default_tenant": platform.default_tenant,
        "limits": {
            "max_instances_per_tenant": platform.max_instances_per_tenant,
            "max_instances_per_user": settings.resource.max_instances_per_user,
            "max_instances_total": settings.resource.max_instances_total,
        },
        "instances": {
            "total": len(instances),
            "by_status": dict(instance_status),
            "by_tenant": dict(instance_tenants),
        },
        "quota": quota,
        "scene_library": {
            "total": len(library_items),
            "by_tenant": dict(library_tenants),
        },
        "batch_tasks": {
            "total": len(tasks),
            "by_status": dict(task_status),
        },
        "api_keys": {
            "total": len(keys),
            "active": len(active_keys),
        },
        "features": {
            "api_keys": platform.api_keys_enabled,
            "audit": platform.audit_enabled,
        },
        "persistence": {
            "enabled": store is not None,
            "db_path": getattr(store, "path", None),
            "restored_instances": int(getattr(state, "restored_instance_count", 0)),
        },
        "execution": describe_execution(request.app),
    }
    return ApiResponse(data=data)


__all__ = ["router"]
