"""认证与平台服务账号路由（V2.3.0 JWT，V2.5.0 API Key）。

提供：
- ``POST /api/v1/sim/auth/token``：用平台内置管理员账号换取 JWT；
- ``POST /api/v1/sim/auth/api-keys``：签发服务账号 API Key（admin）；
- ``GET  /api/v1/sim/auth/api-keys``：列出服务账号（admin）；
- ``DELETE /api/v1/sim/auth/api-keys/{key_id}``：吊销服务账号（admin）。

账号来源：优先用户表（V2.8.0 P2-13，需 ``STATE_ENABLED=true``），未命中回退
``API_ADMIN_USERNAME`` / ``API_ADMIN_PASSWORD``（默认 admin/admin，引导账号，仅限 dev）；
``HUNTER_SIM_ENV=prod`` 时启动闸门会拒绝默认口令。
API Key 依赖状态库（``STATE_ENABLED=true``）：密钥仅存 sha256(salt:secret) 摘要，
明文只在签发响应中返回一次。
"""

from __future__ import annotations

import math
import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool

from hunter_sim.api.deps import (
    Principal,
    create_access_token,
    current_principal,
    get_api_settings,
    get_settings,
    mint_access_token,
    require_admin,
)
from hunter_sim.api.middleware.logging import audit_log
from hunter_sim.api.middleware.rate_limit import LoginFailureGuard
from hunter_sim.api.models import ApiKeyCreateRequest, ApiResponse, TokenRequest, TokenResponse
from hunter_sim.common.metrics import (
    AUTH_TOKEN_EVENTS_TOTAL,
    LOGIN_REJECTIONS_TOTAL,
    USER_EVENTS_TOTAL,
)
from hunter_sim.common.models import APISettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()

# 内置管理员账号的角色（创建/销毁实例等操作需 admin，见 §14.2）
_ADMIN_ROLE = "admin"


def _require_store(request: Request) -> Any:
    """获取状态库；未启用时返回 503（API Key 必须持久化才能跨重启生效）。"""
    store = getattr(request.app.state, "state_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="State persistence is required for API keys; set STATE_ENABLED=true",
        )
    return store


async def _authenticate(
    request: Request, body: TokenRequest, api_settings: APISettings
) -> dict[str, str]:
    """校验登录凭据，返回 ``{username, role, tenant}``；失败抛 401。

    顺序（V2.8.0 P2-13）：先查用户表（PBKDF2 摘要校验，线程池执行以免阻塞事件循环），
    未命中再回退内置管理员比对（V2.3.0 语义），保证无状态库/未建用户时既有客户端不变。
    """
    platform = get_settings().platform
    store = getattr(request.app.state, "state_store", None)
    if store is not None and platform.users_enabled:
        try:
            record: Optional[dict[str, Any]] = await run_in_threadpool(
                store.verify_user, body.username, body.password
            )
        except Exception as exc:  # noqa: BLE001  用户表不可用不阻断内置管理员登录
            logger.warning(f"User table verification unavailable, falling back: {exc}")
            record = None
        if record is not None:
            return {
                "username": str(record["username"]),
                "role": str(record["role"]),
                "tenant": str(record["tenant"] or platform.default_tenant),
            }

    expected_user = api_settings.admin_username
    expected_password = api_settings.admin_password.get_secret_value()
    # 常量时间比较，避免口令被计时侧信道推断；按 UTF-8 字节比较以兼容非 ASCII 用户名
    user_ok = secrets.compare_digest(body.username.encode("utf-8"), expected_user.encode("utf-8"))
    password_ok = secrets.compare_digest(
        body.password.encode("utf-8"),
        expected_password.encode("utf-8"),
    )
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "username": expected_user,
        "role": _ADMIN_ROLE,
        "tenant": platform.default_tenant,
    }


@router.post(
    "/auth/token",
    response_model=ApiResponse,
    summary="获取 JWT 访问令牌",
    description=(
        "用平台账号换取 Bearer Token：优先校对用户表（V2.8.0 P2-13，口令以 PBKDF2 摘要入库），"
        "未命中时回退内置管理员 ``API_ADMIN_USERNAME`` / ``API_ADMIN_PASSWORD``。"
        "返回的 access_token 用于其余接口的 Authorization: Bearer <token>，角色与租户写入 JWT 声明。"
        "V2.6.0：同 IP 连续失败达 API_LOGIN_MAX_FAILED_ATTEMPTS 后进入"
        "指数退避（429 + Retry-After），成功登录即复位。"
    ),
)
async def issue_token(
    body: TokenRequest,
    request: Request,
    api_settings: APISettings = Depends(get_api_settings),
) -> ApiResponse:
    """POST /api/v1/sim/auth/token

    Args:
        body: 用户名/口令。
        request: 当前请求（用于定位登录退避主体 IP 与读取状态库）。
        api_settings: API 配置（内置管理员凭据与 JWT 参数）。

    Returns:
        统一响应体，data 为 TokenResponse 结构。

    Raises:
        HTTPException: 凭据错误返回 401；退避期内拒绝返回 429。
    """
    client_ip = request.client.host if request.client else "unknown"
    guard: Any = getattr(request.app.state, "login_failure_guard", None)
    if not isinstance(guard, LoginFailureGuard):
        guard = None

    # 退避期内直接拒绝（不泄露口令校验结果，不进入凭据比较）
    if guard is not None:
        retry_after = guard.retry_after(client_ip)
        if retry_after is not None:
            LOGIN_REJECTIONS_TOTAL.labels(reason="backoff").inc()
            logger.warning(
                f"Token request from '{client_ip}' rejected by login backoff "
                f"(retry after {retry_after:.1f}s)"
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts; retry later",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )

    try:
        identity = await _authenticate(request, body, api_settings)
    except HTTPException:
        if guard is not None:
            guard.record_failure(client_ip)
        LOGIN_REJECTIONS_TOTAL.labels(reason="credential").inc()
        USER_EVENTS_TOTAL.labels(event="login_denied").inc()
        logger.warning(f"Token request rejected for user '{body.username}'")
        raise

    if guard is not None:
        guard.reset(client_ip)
    token = create_access_token(
        identity["username"],
        api_settings,
        extra_claims={"role": identity["role"]},
        tenant=identity["tenant"],
    )
    USER_EVENTS_TOTAL.labels(event="login").inc()
    audit_log(
        "auth.login",
        identity["username"],
        identity["username"],
        detail=f"role={identity['role']}",
        tenant=identity["tenant"],
        auth_type="jwt",
    )
    logger.info(
        f"Issued access token for '{identity['username']}' "
        f"(role={identity['role']}, tenant={identity['tenant']})"
    )
    payload = TokenResponse(
        access_token=token,
        expires_in=api_settings.jwt_expire_minutes * 60,
    )
    return ApiResponse(data=payload.model_dump())


def _record_disabled(record: dict[str, Any]) -> bool:
    """用户记录是否已禁用（V2.9.0 续期校验）。

    ``StateStore.get_user`` 返回原始行（禁用标记是 ``disabled_at`` 时间戳），
    而 ``list_users`` 另派生布尔 ``disabled``；两种口径都要认，否则禁用账号
    仍能在续期时蒙混过关。字段异常时按“已禁用”处理（fail-closed）。
    """
    if bool(record.get("disabled")):
        return True
    try:
        return float(record.get("disabled_at") or 0.0) > 0.0
    except (TypeError, ValueError):
        return True


async def _resolve_refresh_claims(principal: Principal, store: Any) -> tuple[str, str]:
    """续期前重新确认账号仍有效，返回续期后的 ``(role, tenant)``（V2.9.0）。

    旧实现直接沿用 Token 里的 claim，导致禁用/降级/删除后仍能无限续期，
    账号吊销能力形同虚设。现以用户表当前值为准：

    - ``PLATFORM_USERS_ENABLED=false``：用户体系已关闭，仅内置管理员可续期；
    - 用户表命中：禁用则 401，否则以其 ``role``/``tenant`` 为准（降级即时生效）；
    - 用户表未命中：仅内置管理员（``API_ADMIN_USERNAME``）可续期，其余（已删除用户/
      服务账号）返回 401。

    V2.10.0：数据库读取改为线程池执行，避免阻塞事件循环（与登录路径一致）。

    Raises:
        HTTPException: 账号已禁用、已删除或无权续期（401）。
    """
    platform = get_settings().platform
    admin_username = get_settings().api.admin_username
    if not platform.users_enabled:
        if principal.user_id == admin_username:
            return principal.role or _ADMIN_ROLE, principal.tenant or platform.default_tenant
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "User management is disabled (PLATFORM_USERS_ENABLED=false); "
                "re-login with the built-in admin account is required"
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    record: Optional[dict[str, Any]] = None
    try:
        record = await run_in_threadpool(store.get_user, principal.user_id)
    except Exception as exc:  # noqa: BLE001  用户表不可用时不能“盲续期”
        logger.warning(f"User table unavailable during refresh: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="User table unavailable; retry later or re-login",
        ) from exc
    if record is not None:
        if _record_disabled(record):
            USER_EVENTS_TOTAL.labels(event="login_denied").inc()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account has been disabled; re-login is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return (
            str(record.get("role") or principal.role),
            str(record.get("tenant") or platform.default_tenant),
        )
    if principal.user_id == admin_username:
        return principal.role or _ADMIN_ROLE, principal.tenant or platform.default_tenant
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Account is not stored in the user table (removed or built-in fallback); "
            "re-login is required"
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post(
    "/auth/refresh",
    response_model=ApiResponse,
    summary="滑动续期 JWT 访问令牌",
    description=(
        "用一个当前有效（未过期且未吊销）的 Bearer Token 换取得到已重新签发、"
        "有效期刷新的新 Token；旧 Token 的 ``jti`` 就地吊销（轮换，需状态库）。"
        "用于长时批量任务/WS 监控中途续期，避免重新输入凭据。需 ``API_JWT_REFRESH_ENABLED``"
        "与 ``STATE_ENABLED=true``。"
    ),
)
async def refresh_token(
    request: Request,
    api_settings: APISettings = Depends(get_api_settings),
) -> ApiResponse:
    """POST /api/v1/sim/auth/refresh

    仅接受中间件已认证写入的 ``request.state.token_payload``（含 jti）。

    Raises:
        HTTPException: 未启用续期/无状态库返回 503；旧 Token 被吊销返回 401。
    """
    if not api_settings.jwt_refresh_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token refresh is disabled (API_JWT_REFRESH_ENABLED=false)",
        )
    store = _require_store(request)
    payload = getattr(request.state, "token_payload", None)
    if not isinstance(payload, dict) or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer token is required to refresh",
        )
    old_jti = str(payload.get("jti", ""))
    if old_jti and await run_in_threadpool(store.is_token_revoked, old_jti):
        AUTH_TOKEN_EVENTS_TOTAL.labels(event="revoked_hit").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = current_principal(request)
    role, tenant = await _resolve_refresh_claims(principal, store)
    new_token, new_jti, _ = mint_access_token(
        principal.user_id,
        api_settings,
        extra_claims={"role": role},
        tenant=tenant,
    )
    # 旧 Token 就地吊销（若自然过期则 jti 不再有意义，写入也无害）：best-effort
    if old_jti:
        try:
            await run_in_threadpool(
                store.revoke_token,
                old_jti,
                expires_at=float(payload.get("exp", 0.0) or 0.0),
                user_id=principal.user_id,
            )
        except Exception as exc:  # noqa: BLE001  旧 jti 写入失败不阻断新 Token 签发
            logger.warning(f"Failed to revoke previous token jti on refresh: {exc}")
    AUTH_TOKEN_EVENTS_TOTAL.labels(event="refresh").inc()
    audit_log(
        "auth.token.refresh",
        principal.user_id,
        new_jti,
        detail=f"rotated_from={old_jti or 'none'}",
        tenant=tenant,
        auth_type="jwt",
    )
    logger.info(f"Refreshed access token for '{principal.user_id}' (new jti={new_jti})")
    body = TokenResponse(
        access_token=new_token,
        expires_in=api_settings.jwt_expire_minutes * 60,
    )
    return ApiResponse(data=body.model_dump())


@router.post(
    "/auth/revoke",
    response_model=ApiResponse,
    summary="吊销当前 JWT 访问令牌（退出登录）",
    description=(
        "将当前 Bearer Token 的 ``jti`` 加入吊销名单，立即失效（幂等）。"
        "需 ``STATE_ENABLED=true``；未携带 jti 的旧 Token 无可吊销。"
    ),
)
async def revoke_token(
    request: Request,
    api_settings: APISettings = Depends(get_api_settings),
) -> ApiResponse:
    """POST /api/v1/sim/auth/revoke"""
    store = _require_store(request)
    payload = getattr(request.state, "token_payload", None)
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A bearer token is required to revoke",
        )
    jti = str(payload.get("jti", ""))
    if not jti:
        return ApiResponse(data={"revoked": False, "reason": "token has no jti claim"})
    principal = current_principal(request)
    first_time = await run_in_threadpool(
        store.revoke_token,
        jti,
        expires_at=float(payload.get("exp", 0.0) or 0.0),
        user_id=principal.user_id,
    )
    if first_time:
        AUTH_TOKEN_EVENTS_TOTAL.labels(event="revoke").inc()
        audit_log(
            "auth.token.revoke",
            principal.user_id,
            jti,
            tenant=principal.tenant,
            auth_type="jwt",
        )
    logger.info(f"Revoked access token jti={jti} for '{principal.user_id}'")
    return ApiResponse(data={"revoked": True, "jti": jti})


# ─── 服务账号 API Key（V2.5.0） ──────────────────────────────────────────────


@router.post(
    "/auth/api-keys",
    response_model=ApiResponse,
    summary="签发服务账号 API Key",
    description=(
        "签发用于服务间调用的 API Key（明文仅返回一次）。"
        "调用时使用 `X-API-Key: hsk_...` 或 `Authorization: ApiKey hsk_...`。"
        "需要 admin 角色与 STATE_ENABLED=true。"
    ),
)
async def create_api_key(
    body: ApiKeyCreateRequest,
    request: Request,
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """POST /api/v1/sim/auth/api-keys"""
    platform = get_settings().platform
    if not platform.api_keys_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys are disabled (PLATFORM_API_KEYS_ENABLED=false)",
        )
    principal = getattr(request.state, "principal", None)
    caller_tenant = principal.tenant if isinstance(principal, Principal) else platform.default_tenant
    tenant = body.tenant or caller_tenant
    store = _require_store(request)

    raw_key, key_id, salt, key_hash = await run_in_threadpool(store.generate_api_key)
    now = time.time()
    expires_at = (now + body.expires_in_days * 86400.0) if body.expires_in_days else 0.0
    await run_in_threadpool(
        store.save_api_key,
        {
            "key_id": key_id,
            "name": body.name,
            "tenant": tenant,
            "role": body.role,
            "key_hash": key_hash,
            "salt": salt,
            "prefix": raw_key[: len(raw_key.split("_", 2)[0]) + 1 + 8],
            "created_by": admin_id,
            "created_at": now,
            "expires_at": expires_at,
        },
    )
    audit_log(
        "auth.api_key.create",
        admin_id,
        key_id,
        detail=f"name={body.name}, role={body.role}, tenant={tenant}",
        tenant=caller_tenant,
        auth_type=getattr(principal, "auth_type", ""),
    )
    logger.info(f"API key issued: {key_id} (name={body.name}, role={body.role}, tenant={tenant})")
    return ApiResponse(
        data={
            "key_id": key_id,
            "name": body.name,
            "tenant": tenant,
            "role": body.role,
            "api_key": raw_key,
            "created_at": now,
            "expires_at": expires_at,
            "warning": "该明文密钥仅返回一次，请立即安全保存",
        }
    )


@router.get(
    "/auth/api-keys",
    response_model=ApiResponse,
    summary="列出服务账号 API Key",
    description="列出服务账号元数据（不含密钥内容）；默认仅本租户，admin 可指定 tenant 查询。",
)
async def list_api_keys(
    request: Request,
    tenant: str = Query("", description="租户 ID（空取调用主体租户）"),
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """GET /api/v1/sim/auth/api-keys"""
    platform = get_settings().platform
    principal = getattr(request.state, "principal", None)
    caller_tenant = principal.tenant if isinstance(principal, Principal) else platform.default_tenant
    store = _require_store(request)
    items = [
        {
            "key_id": item["key_id"],
            "name": item["name"],
            "tenant": item["tenant"],
            "role": item["role"],
            "prefix": item["prefix"],
            "created_by": item["created_by"],
            "created_at": item["created_at"],
            "expires_at": item["expires_at"],
            "last_used_at": item["last_used_at"],
            "revoked": float(item["revoked_at"]) > 0,
        }
        for item in await run_in_threadpool(store.list_api_keys, tenant or caller_tenant)
    ]
    return ApiResponse(data={"items": items, "total": len(items)})


@router.delete(
    "/auth/api-keys/{key_id}",
    response_model=ApiResponse,
    summary="吊销服务账号 API Key",
    description="吊销后立即失效（幂等）；仅能吊销本租户密钥，admin 可指定 tenant。",
)
async def revoke_api_key(
    key_id: str,
    request: Request,
    tenant: str = Query("", description="租户 ID（空取调用主体租户）"),
    admin_id: str = Depends(require_admin),
) -> ApiResponse:
    """DELETE /api/v1/sim/auth/api-keys/{key_id}"""
    platform = get_settings().platform
    principal = getattr(request.state, "principal", None)
    caller_tenant = principal.tenant if isinstance(principal, Principal) else platform.default_tenant
    store = _require_store(request)
    target_tenant = tenant or caller_tenant
    if not await run_in_threadpool(store.revoke_api_key, key_id, tenant=target_tenant):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API key '{key_id}' not found or already revoked",
        )
    audit_log(
        "auth.api_key.revoke",
        admin_id,
        key_id,
        detail=f"tenant={target_tenant}",
        tenant=target_tenant,
        auth_type=getattr(principal, "auth_type", ""),
    )
    logger.info(f"API key revoked: {key_id} (tenant={target_tenant}, by={admin_id})")
    return ApiResponse(data={"key_id": key_id, "revoked": True})


__all__ = ["router"]
