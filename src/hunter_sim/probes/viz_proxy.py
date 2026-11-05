"""Viz 反向代理（mTLS 场景下通过探针面 HTTP 访问管理面可视化页面）。

当管理面（:8090）启用 TLS/mTLS 时，浏览器无法直接访问 ``/viz``（需要客户端
证书）。本模块在探针面（:8080, HTTP）注册代理路由，将 viz 页面与 API 请求
转发到管理面，使用本机证书完成 mTLS 握手，从而让浏览器直接通过
``http://<IP>:8080/viz`` 访问完整可视化功能。

设计约束：
- 仅在 ``adapter.tls_enabled`` 为 True 时激活，非 TLS 场景不注册任何代理路由；
- 代理使用 ``ADAPTER_SSL_CERTFILE``/``KEYFILE`` 作为客户端证书连接管理面
  （同一台机器、同一份证书）；管理面服务端证书复用接入包内的客户端证书
  （EKU=clientAuth），Python 客户端按 SERVER_AUTH purpose 校验会报
  ``unsuitable certificate purpose``，故本机回环代理跳过服务端证书校验
  （``verify=False``），仅靠呈现客户端证书完成 mTLS 握手；
- 流式响应（MJPEG ``multipart/x-mixed-replace``、SSE ``text/event-stream``）
  逐块转发，不缓冲全量内容。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from hunter_sim.common.utils import get_logger

if TYPE_CHECKING:
    from hunter_sim.common.models import HunterSimSettings

logger = get_logger(__name__)

#: 代理请求超时（秒）：覆盖 MJPEG/SSE 长连接场景
_PROXY_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)

#: 需要过滤的逐跳头（hop-by-hop），不应透传
_HOP_BY_HOP_HEADERS = frozenset({
    "host", "connection", "transfer-encoding", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "upgrade",
})


def build_ssl_kwargs(settings: "HunterSimSettings") -> dict[str, Any]:
    """根据 adapter TLS 配置构建 httpx 客户端 SSL 参数。

    Returns:
        用于 ``httpx.AsyncClient(verify=..., cert=...)`` 的关键字参数字典；
        非 TLS 时返回空字典。
    """
    adapter = settings.adapter
    if not adapter.tls_enabled:
        return {}
    # 本机回环代理：管理面服务端证书复用接入包内的客户端证书（EKU=clientAuth），
    # Python 客户端按 SERVER_AUTH purpose 校验会报 "unsuitable certificate purpose"，
    # 故跳过服务端证书校验（verify=False）；回环不跨网，不验证服务端证书可接受。
    kwargs: dict[str, Any] = {"verify": False}
    # 客户端证书（与 server 端同一份，本机 mTLS：管理面 CERT_REQUIRED 仍会校验该证书）
    if adapter.ssl_certfile and adapter.ssl_keyfile:
        kwargs["cert"] = (adapter.ssl_certfile, adapter.ssl_keyfile)
    return kwargs


def resolve_upstream_base(settings: "HunterSimSettings") -> str:
    """计算探针代理管理面的 base URL。

    TLS 启用时自动切换为 https scheme，host 回落到 127.0.0.1（本机回环）。
    """
    adapter = settings.adapter
    scheme = "https" if adapter.tls_enabled else "http"
    # 通配地址回落到 127.0.0.1（探针与管理面同机）
    host = (adapter.host or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{adapter.port}"


async def proxy_request(
    request: Request,
    client: httpx.AsyncClient,
    upstream_base: str,
) -> Response:
    """将单个请求透明转发到上游管理面。

    支持普通 JSON 响应和流式响应（MJPEG / SSE），流式响应逐块 yield、不缓冲。
    """
    # 构建目标 URL
    path = request.url.path
    query = str(request.url.query) if request.url.query else ""
    url = f"{upstream_base}{path}"
    if query:
        url = f"{url}?{query}"

    # 透传 headers（过滤 hop-by-hop）
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() not in _HOP_BY_HOP_HEADERS:
            headers[key] = value

    body = await request.body()

    # 发送请求（stream=True 以支持流式转发）
    upstream_request = client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=body if body else None,
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.ConnectError as exc:
        logger.warning(f"Viz proxy connect failed: {exc}")
        return Response(
            content='{"detail": "upstream unavailable"}',
            status_code=502,
            media_type="application/json",
        )
    except httpx.TimeoutException:
        return Response(
            content='{"detail": "upstream timeout"}',
            status_code=504,
            media_type="application/json",
        )

    content_type = upstream_response.headers.get("content-type", "")

    # 判断是否需要流式转发
    if _is_streaming(content_type):
        async def _stream() -> Any:
            try:
                async for chunk in upstream_response.aiter_bytes(chunk_size=4096):
                    yield chunk
            finally:
                await upstream_response.aclose()

        # 过滤响应头中的 hop-by-hop
        resp_headers = {
            k: v for k, v in upstream_response.headers.items()
            if k.lower() not in _HOP_BY_HOP_HEADERS
        }
        return StreamingResponse(
            _stream(),
            status_code=upstream_response.status_code,
            headers=resp_headers,
            media_type=content_type or None,
        )

    # 普通响应：读取全量后返回
    content = await upstream_response.aread()
    await upstream_response.aclose()

    resp_headers = {
        k: v for k, v in upstream_response.headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS
    }
    return Response(
        content=content,
        status_code=upstream_response.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


def _is_streaming(content_type: str) -> bool:
    """判断 content-type 是否为需要流式转发的类型。"""
    ct = content_type.lower()
    return any(
        marker in ct
        for marker in (
            "multipart/x-mixed-replace",
            "text/event-stream",
            "stream",
        )
    )


def register_viz_proxy(app: FastAPI, settings: "HunterSimSettings") -> None:
    """在探针应用上注册 viz 代理路由（仅 TLS 启用时调用）。

    httpx client 与 upstream base 由 lifespan 在应用启动后写入 ``app.state``
    （``viz_proxy_client`` / ``viz_proxy_upstream``），因此路由函数内部延迟从
    ``app.state`` 取 client——注册期 lifespan 尚未执行。

    路由优先级：先注册具体路径（静态资产），再注册 catch-all 代理。
    """
    from hunter_sim.visualization.routes import ASSETS, ASSETS_DIR

    def _get_proxy(request: Request) -> tuple[Any, str]:
        """从 app.state 获取代理 client 与 upstream base。"""
        client = getattr(request.app.state, "viz_proxy_client", None)
        upstream = getattr(request.app.state, "viz_proxy_upstream", "")
        if client is None:
            return None, upstream
        return client, upstream

    @app.get("/viz", include_in_schema=False, response_class=HTMLResponse)
    async def viz_page() -> HTMLResponse:
        """直接返回 viz HTML 页面（静态内容，无需代理）。"""
        html_path = ASSETS_DIR / "index.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    @app.get("/viz/assets/{name}", include_in_schema=False)
    async def viz_asset(name: str) -> Response:
        """直接返回白名单静态资产。"""
        media_type = ASSETS.get(name)
        if media_type is None:
            return Response(content=b"Not Found", status_code=404, media_type="text/plain")
        asset_path = ASSETS_DIR / name
        if not asset_path.exists():
            return Response(content=b"Not Found", status_code=404, media_type="text/plain")
        return Response(content=asset_path.read_bytes(), media_type=media_type)

    @app.api_route(
        "/viz/api/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    async def proxy_viz_api(request: Request, path: str) -> Response:
        """代理 /viz/api/* 到管理面。"""
        client, upstream = _get_proxy(request)
        if client is None:
            return Response(
                content='{"detail": "viz proxy not ready"}',
                status_code=503,
                media_type="application/json",
            )
        return await proxy_request(request, client, upstream)

    @app.api_route(
        "/api/v1/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    async def proxy_api_v1(request: Request, path: str) -> Response:
        """代理 /api/v1/* 到管理面（实例列表、MJPEG 流、截图等）。"""
        client, upstream = _get_proxy(request)
        if client is None:
            return Response(
                content='{"detail": "viz proxy not ready"}',
                status_code=503,
                media_type="application/json",
            )
        return await proxy_request(request, client, upstream)

    @app.get("/docs", include_in_schema=False)
    async def proxy_docs(request: Request) -> Response:
        """代理 /docs 到管理面。"""
        client, upstream = _get_proxy(request)
        if client is None:
            return Response(
                content='{"detail": "viz proxy not ready"}',
                status_code=503,
                media_type="application/json",
            )
        return await proxy_request(request, client, upstream)

    @app.get("/openapi.json", include_in_schema=False)
    async def proxy_openapi(request: Request) -> Response:
        """代理 /openapi.json 到管理面。"""
        client, upstream = _get_proxy(request)
        if client is None:
            return Response(
                content='{"detail": "viz proxy not ready"}',
                status_code=503,
                media_type="application/json",
            )
        return await proxy_request(request, client, upstream)

    upstream_base = resolve_upstream_base(settings)
    logger.info(f"Viz proxy routes registered (upstream={upstream_base})")


__all__ = [
    "build_ssl_kwargs",
    "proxy_request",
    "register_viz_proxy",
    "resolve_upstream_base",
]
