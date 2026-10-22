"""审计日志查询路由（V2.5.0 平台化）。

``GET /api/v1/sim/audit`` 分页查询审计事件（新→旧），仅管理员可见。
依赖状态库（``STATE_ENABLED=true``）且 ``PLATFORM_AUDIT_ENABLED=true``。
``tenant=*`` 表示跨租户全局查询。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from hunter_sim.api.deps import current_principal, get_settings, require_admin
from hunter_sim.api.models import ApiResponse
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.get("/audit", response_model=ApiResponse, summary="查询审计日志")
async def list_audit_events(
    request: Request,
    _: str = Depends(require_admin),
    tenant: str = Query("", description="租户 ID（* 表示全部租户）"),
    action: str = Query("", description="操作类型过滤，如 instance.create"),
    user_id: str = Query("", description="操作用户过滤"),
    limit: int = Query(100, ge=1, le=500, description="每页条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
) -> ApiResponse:
    """GET /api/v1/sim/audit"""
    platform = get_settings().platform
    if not platform.audit_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audit logging is disabled (PLATFORM_AUDIT_ENABLED=false)",
        )
    store: Any = getattr(request.app.state, "state_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit query requires state persistence; set STATE_ENABLED=true",
        )
    caller = current_principal(request)
    scope = "" if tenant == "*" else (tenant or caller.tenant)
    data = store.list_audit(
        tenant=scope, action=action, user_id=user_id, limit=limit, offset=offset
    )
    data["tenant"] = scope or "*"
    return ApiResponse(data=data)


__all__ = ["router"]
