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
  逐块透传：既不缓冲全量内容，也不重组上游分块边界（一帧/一事件一到即下发）。
"""

from __future__ import annotations

import time
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

#: 长连接正常结束类异常：浏览器关闭页面、上游主动断流都会抛出这里
#: （``httpx.ReadError`` 的 ``str()`` 为空，旧版日志只留下一行莫名其妙的
#: ``ReadError:``，现归为正常断流，按 DEBUG 计数 + 限频 INFO 汇总）
_STREAM_END_ERRORS = (
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.StreamClosed,
)

#: 同类告警的最小间隔（秒）：浏览器频繁重连时避免刷屏
_WARN_INTERVAL_S = 60.0

#: 限频状态：key -> (上次输出时刻, 窗口内被抑制次数)
_warn_state: dict[str, list[float]] = {}

#: 断流汇总：(上次输出时刻, 累计次数)
_stream_end_state: list[float] = [0.0, 0.0]


def _describe_exc(exc: BaseException) -> str:
    """异常可读描述：httpx 部分异常 ``str()`` 为空串，不能直接拼到日志里。"""
    name = type(exc).__name__
    text = str(exc).strip()
    return f"{name}: {text}" if text else name


def _rate_limited_warning(key: str, message: str) -> None:
    """按 ``key`` 限频告警：首次立即输出，窗口内只计数，超窗时带上次数。"""
    now = time.monotonic()
    state = _warn_state.setdefault(key, [0.0, 0.0])
    if now - state[0] < _WARN_INTERVAL_S:
        state[1] += 1
        return
    if state[0] and state[1]:
        message = f"{message}（同类告警 {int(state[1]) + 1} 次/{now - state[0]:.0f}s）"
    state[0], state[1] = now, 0.0
    logger.warning(message)


def _note_stream_end(exc: BaseException, path: str) -> None:
    """记录流式响应正常结束：逐条 DEBUG，每分钟一条 INFO 汇总。"""
    logger.debug(f"Viz proxy stream ended: {_describe_exc(exc)} path={path}")
    now = time.monotonic()
    _stream_end_state[1] += 1
    if _stream_end_state[0] == 0.0:
        _stream_end_state[0] = now
        return
    if now - _stream_end_state[0] >= _WARN_INTERVAL_S:
        logger.info(
            "Viz proxy 长连接断开汇总：近 %.0fs 共 %d 次（页面关闭/上游重连，属正常）",
            now - _stream_end_state[0],
            int(_stream_end_state[1]),
        )
        _stream_end_state[0], _stream_end_state[1] = now, 0.0


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
    except httpx.TimeoutException as exc:
        _rate_limited_warning(
            "timeout", f"Viz proxy timeout: {_describe_exc(exc)} url={url}"
        )
        return _proxy_error(504, "upstream timeout")
    except _STREAM_END_ERRORS as exc:
        # 上游已连接但握手/首包前断开：典型为管理面重启或浏览器取消请求，
        # 回环内自愈，限频告警即可（旧版每秒一条 WARNING 且异常体为空）
        _rate_limited_warning(
            "upstream_disconnect",
            f"Viz proxy upstream disconnected: {_describe_exc(exc)} url={url}"
            "（管理面重启/连接被中断，已返回 502；huntersim up 会自动拉起控制面）",
        )
        return _proxy_error(502, "upstream unavailable")
    except httpx.HTTPError as exc:
        # ConnectError 等其余传输错误：上游不可达，必须映射为 502：
        # 早期只捕 ConnectError，ReadError 会冒泡成 500 + ASGI traceback。
        _rate_limited_warning(
            "upstream_error", f"Viz proxy upstream error: {_describe_exc(exc)} url={url}"
        )
        return _proxy_error(502, "upstream unavailable")
    except Exception as exc:  # noqa: BLE001  代理层兜底：上游任何异常都不得变成 500
        _rate_limited_warning(
            "unexpected", f"Viz proxy unexpected error: {_describe_exc(exc)} url={url}"
        )
        return _proxy_error(502, "upstream error")

    content_type = upstream_response.headers.get("content-type", "")

    # 判断是否需要流式转发
    if _is_streaming(content_type):
        async def _stream() -> Any:
            try:
                # aiter_raw：按上游原分块逐段透传。httpx 的
                # aiter_bytes(chunk_size=N) 会攒满 N 字节才吐一段，导致
                # SSE 回放与小尺寸 MJPEG 帧被整段拖住。
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            except _STREAM_END_ERRORS as exc:
                # 客户端关页 / 上游主动断流：MJPEG・SSE 的正常生命周期，
                # 已发出的帧不受影响，只计数不告警
                _note_stream_end(exc, path)
            except Exception as exc:  # noqa: BLE001  响应头已下发，无法再改状态码
                _rate_limited_warning(
                    "stream_error",
                    f"Viz proxy stream interrupted: {_describe_exc(exc)} path={path}",
                )
            finally:
                await _safe_aclose(upstream_response)

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
    try:
        content = await upstream_response.aread()
    except Exception as exc:  # noqa: BLE001  读体阶段断连同样不得变成 500
        _rate_limited_warning(
            "body_read",
            f"Viz proxy body read failed: {_describe_exc(exc)} path={path}",
        )
        return _proxy_error(502, "upstream read error")
    finally:
        await _safe_aclose(upstream_response)

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


def _proxy_error(status_code: int, detail: str) -> Response:
    """构造代理错误响应（JSON；不抛异常）。"""
    return Response(
        content=f'{{"detail": "{detail}"}}',
        status_code=status_code,
        media_type="application/json",
    )


async def _safe_aclose(response: httpx.Response) -> None:
    """关闭上游响应；关闭失败只记 DEBUG（客户端已断开时常见）。"""
    try:
        await response.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Viz proxy upstream close failed: {exc}")


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
