"""Viz 反向代理单元测试（mTLS 场景下探针面代理管理面可视化页面）。

覆盖：
- resolve_upstream_base：TLS/非 TLS scheme 切换；
- build_ssl_kwargs：证书参数组装；
- viz_url：TLS 启用时指向探针端口；
- probes app：TLS 时注册代理路由、非 TLS 时不注册；
- proxy_request：JSON 响应转发、流式响应转发、连接失败处理。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
        assert kwargs["verify"] == "/ca.pem"

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


class TestProxyRequest:
    """代理请求转发逻辑。"""

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

    @pytest.mark.asyncio
    async def test_connect_error_returns_502(self) -> None:
        """上游不可达返回 502。"""
        import httpx

        from hunter_sim.probes.viz_proxy import proxy_request

        mock_client = MagicMock()
        mock_client.build_request = MagicMock(return_value=MagicMock())
        mock_client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/api/v1/instances"
        mock_request.url.query = ""
        mock_request.headers = {}
        mock_request.body = AsyncMock(return_value=b"")

        response = await proxy_request(mock_request, mock_client, "https://127.0.0.1:8090")
        assert response.status_code == 502


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
