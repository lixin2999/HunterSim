"""Viz 反向代理单元测试（mTLS 场景下探针面代理管理面可视化页面）。

覆盖：
- resolve_upstream_base：TLS/非 TLS scheme 切换；
- build_ssl_kwargs：证书参数组装；
- viz_url：TLS 启用时指向探针端口；
- probes app：TLS 时注册代理路由、非 TLS 时不注册；
- proxy_request：JSON 响应转发、流式响应转发、连接失败处理、
  SSE/MJPEG 分块边界透传（V3.3.4）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from hunter_sim.common.models import AdapterSettings, HunterSimSettings, ProbeSettings
from hunter_sim.probes.app import create_probes_app
from hunter_sim.probes.viz_proxy import (
    build_ssl_kwargs,
    resolve_upstream_base,
)
from hunter_sim.visualization.routes import viz_url


# ── resolve_upstream_base ────────────────────────────────────────────────────


class TestResolveUpstreamBase:
    """upstream base URL 计算。"""

    def test_plain_http_when_no_tls(self) -> None:
        settings = HunterSimSettings(
            adapter=AdapterSettings(host="127.0.0.1", port=8090),
        )
        assert resolve_upstream_base(settings) == "http://127.0.0.1:8090"

    def test_https_when_tls_enabled(self) -> None:
        settings = HunterSimSettings(
            adapter=AdapterSettings(
                host="0.0.0.0",
                port=8090,
                ssl_certfile="/path/cert.pem",
                ssl_keyfile="/path/key.pem",
            ),
        )
        # 通配地址回落 127.0.0.1
        assert resolve_upstream_base(settings) == "https://127.0.0.1:8090"

    def test_custom_host(self) -> None:
        settings = HunterSimSettings(
            adapter=AdapterSettings(
                host="192.168.1.50",
                port=9090,
                ssl_certfile="/c.pem",
                ssl_keyfile="/k.pem",
            ),
        )
        assert resolve_upstream_base(settings) == "https://192.168.1.50:9090"


# ── build_ssl_kwargs ─────────────────────────────────────────────────────────


class TestBuildSslKwargs:
    """httpx SSL 参数构建。"""

    def test_empty_when_no_tls(self) -> None:
        settings = HunterSimSettings(adapter=AdapterSettings())
        assert build_ssl_kwargs(settings) == {}

    def test_cert_and_verify(self) -> None:
        settings = HunterSimSettings(
            adapter=AdapterSettings(
                ssl_certfile="/cert.pem",
                ssl_keyfile="/key.pem",
                ssl_ca_certfile="/ca.pem",
            ),
        )
        kwargs = build_ssl_kwargs(settings)
        assert kwargs["cert"] == ("/cert.pem", "/key.pem")
        # 本机回环代理：管理面服务端证书为 clientAuth EKU，跳过服务端校验
        assert kwargs["verify"] is False

    def test_verify_false_without_ca(self) -> None:
        settings = HunterSimSettings(
            adapter=AdapterSettings(
                ssl_certfile="/cert.pem",
                ssl_keyfile="/key.pem",
            ),
        )
        kwargs = build_ssl_kwargs(settings)
        assert kwargs["verify"] is False
        assert kwargs["cert"] == ("/cert.pem", "/key.pem")


# ── viz_url ──────────────────────────────────────────────────────────────────


class TestVizUrl:
    """viz_url 路由选择。"""

    def test_non_tls_points_to_adapter(self) -> None:
        settings = HunterSimSettings(
            adapter=AdapterSettings(host="127.0.0.1", port=8090),
            probe=ProbeSettings(host="127.0.0.1", port=8080),
        )
        assert viz_url(settings) == "http://127.0.0.1:8090/viz"

    def test_tls_points_to_probe_port(self) -> None:
        settings = HunterSimSettings(
            adapter=AdapterSettings(
                host="0.0.0.0",
                port=8090,
                ssl_certfile="/c.pem",
                ssl_keyfile="/k.pem",
            ),
            probe=ProbeSettings(host="127.0.0.1", port=8080),
        )
        # mTLS 下 viz 通过探针面 HTTP 代理
        assert viz_url(settings) == "http://127.0.0.1:8080/viz"

    def test_tls_probe_wildcard_host(self) -> None:
        settings = HunterSimSettings(
            adapter=AdapterSettings(
                host="0.0.0.0",
                port=8090,
                ssl_certfile="/c.pem",
                ssl_keyfile="/k.pem",
            ),
            probe=ProbeSettings(host="0.0.0.0", port=8080),
        )
        # 探针通配地址也回落 127.0.0.1
        assert viz_url(settings) == "http://127.0.0.1:8080/viz"


# ── Probes app 路由注册 ──────────────────────────────────────────────────────


class TestProbesAppRoutes:
    """验证 TLS/非 TLS 下探针应用路由注册差异。"""

    def _make_settings(self, tls: bool) -> HunterSimSettings:
        adapter_kwargs: dict[str, Any] = {"host": "127.0.0.1", "port": 8090}
        if tls:
            adapter_kwargs["ssl_certfile"] = "/fake/cert.pem"
            adapter_kwargs["ssl_keyfile"] = "/fake/key.pem"
        return HunterSimSettings(
            adapter=AdapterSettings(**adapter_kwargs),
            probe=ProbeSettings(host="127.0.0.1", port=8080),
        )

    def test_non_tls_no_viz_routes(self) -> None:
        """非 TLS 时不注册 viz 代理路由。"""
        settings = self._make_settings(tls=False)
        app = create_probes_app(settings)
        paths = [route.path for route in app.routes]
        assert "/viz" not in paths
        assert "/api/v1/{path:path}" not in paths

    def test_tls_registers_viz_routes(self) -> None:
        """TLS 时注册 viz 代理路由。"""
        settings = self._make_settings(tls=True)
        app = create_probes_app(settings)
        paths = [route.path for route in app.routes]
        assert "/viz" in paths
        assert "/viz/assets/{name}" in paths
        assert "/viz/api/{path:path}" in paths
        assert "/api/v1/{path:path}" in paths

    def test_health_still_works_non_tls(self) -> None:
        """非 TLS 时 /health 正常工作。"""
        settings = self._make_settings(tls=False)
        with TestClient(create_probes_app(settings)) as client:
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_viz_page_served_directly(self) -> None:
        """TLS 时 /viz 页面直接返回 HTML（不代理）。"""
        settings = self._make_settings(tls=True)
        with TestClient(create_probes_app(settings)) as client:
            resp = client.get("/viz")
            assert resp.status_code == 200
            assert "text/html" in resp.headers.get("content-type", "")


# ── proxy_request 转发 ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_proxy_module_state() -> Any:
    """用例前后清空模块级熔断/限频状态。

    ``_down_state`` 是跨请求全局：一个用例开启窗口后，下一个用例的
    ``proxy_request`` 会直接熔断快速失败（不发上游请求），断言会跨用例串味。
    """
    from hunter_sim.probes import viz_proxy

    viz_proxy._down_state[:] = [0.0, 0.0]  # noqa: SLF001
    viz_proxy._warn_state.clear()  # noqa: SLF001
    yield
    viz_proxy._down_state[:] = [0.0, 0.0]  # noqa: SLF001
    viz_proxy._warn_state.clear()  # noqa: SLF001


class TestProxyRequest:
    """代理请求转发逻辑。"""

    @staticmethod
    def _stream_request() -> Any:
        """GET ``/viz/api/logs/stream`` 的请求桩。"""
        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/viz/api/logs/stream"
        mock_request.url.query = ""
        mock_request.headers = {"host": "localhost"}
        mock_request.body = AsyncMock(return_value=b"")
        return mock_request

    @pytest.mark.asyncio
    async def test_stream_chunks_keep_upstream_boundaries(self) -> None:
        """SSE/MJPEG 逐段原样转发：分段边界不得被代理重组。

        回归现场：mTLS 下浏览器只能走探针面 :8080 代理，而代理用
        ``aiter_bytes(chunk_size=4096)`` 取数——httpx 会把上游小段攒满 4KB
        才吐一段，日志首屏回放和小尺寸 JPEG 帧因此迟迟不可达（画面空白、
        日志不动）。
        """
        import httpx

        from hunter_sim.probes.viz_proxy import proxy_request

        events = [b"data: log-1\n\n", b"data: log-2\n\n", b"data: log-3\n\n"]

        async def _upstream() -> Any:
            for event in events:
                yield event

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_upstream(),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await proxy_request(
                self._stream_request(), client, "http://upstream"
            )
            assert isinstance(response, StreamingResponse)
            chunks = [chunk async for chunk in response.body_iterator]

        assert chunks == events, "代理重组了上游分块（SSE/MJPEG 会被整段延迟送达）"

    @pytest.mark.asyncio
    async def test_json_forward(self) -> None:
        """普通 JSON 响应正确转发。"""
        from hunter_sim.probes.viz_proxy import proxy_request

        # Mock httpx response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.aread = AsyncMock(return_value=b'{"status": "ok"}')
        mock_response.aclose = AsyncMock()

        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(return_value=mock_response)

        # Mock FastAPI request
        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/viz/api/status"
        mock_request.url.query = ""
        mock_request.headers = {"host": "localhost", "accept": "application/json"}
        mock_request.body = AsyncMock(return_value=b"")

        response = await proxy_request(mock_request, mock_client, "https://127.0.0.1:8090")
        assert response.status_code == 200
        assert b'"status"' in response.body

    @staticmethod
    def _api_request(path: str = "/viz/api/status") -> Any:
        """普通 JSON 接口请求桩。"""
        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = path
        mock_request.url.query = ""
        mock_request.headers = {}
        mock_request.body = AsyncMock(return_value=b"")
        return mock_request

    @pytest.mark.asyncio
    async def test_connect_error_returns_503_with_retry_after(self) -> None:
        """上游不可达 = 「暂时不在」而非「网关收到无效响应」：503 + Retry-After。"""
        import httpx

        from hunter_sim.probes.viz_proxy import proxy_request

        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))

        response = await proxy_request(
            self._api_request(), mock_client, "https://127.0.0.1:8090"
        )

        assert response.status_code == 503
        assert response.headers["Retry-After"] == "2"
        assert b"unavailable" in response.body

    @pytest.mark.asyncio
    async def test_down_window_fast_fails_without_touching_upstream(self) -> None:
        """熔断窗口内不再逐个撞已断开的端口（现场「大量 502」刷屏的源头）。

        控制面原生崩溃到重启有 10~20s，/viz 有 8 个独立轮询都在这个窗口里发
        请求；旧实现每个都去建一次连接再失败，日志里就是一片 502 报警。
        """
        import httpx

        from hunter_sim.probes.viz_proxy import proxy_request

        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(side_effect=httpx.ReadError(""))

        first = await proxy_request(self._api_request(), mock_client, "http://upstream")
        assert first.status_code == 503
        assert mock_client.send.await_count == 1

        second = await proxy_request(
            self._api_request("/viz/api/instances"), mock_client, "http://upstream"
        )

        assert second.status_code == 503
        assert second.headers["Retry-After"] == "2"
        assert mock_client.send.await_count == 1, "熔断窗口内仍向已断开的上游发请求"

    @pytest.mark.asyncio
    async def test_upstream_response_closes_down_window(self) -> None:
        """窗口到期后的首次成功应答必须彻底关窗（不留残余时间戳）。

        否则控制面恢复后下一轮偶发断连会在一个“已过期”的窗口上续期，
        退避时长与现场实际情况脱节。
        """
        from hunter_sim.probes import viz_proxy
        from hunter_sim.probes.viz_proxy import proxy_request

        viz_proxy._open_down_window("test")  # noqa: SLF001
        assert viz_proxy._in_down_window() is True  # noqa: SLF001
        viz_proxy._down_state[0] -= 5.0  # noqa: SLF001  模拟窗口到期，放行一次探测

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.aread = AsyncMock(return_value=b'{"status": "ok"}')
        mock_response.aclose = AsyncMock()
        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(return_value=mock_response)

        response = await proxy_request(self._api_request(), mock_client, "http://upstream")

        assert response.status_code == 200
        assert viz_proxy._down_state == [0.0, 0.0]  # noqa: SLF001
        assert viz_proxy._in_down_window() is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_unexpected_error_still_returns_502(self) -> None:
        """非传输类异常仍归 502：不能把代码缺陷伪装成「服务不可用」。"""
        from hunter_sim.probes.viz_proxy import proxy_request

        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(side_effect=RuntimeError("bug"))

        response = await proxy_request(self._api_request(), mock_client, "http://upstream")

        assert response.status_code == 502
        assert "Retry-After" not in response.headers


# ── _fetch_upstream TLS 感知 ─────────────────────────────────────────────────


class TestFetchUpstreamTls:
    """_fetch_upstream 在 TLS 场景下使用 ssl_kwargs。"""

    def test_passes_ssl_kwargs(self) -> None:
        from hunter_sim.probes.app import _fetch_upstream

        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "ok"}
            mock_get.return_value = mock_resp

            _fetch_upstream(
                "https://127.0.0.1:8090",
                ssl_kwargs={"verify": "/ca.pem", "cert": ("/c.pem", "/k.pem")},
            )
            call_kwargs = mock_get.call_args[1]
            assert call_kwargs["verify"] == "/ca.pem"
            assert call_kwargs["cert"] == ("/c.pem", "/k.pem")

    def test_no_ssl_kwargs_plain(self) -> None:
        from hunter_sim.probes.app import _fetch_upstream

        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "ok"}
            mock_get.return_value = mock_resp

            _fetch_upstream("http://127.0.0.1:8090")
            call_kwargs = mock_get.call_args[1]
            assert "verify" not in call_kwargs
            assert "cert" not in call_kwargs
