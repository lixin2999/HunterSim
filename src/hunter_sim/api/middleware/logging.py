"""请求日志与审计日志（PROMPT-API-001）。

记录每个 HTTP 请求的方法、路径、耗时、状态码；
并提供 audit_log() 记录实例创建/销毁等关键操作（设计文档 §14.2 审计日志）。
"""

from __future__ import annotations

import time
import uuid
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 审计日志专用 logger，便于独立输出/采集
_audit_logger = get_logger("hunter_sim.audit")

# 审计落库 sink（V2.5.0）：由 main 注入 StateStore.append_audit
_AuditSink = Callable[[str, str, str, str, str, str], None]
_audit_sink: Optional[_AuditSink] = None


def set_audit_sink(sink: Optional[_AuditSink]) -> None:
    """注册/清除审计落库 sink（参数：action, user_id, target, detail, tenant, auth_type）。"""
    global _audit_sink  # noqa: PLW0603
    _audit_sink = sink


def audit_log(
    action: str,
    user_id: str,
    target: str,
    detail: str = "",
    tenant: str = "default",
    auth_type: str = "",
) -> None:
    """记录一条审计日志（设计文档 §14.2：所有实例操作记录审计日志）。

    V2.5.0 起同时写入状态库（由 ``set_audit_sink`` 注入），支持 ``GET /audit``
    查询；落库失败仅告警，不影响主流程。

    Args:
        action: 操作类型，如 instance.create / instance.destroy。
        user_id: 操作用户 ID（JWT sub 或 API Key 绑定账号）。
        target: 操作对象，如实例 ID。
        detail: 附加信息。
        tenant: 租户 ID。
        auth_type: 认证方式（jwt/api_key）。
    """
    _audit_logger.info(
        {
            "audit": True,
            "action": action,
            "user_id": user_id,
            "tenant": tenant,
            "target": target,
            "detail": detail,
            "auth_type": auth_type,
            "timestamp": time.time(),
        }
    )
    sink = _audit_sink
    if sink is None:
        return
    try:
        sink(action, user_id, target, detail, tenant, auth_type)
    except Exception as exc:  # noqa: BLE001  审计落库失败不阻断业务操作
        logger.warning(f"Audit sink failed for action '{action}': {exc}")


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
