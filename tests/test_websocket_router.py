"""WebSocket 状态推送路由测试（PROMPT-API-002）。

覆盖 WebSocketConnectionManager 的连接/断开/广播/计数逻辑，
以及通过 TestClient 驱动真实端点（connected / ping-pong / unsubscribe）。
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

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


class TestWebsocketEndpoint:
    def test_connected_and_ping_pong(self, client: TestClient) -> None:
        with client.websocket_connect("/api/v1/sim/ws/sim/inst-ws-1/status") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "connected"
            assert hello["instance_id"] == "inst-ws-1"

            ws.send_text(json.dumps({"type": "ping"}))
            pong = ws.receive_json()
            assert pong["type"] == "pong"

            ws.send_text(json.dumps({"type": "unsubscribe"}))

    def test_invalid_json_ignored_then_unsubscribe(self, client: TestClient) -> None:
        with client.websocket_connect("/api/v1/sim/ws/sim/inst-ws-2/status") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_text("not-json")  # 被忽略
            ws.send_text(json.dumps({"type": "unsubscribe"}))
