"""FastAPI 依赖注入（PROMPT-API-001）。

提供 JWT 认证、全局配置单例、服务层对象注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from hunter_sim.common.models import APISettings, HunterSimSettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
_bearer_scheme = HTTPBearer(auto_error=False)

# 管理员角色名（设计文档 §14.2：创建/销毁实例需管理员权限）
_ADMIN_ROLE = "admin"
# 平台化（V2.5.0）：角色层级 viewer < operator < admin
_OPERATOR_ROLE = "operator"
_ROLE_LEVELS: dict[str, int] = {"guest": 0, "viewer": 1, "operator": 2, "admin": 3}

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
    tenant: str = "",
) -> str:
    """生成 JWT access token（仅返回编码后的字符串）。

    详见 ``mint_access_token``；本函数为既有调用方保留的便捷包装。
    """
    token, _, _ = mint_access_token(subject, api_settings, extra_claims, tenant)
    return token


def mint_access_token(
    subject: str,
    api_settings: APISettings,
    extra_claims: Optional[dict[str, Any]] = None,
    tenant: str = "",
    jti: Optional[str] = None,
) -> tuple[str, str, int]:
    """签发 access token 并返回 ``(token, jti, exp_epoch)``（V2.7.0 P1-6）。

    每次签发写入唯一 ``jti``，供吊销名单与滑动续期轮换识别；``jti`` 也可由调用方
    指定（测试/回放可重现）。

    Args:
        subject: 用户标识（user_id）。
        api_settings: API 配置（包含 jwt_secret 等）。
        extra_claims: 额外载荷字段。
        tenant: 租户 ID（V2.5.0）；为空时不写入 claim。
        jti: 指定令牌唯一标识；None 时自动生成。

    Returns:
        ``(编码后的 JWT 字符串, jti, 过期绝对时间 epoch 秒)``。
    """
    import uuid  # noqa: PLC0415  仅签发时使

    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=api_settings.jwt_expire_minutes)
    token_jti = jti or uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": token_jti,
    }
    if extra_claims:
        payload.update(extra_claims)
    if tenant:
        payload["tenant"] = tenant
    token = jwt.encode(
        payload, api_settings.jwt_secret.get_secret_value(), algorithm=api_settings.jwt_algorithm
    )
    return cast(str, token), token_jti, int(expire.timestamp())


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


# ─── 认证主体（Principal） ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Principal:
    """已认证的请求主体（V2.5.0 平台化）。

    统一承载 JWT 用户与服务账号 API Key 两种身份，供租户隔离与 RBAC 判断。

    Attributes:
        user_id: 主体标识（JWT sub 或 API Key 绑定账号）。
        role: 角色（guest/viewer/operator/admin）。
        tenant: 租户 ID；旧 Token 缺省时取 ``PLATFORM_DEFAULT_TENANT``。
        auth_type: 认证方式（jwt/api_key）。
        api_key_id: API Key 标识（仅 api_key 方式非空）。
    """

    user_id: str
    role: str = "guest"
    tenant: str = "default"
    auth_type: str = "jwt"
    api_key_id: str = ""

    @property
    def is_admin(self) -> bool:
        """是否管理员（与 ``require_admin`` 语义一致）。"""
        return self.role == _ADMIN_ROLE

    def has_role(self, minimum: str) -> bool:
        """角色是否达到指定层级（未知角色按 guest 计算）。"""
        return _ROLE_LEVELS.get(self.role, 0) >= _ROLE_LEVELS.get(minimum, 0)


def get_default_tenant() -> str:
    """返回平台默认租户（配置未初始化时回退 ``default``）。"""
    if _settings is None:
        return "default"
    return _settings.platform.default_tenant


def principal_from_payload(
    payload: dict[str, Any],
    auth_type: str = "jwt",
    api_key_id: str = "",
) -> Principal:
    """由 JWT/合成载荷构造 Principal。

    Raises:
        HTTPException: 载荷缺少 ``sub`` 时返回 401。
    """
    user_id: Optional[str] = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing 'sub' claim")
    return Principal(
        user_id=str(user_id),
        role=get_user_role(payload),
        tenant=str(payload.get("tenant") or get_default_tenant()),
        auth_type=auth_type,
        api_key_id=api_key_id,
    )


# ─── 认证 Dependency ──────────────────────────────────────────────────────────


async def get_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Principal:
    """FastAPI Dependency：解析当前请求主体（JWT 或 API Key）。

    优先使用中间件写入的 ``request.state.token_payload``（中间件已支持两种
    认证方式）；无中间件的测试/子应用直接校验 Bearer Token。

    Raises:
        HTTPException: 凭证缺失或 token 无效时抛出 401。
    """
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, Principal):
        return cached

    payload = getattr(request.state, "token_payload", None)
    if isinstance(payload, dict) and payload.get("sub"):
        principal = principal_from_payload(
            payload,
            auth_type=str(payload.get("auth_type", "jwt")),
            api_key_id=str(payload.get("api_key_id", "")),
        )
        request.state.principal = principal
        return principal

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    decoded = decode_token(credentials.credentials, get_api_settings())
    principal = principal_from_payload(decoded)
    request.state.principal = principal
    return principal


def current_principal(request: Request) -> Principal:
    """读取已认证主体（供已通过 ``require_*`` 依赖的路由在内部使用）。

    与 ``get_principal`` 的区别：不抛 401，缺失时返回 guest/default，
    仅用于已完成鉴权的路径（如审计字段填充），不会成为鉴权旁路。
    """
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, Principal):
        return cached
    payload = getattr(request.state, "token_payload", None)
    if isinstance(payload, dict) and payload.get("sub"):
        return principal_from_payload(
            payload,
            auth_type=str(payload.get("auth_type", "jwt")),
            api_key_id=str(payload.get("api_key_id", "")),
        )
    return Principal(user_id="", role="guest", tenant=get_default_tenant())


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> str:
    """FastAPI Dependency：返回当前用户 ID（JWT sub 或 API Key 绑定账号）。

    Raises:
        HTTPException: 凭证缺失或 token 无效时抛出 401。
    """
    return (await get_principal(request, credentials)).user_id


def get_user_role(payload: dict[str, Any]) -> str:
    """从 JWT payload 中提取用户角色，未声明时默认 guest（无管理员权限）。"""
    return str(payload.get("role", "guest"))


async def require_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> str:
    """FastAPI Dependency：要求当前主体具备管理员角色（设计文档 §14.2）。

    创建/销毁仿真实例等高危操作需 role=admin 的 JWT token 或 API Key。

    Raises:
        HTTPException: 401 凭证无效；403 角色不足。
    """
    principal = await get_principal(request, credentials)
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Admin role required for this operation (current role: '{principal.role}')",
        )
    return principal.user_id


async def require_operator(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Principal:
    """FastAPI Dependency：要求 operator 及以上角色（V2.5.0）。

    日常场景作业（创建实例/下发场景）开放给 operator，销毁与平台配置仅限 admin。

    Raises:
        HTTPException: 401 凭证无效；403 角色不足。
    """
    principal = await get_principal(request, credentials)
    if not principal.has_role(_OPERATOR_ROLE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Operator role required for this operation (current role: '{principal.role}')"
            ),
        )
    return principal


async def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[str]:
    """可选认证 Dependency：token 不存在时返回 None（用于公开接口）。"""
    payload = getattr(request.state, "token_payload", None)
    if isinstance(payload, dict):
        return payload.get("sub")
    if credentials is None:
        return None
    try:
        decoded = decode_token(credentials.credentials, get_api_settings())
        return decoded.get("sub")
    except HTTPException:
        return None
