"""场景库路由（V2.5.0 平台化）。

路由组：/scenes/library
- POST   /scenes/library               保存场景（同名自动递增版本）
- GET    /scenes/library               列出租户场景
- GET    /scenes/library/{id}          读取场景（含完整配置）
- DELETE /scenes/library/{id}          归档场景（软删除）

租户隔离：非 admin 只能访问自己租户的条目；保存/归档写入审计日志。
依赖状态库（``STATE_ENABLED=true``）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from hunter_sim.api.deps import Principal, get_principal, require_operator
from hunter_sim.api.middleware.logging import audit_log
from hunter_sim.api.models import ApiResponse, SceneLibraryCreateRequest
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _require_store(request: Request) -> Any:
    """获取状态库；未启用时返回 503。"""
    store = getattr(request.app.state, "state_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scene library requires state persistence; set STATE_ENABLED=true",
        )
    return store


def _tenant_scope(principal: Principal, tenant: str) -> str:
    """解析目标租户：非 admin 不允许跨租户，admin 可显式指定。"""
    if tenant and tenant != principal.tenant:
        if not principal.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admin can access another tenant's scene library",
            )
        return tenant
    return tenant or principal.tenant


@router.post(
    "/library",
    response_model=ApiResponse,
    status_code=status.HTTP_201_CREATED,
    summary="保存场景到场景库",
    description="保存完整场景配置；同租户同名场景自动递增版本并归档旧版本。",
)
async def save_library_scene(
    body: SceneLibraryCreateRequest,
    request: Request,
    principal: Principal = Depends(require_operator),
    tenant: str = Query("", description="目标租户（仅 admin 可指定）"),
) -> ApiResponse:
    """POST /api/v1/sim/scenes/library"""
    from hunter_sim.scene_runner.scene_config import SceneConfig  # noqa: PLC0415

    try:
        config_model = SceneConfig(**body.config)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Scene config validation failed: {exc}",
        ) from exc

    target_tenant = _tenant_scope(principal, tenant)
    store = _require_store(request)
    scene_id = body.scene_id or config_model.scene_id
    config = dict(body.config)
    config["scene_id"] = scene_id
    item = store.save_library_scene(
        tenant=target_tenant,
        name=body.name,
        scene_id=scene_id,
        map_id=config_model.map_id,
        description=body.description,
        config=config,
        created_by=principal.user_id,
    )
    audit_log(
        "scene.library.save",
        principal.user_id,
        str(item["library_scene_id"]),
        detail=f"name={body.name} version={item['version']} tenant={target_tenant}",
        tenant=target_tenant,
        auth_type=principal.auth_type,
    )
    logger.info(
        f"Scene library saved: {item['library_scene_id']} "
        f"(name={body.name}, version={item['version']}, tenant={target_tenant})"
    )
    # 响应不回传完整配置，避免大响应
    item = {k: v for k, v in item.items() if k != "config"}
    return ApiResponse(data=item)


@router.get(
    "/library",
    response_model=ApiResponse,
    summary="列出场景库",
    description="列出当前租户场景库（默认不含历史版本）；admin 可指定 tenant。",
)
async def list_library_scenes(
    request: Request,
    principal: Principal = Depends(get_principal),
    tenant: str = Query("", description="租户 ID（仅 admin 可跨租户）"),
    include_archived: bool = Query(False, description="是否包含历史版本"),
) -> ApiResponse:
    """GET /api/v1/sim/scenes/library"""
    store = _require_store(request)
    items = store.list_library_scenes(
        _tenant_scope(principal, tenant), include_archived=include_archived
    )
    return ApiResponse(data={"items": items, "total": len(items)})


@router.get(
    "/library/{library_scene_id}",
    response_model=ApiResponse,
    summary="读取场景库条目",
    description="读取单个场景库条目（含完整场景配置），用于查看或下发到实例。",
)
async def get_library_scene(
    library_scene_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> ApiResponse:
    """GET /api/v1/sim/scenes/library/{library_scene_id}"""
    store = _require_store(request)
    # admin 可读取任意租户条目（平台运维），普通用户仅限本租户
    item = store.get_library_scene(
        library_scene_id, tenant="" if principal.is_admin else principal.tenant
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Library scene '{library_scene_id}' not found",
        )
    return ApiResponse(data=item)


@router.delete(
    "/library/{library_scene_id}",
    response_model=ApiResponse,
    summary="归档场景库条目",
    description="软删除（archived=true），历史版本与已下发实例不受影响。",
)
async def delete_library_scene(
    library_scene_id: str,
    request: Request,
    principal: Principal = Depends(require_operator),
    tenant: str = Query("", description="租户 ID（仅 admin 可跨租户）"),
) -> ApiResponse:
    """DELETE /api/v1/sim/scenes/library/{library_scene_id}"""
    store = _require_store(request)
    target_tenant = _tenant_scope(principal, tenant)
    if not store.delete_library_scene(library_scene_id, tenant=target_tenant):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Library scene '{library_scene_id}' not found",
        )
    audit_log(
        "scene.library.delete",
        principal.user_id,
        library_scene_id,
        detail=f"tenant={target_tenant}",
        tenant=target_tenant,
        auth_type=principal.auth_type,
    )
    return ApiResponse(data={"library_scene_id": library_scene_id, "archived": True})


__all__ = ["router"]
