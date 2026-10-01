"""FastAPI 依赖注入（PROMPT-API-001）。

提供 JWT 认证、全局配置单例、服务层对象注入。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from hunter_sim.common.models import APISettings, HunterSimSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
_bearer_scheme = HTTPBearer(auto_error=False)

# 管理员角色名（设计文档 §14.2：创建/销毁实例需管理员权限）
_ADMIN_ROLE = "admin"

# 全局配置实例（由 main.py 在 create_app 时设置）
_settings: Optional[HunterSimSettings] = None


def set_app_settings(settings: HunterSimSettings) -> None:
    """由应用启动时调用，注入全局配置。"""
    global _settings  # noqa: PLW0603
    _settings = settings


def get_settings() -> HunterSimSettings:
    """获取全局配置（Dependency）。"""
    if _settings is None:
        raise HTTPException(status_code=500, detail="Settings not initialized")
    return _settings


def get_api_settings() -> APISettings:
    """获取 API 层配置。"""
    return get_settings().api


# ─── JWT 工具 ─────────────────────────────────────────────────────────────────


def create_access_token(
    subject: str,
    api_settings: APISettings,
    extra_claims: Optional[dict[str, Any]] = None,
) -> str:
    """生成 JWT access token。

    Args:
        subject: 用户标识（user_id）。
        api_settings: API 配置（包含 jwt_secret 等）。
        extra_claims: 额外载荷字段。

    Returns:
        编码后的 JWT 字符串。
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=api_settings.jwt_expire_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(
        payload, api_settings.jwt_secret.get_secret_value(), algorithm=api_settings.jwt_algorithm
    )
    return cast(str, token)


def decode_token(token: str, api_settings: APISettings) -> dict[str, Any]:
    """解码并验证 JWT token。

    Args:
        token: JWT 字符串。
        api_settings: API 配置。

    Returns:
        解码后的 payload 字典。

    Raises:
        HTTPException: Token 无效或过期时抛出 401。
    """
    try:
        payload = cast(dict[str, Any], jwt.decode(
            token,
            api_settings.jwt_secret.get_secret_value(),
            algorithms=[api_settings.jwt_algorithm],
        ))
        return payload
    except JWTError as exc:
        logger.warning(f"JWT decode failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ─── 认证 Dependency ──────────────────────────────────────────────────────────


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> str:
    """FastAPI Dependency：从 Authorization header 解析当前用户 ID。

    Args:
        credentials: HTTP Bearer 凭证。

    Returns:
        用户 ID（JWT sub 字段）。

    Raises:
        HTTPException: 凭证缺失或 token 无效时抛出 401。
    """
    api_settings = get_api_settings()
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials, api_settings)
    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing 'sub' claim")
    return user_id


def get_user_role(payload: dict[str, Any]) -> str:
    """从 JWT payload 中提取用户角色，未声明时默认 guest（无管理员权限）。"""
    return str(payload.get("role", "guest"))


async def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> str:
    """FastAPI Dependency：要求当前用户具备管理员角色（设计文档 §14.2）。

    创建/销毁仿真实例等高危操作需 role=admin 的 JWT token。

    Args:
        credentials: HTTP Bearer 凭证。

    Returns:
        管理员用户 ID。

    Raises:
        HTTPException: 401 凭证无效；403 角色不足。
    """
    api_settings = get_api_settings()
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials, api_settings)
    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing 'sub' claim")
    if get_user_role(payload) != _ADMIN_ROLE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Admin role required for this operation (current role: '{get_user_role(payload)}')",
        )
    return user_id


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[str]:
    """可选认证 Dependency：token 不存在时返回 None（用于公开接口）。"""
    if credentials is None:
        return None
    api_settings = get_api_settings()
    try:
        payload = decode_token(credentials.credentials, api_settings)
        return payload.get("sub")
    except HTTPException:
        return None
