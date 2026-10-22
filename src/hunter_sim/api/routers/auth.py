"""认证路由（V2.3.0）。

提供 ``POST /api/v1/sim/auth/token``：用平台内置管理员账号换取 JWT。
此前该路由只存在于中间件白名单与数据模型里，没有任何实现，
导致「拿不到 token → 所有受保护接口 401」的接入死循环。

账号来源：``API_ADMIN_USERNAME`` / ``API_ADMIN_PASSWORD``（默认 admin/admin，仅限 dev）；
``HUNTER_SIM_ENV=prod`` 时启动闸门会拒绝默认口令。
后续接入统一身份源（LDAP/OIDC/HunterCore）时替换本模块即可。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, status

from hunter_sim.api.deps import create_access_token, get_api_settings
from hunter_sim.api.models import ApiResponse, TokenRequest, TokenResponse
from hunter_sim.common.models import APISettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()

# 内置管理员账号的角色（创建/销毁实例等操作需 admin，见 §14.2）
_ADMIN_ROLE = "admin"


@router.post(
    "/auth/token",
    response_model=ApiResponse,
    summary="获取 JWT 访问令牌",
    description=(
        "使用 API_ADMIN_USERNAME / API_ADMIN_PASSWORD 换取 Bearer Token。"
        "返回的 access_token 用于其余接口的 Authorization: Bearer <token>。"
    ),
)
async def issue_token(
    body: TokenRequest,
    api_settings: APISettings = Depends(get_api_settings),
) -> ApiResponse:
    """POST /api/v1/sim/auth/token

    Args:
        body: 用户名/口令。
        api_settings: API 配置（内置管理员凭据与 JWT 参数）。

    Returns:
        统一响应体，data 为 TokenResponse 结构。

    Raises:
        HTTPException: 凭据错误返回 401。
    """
    expected_user = api_settings.admin_username
    expected_password = api_settings.admin_password.get_secret_value()

    # 常量时间比较，避免口令被计时侧信道推断；按 UTF-8 字节比较以兼容非 ASCII 用户名
    user_ok = secrets.compare_digest(body.username.encode("utf-8"), expected_user.encode("utf-8"))
    password_ok = secrets.compare_digest(
        body.password.encode("utf-8"),
        expected_password.encode("utf-8"),
    )
    if not (user_ok and password_ok):
        logger.warning(f"Token request rejected for user '{body.username}'")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(body.username, api_settings, extra_claims={"role": _ADMIN_ROLE})
    logger.info(f"Issued access token for '{body.username}' (role={_ADMIN_ROLE})")
    payload = TokenResponse(
        access_token=token,
        expires_in=api_settings.jwt_expire_minutes * 60,
    )
    return ApiResponse(data=payload.model_dump())


__all__ = ["router"]
