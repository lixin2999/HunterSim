"""限流与登录退避单测（V2.6.0 P0-3）。

覆盖 ``TokenBucket`` 令牌补给/空闲回收、``RateLimitMiddleware`` 429 与豁免前缀、
``LoginFailureGuard`` 指数退避与复位，以及 ``/auth/token`` 端点的退避行为集成路径。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from hunter_sim.api.main import create_app
from hunter_sim.api.middleware.rate_limit import (
    _BUCKET_MAX_KEYS,
    LoginFailureGuard,
    RateLimitMiddleware,
    TokenBucket,
)
from hunter_sim.common.metrics import (
    LOGIN_REJECTIONS_TOTAL,
    RATE_LIMITED_REJECTIONS_TOTAL,
    counter_value,
)
from hunter_sim.common.models import APISettings, HunterSimSettings

# ─── TokenBucket ─────────────────────────────────────────────────────────────


class TestTokenBucket:
    def test_allows_until_capacity_then_rejects(self) -> None:
        bucket = TokenBucket(rate_per_minute=3)
        results = [bucket.allow("1.2.3.4")[0] for _ in range(4)]
        assert results == [True, True, True, False]

    def test_retry_after_positive_when_rejected(self) -> None:
        bucket = TokenBucket(rate_per_minute=2)
        bucket.allow("ip")
        bucket.allow("ip")
        allowed, retry_after = bucket.allow("ip")
        assert allowed is False
        assert retry_after > 0

    def test_refill_restores_allowance(self) -> None:
        bucket = TokenBucket(rate_per_minute=60)  # 1 token/s
        bucket.allow("ip")
        # 将余额清零并把补给时基拨回 5s 前 → 应重新放行
        with bucket._lock:
            bucket._buckets["ip"][0] = 0.0
            bucket._buckets["ip"][1] -= 5.0
        assert bucket.allow("ip")[0] is True

    def test_keys_are_independent(self) -> None:
        bucket = TokenBucket(rate_per_minute=1)
        assert bucket.allow("ip-a")[0] is True
        assert bucket.allow("ip-a")[0] is False
        assert bucket.allow("ip-b")[0] is True

    def test_zero_rate_rejects_with_fallback_retry(self) -> None:
        bucket = TokenBucket(rate_per_minute=0, capacity=0)
        allowed, retry_after = bucket.allow("ip")
        assert allowed is False
        assert retry_after == 60.0

    def test_idle_buckets_pruned_at_soft_cap(self) -> None:
        bucket = TokenBucket(rate_per_minute=1000)
        now = time.monotonic()
        for i in range(_BUCKET_MAX_KEYS):
            bucket._buckets[f"stale-{i}"] = [1.0, now - 700.0]  # 全部空闲超阈值
        bucket.allow("new-comer")  # 建桶触达软上限 → 触发回收
        assert len(bucket._buckets) <= 2


# ─── RateLimitMiddleware ─────────────────────────────────────────────────────


def _build_middleware_app(
    rate_per_minute: int = 2,
    enabled: bool = True,
    exempt_prefixes: tuple[str, ...] = ("/api/v1/sim/health",),
) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/sim/ping")
    def _protected() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/v1/sim/health")
    def _exempt() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/open/no-limit")
    def _outside() -> dict[str, Any]:
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware,
        rate_per_minute=rate_per_minute,
        enabled=enabled,
        exempt_prefixes=exempt_prefixes,
    )
    return app


class TestRateLimitMiddleware:
    def test_exceeding_limit_returns_429_with_retry_after(self) -> None:
        client = TestClient(_build_middleware_app(rate_per_minute=2))
        baseline = counter_value(RATE_LIMITED_REJECTIONS_TOTAL)
        assert client.get("/api/v1/sim/ping").status_code == 200
        assert client.get("/api/v1/sim/ping").status_code == 200
        resp = client.get("/api/v1/sim/ping")
        assert resp.status_code == 429
        assert int(resp.headers["Retry-After"]) >= 1
        body = resp.json()
        assert body["code"] == 429
        assert body["data"] is None
        assert "Too many requests" in body["message"]
        assert counter_value(RATE_LIMITED_REJECTIONS_TOTAL) >= baseline + 1

    def test_exempt_prefix_bypasses_limiter(self) -> None:
        client = TestClient(_build_middleware_app(rate_per_minute=1))
        for _ in range(5):
            assert client.get("/api/v1/sim/health").status_code == 200

    def test_unprotected_prefix_bypasses_limiter(self) -> None:
        client = TestClient(_build_middleware_app(rate_per_minute=1))
        for _ in range(5):
            assert client.get("/open/no-limit").status_code == 200

    def test_options_preflight_bypasses_limiter(self) -> None:
        client = TestClient(_build_middleware_app(rate_per_minute=1))
        assert client.get("/api/v1/sim/ping").status_code == 200
        assert client.options("/api/v1/sim/ping").status_code in (200, 405)

    def test_disabled_middleware_passes_through(self) -> None:
        client = TestClient(_build_middleware_app(rate_per_minute=1, enabled=False))
        for _ in range(5):
            assert client.get("/api/v1/sim/ping").status_code == 200

    def test_plaintext_response_passes(self) -> None:
        app = _build_middleware_app(rate_per_minute=10)

        @app.post("/api/v1/sim/echo")
        def _echo() -> PlainTextResponse:
            return PlainTextResponse("pong")

        client = TestClient(app)
        assert client.post("/api/v1/sim/echo").text == "pong"


# ─── LoginFailureGuard ───────────────────────────────────────────────────────


class TestLoginFailureGuard:
    def test_no_backoff_below_threshold(self) -> None:
        guard = LoginFailureGuard(max_failures=3)
        guard.record_failure("ip")
        guard.record_failure("ip")
        assert guard.failures("ip") == 2
        assert guard.retry_after("ip") is None

    def test_backoff_engages_at_threshold_and_grows_exponentially(self) -> None:
        guard = LoginFailureGuard(max_failures=2, base_seconds=2.0, max_seconds=60.0)
        guard.record_failure("ip")
        guard.record_failure("ip")  # 第 2 次失败 → 退避 2s
        first = guard.retry_after("ip")
        assert first is not None and 0 < first <= 2.0
        guard.record_failure("ip")  # 第 3 次 → 退避 4s
        second = guard.retry_after("ip")
        assert second is not None and second > 2.0

    def test_backoff_capped_at_max_seconds(self) -> None:
        guard = LoginFailureGuard(max_failures=1, base_seconds=2.0, max_seconds=3.0)
        for _ in range(8):
            guard.record_failure("ip")
        remaining = guard.retry_after("ip")
        assert remaining is not None and remaining <= 3.0

    def test_backoff_expires_allows_retry(self) -> None:
        guard = LoginFailureGuard(max_failures=1, base_seconds=0.05, max_seconds=0.05)
        guard.record_failure("ip")
        assert guard.retry_after("ip") is not None
        time.sleep(0.08)
        assert guard.retry_after("ip") is None

    def test_reset_clears_state(self) -> None:
        guard = LoginFailureGuard(max_failures=1)
        guard.record_failure("ip")
        guard.reset("ip")
        assert guard.failures("ip") == 0
        assert guard.retry_after("ip") is None


# ─── /auth/token 退避集成路径 ────────────────────────────────────────────────


class TestTokenEndpointBackoff:
    @pytest.fixture()
    def auth_client(self) -> Any:
        settings = HunterSimSettings(
            env="dev",
            api=APISettings(
                rate_limit_enabled=False,
                login_max_failed_attempts=2,
                login_backoff_base_seconds=5.0,
                login_backoff_max_seconds=30.0,
            ),
        )
        application = create_app(settings)
        with TestClient(application) as c:
            yield c, application

    def _post_token(self, client: TestClient, password: str) -> Any:
        return client.post(
            "/api/v1/sim/auth/token",
            json={"username": "admin", "password": password},
        )

    # TestClient 默认 transport 不携带 client 地址 → request.client is None → key="unknown"
    _CLIENT_KEY = "unknown"

    def test_failures_engage_backoff_429(self, auth_client: Any) -> None:
        client, application = auth_client
        baseline = counter_value(LOGIN_REJECTIONS_TOTAL, reason="backoff")
        assert self._post_token(client, "wrong-1").status_code == 401
        assert self._post_token(client, "wrong-2").status_code == 401
        resp = self._post_token(client, "wrong-3")
        assert resp.status_code == 429
        assert int(resp.headers["Retry-After"]) >= 1
        guard = application.state.login_failure_guard
        assert isinstance(guard, LoginFailureGuard)
        assert guard.failures(self._CLIENT_KEY) == 2
        assert counter_value(LOGIN_REJECTIONS_TOTAL, reason="backoff") >= baseline + 1
        # 401 处理器透传异常头（WWW-Authenticate / Retry-After）
        assert resp.json()["code"] == 429

    def test_success_resets_failure_counter(self, auth_client: Any) -> None:
        client, application = auth_client
        self._post_token(client, "wrong-1")
        assert self._post_token(client, "admin").status_code == 200
        guard = application.state.login_failure_guard
        assert guard.failures(self._CLIENT_KEY) == 0
        # 复位后再次失败不应立刻进入退避
        assert self._post_token(client, "wrong-2").status_code == 401
