"""请求日志中间件（PROMPT-API-001）。

记录每个 HTTP 请求的方法、路径、耗时、状态码。
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """HTTP 请求日志中间件。

    为每个请求生成唯一 request_id，并记录：
    - 请求方法 / 路径 / 查询参数
    - 响应状态码
    - 处理耗时（毫秒）
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """拦截请求，记录日志后传递。

        Args:
            request: 传入的 HTTP 请求。
            call_next: 下游处理链。

        Returns:
            响应对象。
        """
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id
        start = time.perf_counter()

        # 注入 request_id 到响应 header
        response: Response = await call_next(request)

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        response.headers["X-Request-ID"] = request_id

        log_level = "warning" if response.status_code >= 400 else "info"
        getattr(logger, log_level)(
            {
                "request_id": request_id,
                "method": request.method,
                "path": str(request.url.path),
                "query": str(request.url.query) if request.url.query else None,
                "status": response.status_code,
                "elapsed_ms": round(elapsed_ms, 2),
                "client": request.client.host if request.client else "unknown",
            }
        )
        return response
