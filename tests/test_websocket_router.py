"""WebSocket 状态推送路由测试（PROMPT-API-002）。

覆盖 WebSocketConnectionManager 的连接/断开/广播/计数逻辑，
以及通过 TestClient 驱动真实端点（connected / ping-pong / unsubscribe）。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import hunter_sim.api.routers.websocket as ws_mod
from hunter_sim.api.routers.websocket import WebSocketConnectionManager


class _FakeWS:
    """最小 WebSocket 替身，记录发送的消息。"""

    def __init__(self, recv_text: str = "") -> None:
        self.sent: list[str] = []
        self.accepted = False
        self._recv_text = recv_text
        self._recv_once = False
        self.raise_on_send = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        if self.raise_on_send:
            raise RuntimeError("broken pipe")
        self.sent.append(text)

    async def receive_text(self) -> str:
        if self._recv_once:
            # 第二次接收模拟断开
            raise ConnectionError("closed")
        self._recv_once = True
        return self._recv_text


class TestConnectionManager:
    def test_connect_registers(self) -> None:
        mgr = WebSocketConnectionManager()
        ws = _FakeWS()

        async def _run() -> None:
            await mgr.connect("i1", ws)

        asyncio.run(_run())
        assert ws.accepted is True
        assert mgr.get_connection_count("i1") == 1

    def test_disconnect_removes(self) -> None:
        mgr = WebSocketConnectionManager()
        ws = _FakeWS()

        async def _run() -> None:
            await mgr.connect("i1", ws)
            await mgr.disconnect("i1", ws)

        asyncio.run(_run())
        assert mgr.get_connection_count("i1") == 0

    def test_broadcast_sends_to_all(self) -> None:
        mgr = WebSocketConnectionManager()
        a, b = _FakeWS(), _FakeWS()

        async def _run() -> None:
            await mgr.connect("i1", a)
            await mgr.connect("i1", b)
            await mgr.broadcast("i1", {"type": "status_update", "fps": 50})

        asyncio.run(_run())
        assert json.loads(a.sent[0])["fps"] == 50
        assert json.loads(b.sent[0])["fps"] == 50

    def test_broadcast_cleans_broken(self) -> None:
        mgr = WebSocketConnectionManager()
        good, bad = _FakeWS(), _FakeWS()
        bad.raise_on_send = True

        async def _run() -> None:
            await mgr.connect("i1", good)
            await mgr.connect("i1", bad)
            await mgr.broadcast("i1", {"x": 1})

        asyncio.run(_run())
        # 发送失败的连接被清理
        assert mgr.get_connection_count("i1") == 1

    def test_broadcast_unknown_instance_noop(self) -> None:
        mgr = WebSocketConnectionManager()

        async def _run() -> None:
            await mgr.broadcast("ghost", {"x": 1})

        asyncio.run(_run())  # 不应抛异常
        assert mgr.get_connection_count("ghost") == 0

    def test_get_connection_count_empty(self) -> None:
        mgr = WebSocketConnectionManager()
        assert mgr.get_connection_count("none") == 0


class _HangingWS(_FakeWS):
    """发送永不返回的慢客户端替身。"""

    async def send_text(self, text: str) -> None:
        await asyncio.sleep(10)


class TestBroadcastIsolation:
    def test_slow_client_does_not_block_others(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """V2.10.0：并发发送 + 超时，慢客户端不得拖住同实例其他订阅者。"""
        monkeypatch.setattr(ws_mod, "_WS_SEND_TIMEOUT_SECONDS", 0.05)
        mgr = WebSocketConnectionManager()
        slow, fast = _HangingWS(), _FakeWS()

        async def _run() -> float:
            await mgr.connect("i1", slow)
            await mgr.connect("i1", fast)
            started = time.monotonic()
            await mgr.broadcast("i1", {"x": 1})
            return time.monotonic() - started

        elapsed = asyncio.run(_run())
        assert elapsed < 1.0
        assert mgr.get_connection_count("i1") == 1  # 超时连接被清理
        assert len(fast.sent) == 1


class TestRevocationCheck:
    def _ws(self, store: Any) -> Any:
        return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(state_store=store)))

    def test_revoked_jti_detected(self) -> None:
        ws = self._ws(SimpleNamespace(is_token_revoked=lambda jti: jti == "j1"))
        assert asyncio.run(ws_mod._is_token_revoked(ws, {"jti": "j1"})) is True
        assert asyncio.run(ws_mod._is_token_revoked(ws, {"jti": "j2"})) is False

    def test_missing_jti_or_store_is_valid(self) -> None:
        assert asyncio.run(ws_mod._is_token_revoked(self._ws(None), {})) is False
        assert asyncio.run(
            ws_mod._is_token_revoked(self._ws(SimpleNamespace()), {"jti": "j1"})
        ) is False

    def test_store_failure_treated_as_valid(self) -> None:
        def boom(jti: str) -> bool:
            raise RuntimeError("db down")

        ws = self._ws(SimpleNamespace(is_token_revoked=boom))
        assert asyncio.run(ws_mod._is_token_revoked(ws, {"jti": "j1"})) is False


class TestWebsocketEndpoint:
    def test_connected_and_ping_pong(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        token = auth_headers["Authorization"].removeprefix("Bearer ")
        with client.websocket_connect(f"/api/v1/sim/ws/sim/inst-ws-1/status?token={token}") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "connected"
            assert hello["instance_id"] == "inst-ws-1"

            ws.send_text(json.dumps({"type": "ping"}))
            pong = ws.receive_json()
            assert pong["type"] == "pong"

            ws.send_text(json.dumps({"type": "unsubscribe"}))

    def test_invalid_json_ignored_then_unsubscribe(
        self, client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        token = auth_headers["Authorization"].removeprefix("Bearer ")
        with client.websocket_connect(f"/api/v1/sim/ws/sim/inst-ws-2/status?token={token}") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_text("not-json")  # 被忽略
            ws.send_text(json.dumps({"type": "unsubscribe"}))

    def test_rejects_missing_token(self, client: TestClient) -> None:
        """未携带令牌的握手必须被拒绝（修复 WS 零鉴权）。"""
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/sim/ws/sim/inst-ws-3/status"):
                pass

    def test_rejects_invalid_token(self, client: TestClient) -> None:
        """伪造令牌的握手必须被拒绝。"""
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/sim/ws/sim/inst-ws-4/status?token=not-a-jwt"):
                pass
