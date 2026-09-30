"""JWT 认证中间件（PROMPT-API-001）。

对 /api/v1/sim 路径下的受保护接口进行 JWT Bearer Token 验证。
白名单路径（/docs /redoc /openapi.json /health /auth/token）免鉴权。
"""

from __future__ import annotations

from typing import Set

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


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """全局 JWT Bearer 认证中间件。

    Args:
        api_settings: API 配置（包含 jwt_secret / jwt_algorithm）。
        whitelist: 额外白名单路径集合。
    """

    def __init__(
        self,
        app: object,
        api_settings: APISettings,
        whitelist: Set[str] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(app, **kwargs)  # type: ignore[arg-type]
        self._settings = api_settings
        self._whitelist = whitelist or _DEFAULT_WHITELIST

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # 放行 OPTIONS（CORS 预检）
        if request.method == "OPTIONS":
            return await call_next(request)

        # 白名单路径直接放行
        if any(path.startswith(w) for w in self._whitelist):
            return await call_next(request)

        # 提取 Authorization header
        auth_header: str = request.headers.get("Authorization", "")
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
            request.state.user_id = user_id
            request.state.token_payload = payload
        except JWTError as exc:
            logger.warning(f"JWT validation failed: {exc}")
            return _unauthorized("Invalid or expired token")

        return await call_next(request)


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"code": 401, "message": message, "data": None},
    )
