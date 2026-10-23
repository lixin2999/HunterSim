"""多用户体系路由（V2.8.0 P2-13）。

提供：
- ``POST   /api/v1/sim/auth/users``：创建用户（admin）；
- ``GET    /api/v1/sim/auth/users``：列出用户（admin，可按租户过滤）；
- ``GET    /api/v1/sim/auth/users/{username}``：查询用户详情（admin）；
- ``PATCH  /api/v1/sim/auth/users/{username}``：改角色/租户/口令/禁用（admin）；
- ``DELETE /api/v1/sim/auth/users/{username}``：删除用户（admin）；
- ``POST   /api/v1/sim/auth/password``：当前登录用户自助改口令。

设计口径：
- 口令以 PBKDF2-HMAC-SHA256 摘要 + 随机 salt 入库（``StateStore``），明文只在请求体中 transient 存在；
- 用户自带 ``tenant`` 与 ``role``，登录时写入 JWT 声明，审计 ``created_by`` 由此落到具体人；
- 内置管理员（``API_ADMIN_USERNAME``）不入库，作为引导账号保留（见 ``auth.py`` 登录回退）；
- 所有变更写审计日志并打点 ``huntersim_user_events_total``；
- 依赖状态库：``STATE_ENABLED=false`` 时返回 503（用户表必须持久化）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool

from hunter_sim.api.deps import current_principal, get_settings, require_admin
from hunter_sim.api.middleware.logging import audit_log
from hunter_sim.api.models import (
    ApiResponse,
    PasswordChangeRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from hunter_sim.common.metrics import USER_EVENTS_TOTAL
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()

# 允许的用户角色（与 deps._ROLE_LEVELS / API Key 角色口径一致）
_VALID_ROLES = ("viewer", "operator", "admin")


def _require_store(request: Request) -> Any:
    """获取状态库；未启用时返回 503（用户表必须持久化才能跨重启生效）。"""
    store = getattr(request.app.state, "state_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="State persistence is required for user management; set STATE_ENABLED=true",
        )
    return store


def _ensure_users_enabled() -> None:
    """用户体系开关（PLATFORM_USERS_ENABLED=false 时管理接口整体关闭）。"""
    if not get_settings().platform.users_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User management is disabled (PLATFORM_USERS_ENABLED=false)",
        )


def _check_password_policy(username: str, password: str) -> None:
    """口令强度校验：长度 + 字符类别 + 不得与登录名相同。

    Raises:
        HTTPException: 不满足策略时返回 422（detail 指明原因）。
    """
    platform = get_settings().platform
    min_length = platform.min_password_length
    if len(password) < min_length:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Password must be at least {min_length} characters (PLATFORM_MIN_PASSWORD_LENGTH)",
        )
    if not (any(ch.isalpha() for ch in password) and any(ch.isdigit() for ch in password)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must contain at least one letter and one digit",
        )
    if password == username or username.lower() in password.lower():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must not be equal to or contain the username",
        )


def _public_view(record: dict[str, Any]) -> dict[str, Any]:
    """把用户记录裁剪为对外视图（去掉摘要/salt/迭代次数）。"""
    return {
        "username": record["username"],
        "tenant": record["tenant"],
        "role": record["role"],
        "created_by": record.get("created_by", ""),
        "created_at": float(record.get("created_at", 0.0)),
        "updated_at": float(record.get("updated_at", 0.0)),
        "last_login_at": float(record.get("last_login_at", 0.0)),
        "disabled": float(record.get("disabled_at", 0.0)) > 0,
    }


def _caller_context(request: Request) -> tuple[str, str]:
    """取调用主体（登录名, 租户）；内置管理员登录时同样可从 JWT 声明取得。"""
    principal = current_principal(request)
    tenant = principal.tenant or get_settings().platform.default_tenant
    return principal.user_id, tenant


async def _guard_last_admin(store: Any, username: str, operation: str) -> None:
    """避免把最后一个可用 admin 降级/禁用/删除导致平台失去管理能力。

    内置管理员（API_ADMIN_USERNAME）不计入用户表，因此库里 0 个 admin 时
    仍可通过内置账号收尾，不在此处拦截。

    V2.10.0：改为 async，数据库读取经线程池，避免阻塞事件循环。
    """
    builtin_admin = get_settings().api.admin_username
    if username == builtin_admin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User '{username}' is the built-in admin (configured via API_ADMIN_USERNAME) and cannot be modified here",
        )
    users = await run_in_threadpool(store.list_users)
    admins = [item for item in users if item["role"] == "admin" and not item["disabled"]]
    target = await run_in_threadpool(store.get_user, username)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User '{username}' not found")
    if target["role"] == "admin" and len(admins) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Refusing to {operation} the last enabled admin user '{username}' (lockout protection)",
        )


@router.post(
    "/auth/users",
    response_model=ApiResponse,
    summary="创建平台用户",
    description=(
        "创建一个可登录 ``POST /auth/token`` 的用户（admin 限定）。"
        "口令按 PBKDF2-HMAC-SHA256 摘要入库，长度与字符类别受 "
        "``PLATFORM_MIN_PASSWORD_LENGTH`` 约束；``tenant`` 留空继承调用主体租户。"
    ),
)
async def create_user(
    body: UserCreateRequest,
    request: Request,
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """POST /api/v1/sim/auth/users"""
    _ensure_users_enabled()
    store = _require_store(request)
    caller, caller_tenant = _caller_context(request)
    _check_password_policy(body.username, body.password)
    tenant = body.tenant or caller_tenant
    iterations = get_settings().platform.password_hash_iterations

    if await run_in_threadpool(store.get_user, body.username) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User '{body.username}' already exists",
        )
    record = await run_in_threadpool(
        store.create_user,
        username=body.username,
        password=body.password,
        role=body.role,
        tenant=tenant,
        created_by=caller,
        iterations=iterations,
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to persist user '{body.username}'",
        )
    USER_EVENTS_TOTAL.labels(event="create").inc()
    audit_log(
        "auth.user.create",
        caller,
        body.username,
        detail=f"role={body.role}, tenant={tenant}",
        tenant=caller_tenant,
        auth_type=getattr(current_principal(request), "auth_type", ""),
    )
    logger.info(f"User created: '{body.username}' (role={body.role}, tenant={tenant}, by={caller})")
    return ApiResponse(data=_public_view(record))


@router.get(
    "/auth/users",
    response_model=ApiResponse,
    summary="列出平台用户",
    description="列出用户元数据（不含口令摘要）；admin 可指定 tenant 过滤，留空返回全量。",
)
async def list_users(
    request: Request,
    tenant: str = Query("", description="租户 ID（留空返回全部租户）"),
    include_disabled: bool = Query(True, description="是否包含已禁用用户"),
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """GET /api/v1/sim/auth/users"""
    _ensure_users_enabled()
    store = _require_store(request)
    items = [
        _public_view(item)
        for item in await run_in_threadpool(store.list_users, tenant)
        if include_disabled or not item["disabled"]
    ]
    return ApiResponse(data={"items": items, "total": len(items)})


@router.get(
    "/auth/users/{username}",
    response_model=ApiResponse,
    summary="查询用户详情",
    description="按登录名查询用户元数据（admin 限定）。",
)
async def get_user(
    username: str,
    request: Request,
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """GET /api/v1/sim/auth/users/{username}"""
    _ensure_users_enabled()
    store = _require_store(request)
    record = await run_in_threadpool(store.get_user, username)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{username}' not found",
        )
    return ApiResponse(data=_public_view(record))


@router.patch(
    "/auth/users/{username}",
    response_model=ApiResponse,
    summary="更新用户（角色/租户/口令/禁用）",
    description=(
        "部分更新用户（admin 限定）。``password`` 为重置口令（会做强度校验）；"
        "``disabled=true`` 后无法登录，已签发 Token 靠自然过期或 ``/auth/revoke`` 失效。"
        "最后一名可用 admin 不允许被降级/禁用/删除（锁死保护）。"
    ),
)
async def update_user(
    username: str,
    body: UserUpdateRequest,
    request: Request,
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """PATCH /api/v1/sim/auth/users/{username}"""
    _ensure_users_enabled()
    store = _require_store(request)
    caller, caller_tenant = _caller_context(request)
    if body.role is not None and body.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid role '{body.role}' (expected one of {list(_VALID_ROLES)})",
        )
    if body.password is not None:
        _check_password_policy(username, body.password)
    if body.disabled or (body.role is not None and body.role != "admin"):
        await _guard_last_admin(store, username, "disable" if body.disabled else "downgrade")

    record = await run_in_threadpool(
        store.update_user,
        username,
        role=body.role,
        tenant=body.tenant,
        password=body.password,
        disabled=body.disabled,
        iterations=get_settings().platform.password_hash_iterations,
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{username}' not found",
        )
    changed = ",".join(
        name
        for name, value in (
            ("role", body.role),
            ("tenant", body.tenant),
            ("password", body.password),
            ("disabled", body.disabled),
        )
        if value is not None
    )
    USER_EVENTS_TOTAL.labels(event="update").inc()
    audit_log(
        "auth.user.update",
        caller,
        username,
        detail=f"fields={changed or 'none'}",
        tenant=caller_tenant,
        auth_type=getattr(current_principal(request), "auth_type", ""),
    )
    logger.info(f"User updated: '{username}' (fields={changed or 'none'}, by={caller})")
    return ApiResponse(data=_public_view(record))


@router.delete(
    "/auth/users/{username}",
    response_model=ApiResponse,
    summary="删除平台用户",
    description="硬删除用户（admin 限定）。最后一名可用 admin 不允许删除；用户已签发的 Token 自然过期。",
)
async def delete_user(
    username: str,
    request: Request,
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """DELETE /api/v1/sim/auth/users/{username}"""
    _ensure_users_enabled()
    store = _require_store(request)
    caller, caller_tenant = _caller_context(request)
    await _guard_last_admin(store, username, "delete")
    if not await run_in_threadpool(store.delete_user, username):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{username}' not found",
        )
    USER_EVENTS_TOTAL.labels(event="delete").inc()
    audit_log(
        "auth.user.delete",
        caller,
        username,
        tenant=caller_tenant,
        auth_type=getattr(current_principal(request), "auth_type", ""),
    )
    logger.info(f"User deleted: '{username}' (by={caller})")
    return ApiResponse(data={"username": username, "deleted": True})


@router.post(
    "/auth/password",
    response_model=ApiResponse,
    summary="修改当前用户口令",
    description=(
        "自助改口令：需携带本人 Bearer Token 并提供当前口令（``old_password``）。"
        "仅支持用户表账号；内置管理员与服务账号 API Key 不在此接口管理。"
    ),
)
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
) -> ApiResponse:
    """POST /api/v1/sim/auth/password"""
    _ensure_users_enabled()
    store = _require_store(request)
    principal = current_principal(request)
    if principal.auth_type != "jwt":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Password change requires a user login (bearer token)",
        )
    username = principal.user_id
    record = await run_in_threadpool(store.get_user, username)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Account '{username}' is not stored in the user table "
                "(built-in admin or service account); manage it via API_ADMIN_PASSWORD / API keys"
            ),
        )
    verified = await run_in_threadpool(
        store.verify_password, body.old_password, str(record["pass_hash"])
    )
    if not verified:
        USER_EVENTS_TOTAL.labels(event="password_change_denied").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )
    _check_password_policy(username, body.new_password)
    updated = await run_in_threadpool(
        store.update_user,
        username,
        password=body.new_password,
        iterations=get_settings().platform.password_hash_iterations,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{username}' not found",
        )
    USER_EVENTS_TOTAL.labels(event="password_change").inc()
    audit_log(
        "auth.user.password_change",
        username,
        username,
        tenant=principal.tenant,
        auth_type="jwt",
    )
    logger.info(f"Password changed by user '{username}'")
    return ApiResponse(data={"username": username, "changed": True})


__all__ = ["router"]
