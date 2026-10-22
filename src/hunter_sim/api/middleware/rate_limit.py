"""请求限流与登录退避（V2.6.0 P0-3）。

补齐 ``API_RATE_LIMIT_PER_MINUTE`` 配置的消费端：此前配置声明了但未落地，
``/auth/token`` 等公开端点没有爆破防护。

- ``TokenBucket``：按客户端 IP 的令牌桶（线程安全，惰性建桶 + 空闲回收）；
- ``RateLimitMiddleware``：对 API 路径按 IP 限流，超限返回 429 + ``Retry-After``；
- ``LoginFailureGuard``：登录失败计数与指数退避，连续失败超过阈值后拒绝
  （429），成功登录即复位；退避时长 ``base * 2^(failures - max_failures)`` 封顶。

两者均不引入外部存储；单副本部署下进程内计数即为权威（与 V2.5.0 配额模型一致）。
"""

from __future__ import annotations

import math
import threading
import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hunter_sim.common.metrics import RATE_LIMITED_REJECTIONS_TOTAL
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

# 空闲桶回收：超过该秒数未使用且无余额意义保留的桶直接删除，防止内存单调增长
_BUCKET_IDLE_SECONDS = 600.0
# 桶数量软上限：达到后触发一次全量空闲回收
_BUCKET_MAX_KEYS = 4096


class TokenBucket:
    """按 key 的令牌桶限流器（线程安全）。

    Args:
        rate_per_minute: 每分钟允许的请求数（每 key 独立补给）。
        capacity: 桶容量（突发上限），默认与 rate_per_minute 相同。
    """

    def __init__(self, rate_per_minute: float, capacity: Optional[float] = None) -> None:
        self._rate_per_second = max(float(rate_per_minute), 0.0) / 60.0
        self._capacity = float(capacity) if capacity is not None else max(float(rate_per_minute), 1.0)
        self._buckets: dict[str, list[float]] = {}  # key -> [tokens, last_refill]
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """消费一个令牌。

        Args:
            key: 限流主体（通常为客户端 IP）。

        Returns:
            ``(是否放行, 建议 Retry-After 秒数)``；放行时 Retry-After 为 0。
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= _BUCKET_MAX_KEYS:
                    self._prune_locked(now)
                bucket = [self._capacity, now]
                self._buckets[key] = bucket
            refill = (now - bucket[1]) * self._rate_per_second
            bucket[0] = min(self._capacity, bucket[0] + refill)
            bucket[1] = now
            if bucket[0] >= 1.0:
                bucket[0] -= 1.0
                return True, 0.0
            missing = 1.0 - bucket[0]
            retry_after = missing / self._rate_per_second if self._rate_per_second > 0 else 60.0
            return False, retry_after

    def _prune_locked(self, now: float) -> None:
        """回收长期空闲的桶（调用方已持锁）。"""
        stale = [
            key
            for key, (_, last) in self._buckets.items()
            if now - last > _BUCKET_IDLE_SECONDS
        ]
        for key in stale:
            self._buckets.pop(key, None)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """按客户端 IP 的 API 限流中间件。

    Args:
        app: ASGI 应用。
        rate_per_minute: 每分钟请求上限（来自 ``API_RATE_LIMIT_PER_MINUTE``）。
        enabled: 关闭时中间件直通（dev 联调/压测可显式关闭）。
        exempt_prefixes: 豁免前缀（健康探针/Prometheus 抓取/WebSocket 握手）。
        protected_prefix: 仅对该前缀下的路径限流（默认 ``/api/v1/sim``）。
    """

    def __init__(
        self,
        app: object,
        rate_per_minute: int = 1000,
        enabled: bool = True,
        exempt_prefixes: tuple[str, ...] = (),
        protected_prefix: str = "/api/v1/sim",
        **kwargs: object,
    ) -> None:
        super().__init__(app, **kwargs)  # type: ignore[arg-type]
        self._enabled = enabled and rate_per_minute > 0
        self._bucket = TokenBucket(rate_per_minute)
        self._exempt = tuple(exempt_prefixes)
        self._protected_prefix = protected_prefix

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """放行/拒绝请求；超限返回 429（统一响应体格式）。"""
        if not self._enabled or request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if not path.startswith(self._protected_prefix) or any(
            path.startswith(p) for p in self._exempt
        ):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        allowed, retry_after = self._bucket.allow(client_ip)
        if not allowed:
            RATE_LIMITED_REJECTIONS_TOTAL.inc()
            logger.warning(
                f"Rate limit exceeded for client '{client_ip}' on {path} "
                f"(retry after {retry_after:.1f}s)"
            )
            return JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "message": "Too many requests; slow down and retry later",
                    "data": None,
                },
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
        return await call_next(request)


class LoginFailureGuard:
    """登录失败退避守卫（按客户端 IP，线程安全）。

    连续失败 ``max_failures`` 次后开始拒绝（交由路由转 429），退避时长按
    ``base_seconds * 2**(failures - max_failures)`` 指数增长，封顶 ``max_seconds``；
    任意一次成功登录即清零计数。

    Args:
        max_failures: 触发退避的连续失败次数阈值。
        base_seconds: 首次退避时长（秒）。
        max_seconds: 退避时长上限（秒）。
    """

    def __init__(
        self,
        max_failures: int = 5,
        base_seconds: float = 2.0,
        max_seconds: float = 60.0,
    ) -> None:
        self._max_failures = max(1, int(max_failures))
        self._base_seconds = float(base_seconds)
        self._max_seconds = float(max_seconds)
        # key -> (failures, blocked_until_monotonic)
        self._states: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> Optional[float]:
        """查询是否处于退避期。

        Returns:
            剩余拒绝秒数（>0 表示应拒绝）；不在退避期返回 None。
        """
        now = time.monotonic()
        with self._lock:
            failures, blocked_until = self._states.get(key, (0, 0.0))
        if failures >= self._max_failures and blocked_until > now:
            return blocked_until - now
        return None

    def record_failure(self, key: str) -> None:
        """记一次失败，并按指数退避更新拒绝窗口。"""
        with self._lock:
            failures, _ = self._states.get(key, (0, 0.0))
            failures += 1
            blocked_until = 0.0
            if failures >= self._max_failures:
                exponent = min(failures - self._max_failures, 16)
                delay = min(self._base_seconds * (2**exponent), self._max_seconds)
                blocked_until = time.monotonic() + delay
            self._states[key] = (failures, blocked_until)

    def reset(self, key: str) -> None:
        """成功登录后清零该主体计数。"""
        with self._lock:
            self._states.pop(key, None)

    def failures(self, key: str) -> int:
        """当前连续失败次数（测试与诊断辅助）。"""
        with self._lock:
            return self._states.get(key, (0, 0.0))[0]


__all__ = ["LoginFailureGuard", "RateLimitMiddleware", "TokenBucket"]
