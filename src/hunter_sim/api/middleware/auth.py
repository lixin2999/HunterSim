"""JWT / API Key 认证中间件（PROMPT-API-001，V2.5.0 服务账号）。

对 /api/v1/sim 路径下的受保护接口进行认证：
- ``Authorization: Bearer <jwt>``（用户）；
- ``X-API-Key: hsk_...`` 或 ``Authorization: ApiKey hsk_...``（服务账号，V2.5.0）。

白名单路径（/docs /redoc /openapi.json /health /auth/token）免鉴权。
"""

from __future__ import annotations

from typing import Any, Set

from fastapi.concurrency import run_in_threadpool
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hunter_sim.common.models import APISettings
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 不需要鉴权的路径前缀
_DEFAULT_WHITELIST: Set[str] = {
    "/api/v1/sim/docs",
    "/api/v1/sim/redoc",
    "/api/v1/sim/openapi.json",
    "/api/v1/sim/health",
    "/api/v1/sim/auth/token",
    "/api/v1/sim/metrics",  # Prometheus 抓取端点，免鉴权
    "/api/v1/sim/ws/",  # WebSocket 在连接时单独鉴权
}

_API_KEY_HEADER = "X-API-Key"
_API_KEY_SCHEME = "ApiKey "


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """全局认证中间件（JWT + API Key）。

    Args:
        api_settings: API 配置（包含 jwt_secret / jwt_algorithm）。
        whitelist: 额外白名单路径集合。
        api_keys_enabled: 是否接受服务账号 API Key（V2.5.0）。
    """

    def __init__(
        self,
        app: object,
        api_settings: APISettings,
        whitelist: Set[str] | None = None,
        api_keys_enabled: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(app, **kwargs)  # type: ignore[arg-type]
        self._settings = api_settings
        self._whitelist = whitelist or _DEFAULT_WHITELIST
        self._api_keys_enabled = api_keys_enabled

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # 放行 OPTIONS（CORS 预检）
        if request.method == "OPTIONS":
            return await call_next(request)

        # 白名单路径直接放行
        if any(path.startswith(w) for w in self._whitelist):
            return await call_next(request)

        # 服务账号 API Key（优先：两种头任选其一）
        api_key = request.headers.get(_API_KEY_HEADER, "").strip()
        auth_header: str = request.headers.get("Authorization", "")
        if not api_key and auth_header.startswith(_API_KEY_SCHEME):
            api_key = auth_header[len(_API_KEY_SCHEME):].strip()
        if api_key:
            return await self._authenticate_api_key(request, call_next, api_key)

        # 用户 JWT Bearer
        if not auth_header.startswith("Bearer "):
            return _unauthorized("Missing or invalid Authorization header")

        token = auth_header[7:]
        try:
            payload = jwt.decode(
                token,
                self._settings.jwt_secret.get_secret_value(),
                algorithms=[self._settings.jwt_algorithm],
            )
            user_id: str | None = payload.get("sub")
            if not user_id:
                return _unauthorized("Token missing 'sub' claim")
            # V2.7.0 P1-6：吊销名单校验（仅当启用且状态库可用时生效）
            if await self._is_revoked(request, payload):
                logger.warning(
                    f"Rejected revoked token jti='{payload.get('jti', '')}' for '{user_id}'"
                )
                return _unauthorized("Token has been revoked")
            request.state.user_id = user_id
            request.state.token_payload = payload
        except JWTError as exc:
            logger.warning(f"JWT validation failed: {exc}")
            return _unauthorized("Invalid or expired token")

        return await call_next(request)

    async def _is_revoked(self, request: Request, payload: dict[str, Any]) -> bool:
        """判断已解码载荷的 ``jti`` 是否命中吊销名单（P1-6）。

        仅当 ``jwt_revocation_enabled`` 且请求应用挂载了状态库时才判定；读库异常
        按未吊销处理（不阻断鉴权主链路），与 API Key 校验的降级语义一致。
        状态库经 ``request.app.state`` 获取（BaseHTTPMiddleware 的 ``self.app``
        是包装后的内层 ASGI 应用，不具备 ``state``）。

        V2.10.0：SQLite 查询改经线程池，避免每个请求在事件循环内同步读库。
        """
        if not getattr(self._settings, "jwt_revocation_enabled", False):
            return False
        jti = str(payload.get("jti", ""))
        if not jti:
            return False
        store: Any = getattr(request.app.state, "state_store", None)
        checker = getattr(store, "is_token_revoked", None)
        if not callable(checker):
            return False
        try:
            return bool(await run_in_threadpool(checker, jti))
        except Exception as exc:  # noqa: BLE001  读吊销名单失败不阻断鉴权
            logger.warning(f"Token revocation check failed (treating as valid): {exc}")
            return False

    async def _authenticate_api_key(
        self, request: Request, call_next: RequestResponseEndpoint, raw_key: str
    ) -> Response:
        """校验服务账号密钥并写入合成身份（不直接访问数据库，委托 StateStore）。"""
        if not self._api_keys_enabled:
            return _unauthorized("API key authentication is disabled")
        store: Any = getattr(request.app.state, "state_store", None)
        verify = getattr(store, "verify_api_key", None)
        if not callable(verify):
            return _unauthorized("API key authentication requires state persistence")
        # V2.10.0：密钥摘要校验为用户可控的高成本计算，必须离开事件循环
        info = await run_in_threadpool(verify, raw_key)
        if not info:
            logger.warning(
                f"API key validation failed from {request.client.host if request.client else 'unknown'}"
            )
            return _unauthorized("Invalid or revoked API key")
        # 服务账号主体以 svc:<name> 标识，审计可追溯到具体 key_id
        user_id = f"svc:{info.get('name', '')}"
        request.state.user_id = user_id
        request.state.token_payload = {
            "sub": user_id,
            "role": info.get("role", "operator"),
            "tenant": info.get("tenant", "default"),
            "auth_type": "api_key",
            "api_key_id": info.get("key_id", ""),
        }
        return await call_next(request)


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"code": 401, "message": message, "data": None},
    )
