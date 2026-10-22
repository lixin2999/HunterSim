"""认证与平台服务账号路由（V2.3.0 JWT，V2.5.0 API Key）。

提供：
- ``POST /api/v1/sim/auth/token``：用平台内置管理员账号换取 JWT；
- ``POST /api/v1/sim/auth/api-keys``：签发服务账号 API Key（admin）；
- ``GET  /api/v1/sim/auth/api-keys``：列出服务账号（admin）；
- ``DELETE /api/v1/sim/auth/api-keys/{key_id}``：吊销服务账号（admin）。

账号来源：``API_ADMIN_USERNAME`` / ``API_ADMIN_PASSWORD``（默认 admin/admin，仅限 dev）；
``HUNTER_SIM_ENV=prod`` 时启动闸门会拒绝默认口令。
API Key 依赖状态库（``STATE_ENABLED=true``）：密钥仅存 sha256(salt:secret) 摘要，
明文只在签发响应中返回一次。
"""

from __future__ import annotations

import math
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from hunter_sim.api.deps import (
    Principal,
    create_access_token,
    get_api_settings,
    get_settings,
    require_admin,
)
from hunter_sim.api.middleware.logging import audit_log
from hunter_sim.api.middleware.rate_limit import LoginFailureGuard
from hunter_sim.api.models import ApiKeyCreateRequest, ApiResponse, TokenRequest, TokenResponse
from hunter_sim.common.metrics import LOGIN_REJECTIONS_TOTAL
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


@router.post(
    "/auth/token",
    response_model=ApiResponse,
    summary="获取 JWT 访问令牌",
    description=(
        "使用 API_ADMIN_USERNAME / API_ADMIN_PASSWORD 换取 Bearer Token。"
        "返回的 access_token 用于其余接口的 Authorization: Bearer <token>。"
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
        request: 当前请求（用于定位登录退避主体 IP）。
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

    expected_user = api_settings.admin_username
    expected_password = api_settings.admin_password.get_secret_value()

    # 常量时间比较，避免口令被计时侧信道推断；按 UTF-8 字节比较以兼容非 ASCII 用户名
    user_ok = secrets.compare_digest(body.username.encode("utf-8"), expected_user.encode("utf-8"))
    password_ok = secrets.compare_digest(
        body.password.encode("utf-8"),
        expected_password.encode("utf-8"),
    )
    if not (user_ok and password_ok):
        if guard is not None:
            guard.record_failure(client_ip)
        LOGIN_REJECTIONS_TOTAL.labels(reason="credential").inc()
        logger.warning(f"Token request rejected for user '{body.username}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if guard is not None:
        guard.reset(client_ip)
    token = create_access_token(
        body.username,
        api_settings,
        extra_claims={"role": _ADMIN_ROLE},
        tenant=get_settings().platform.default_tenant,
    )
    logger.info(
        f"Issued access token for '{body.username}' "
        f"(role={_ADMIN_ROLE}, tenant={get_settings().platform.default_tenant})"
    )
    payload = TokenResponse(
        access_token=token,
        expires_in=api_settings.jwt_expire_minutes * 60,
    )
    return ApiResponse(data=payload.model_dump())


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

    raw_key, key_id, salt, key_hash = store.generate_api_key()
    now = time.time()
    expires_at = (now + body.expires_in_days * 86400.0) if body.expires_in_days else 0.0
    store.save_api_key(
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
        }
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
        for item in store.list_api_keys(tenant or caller_tenant)
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
    if not store.revoke_api_key(key_id, tenant=target_tenant):
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
