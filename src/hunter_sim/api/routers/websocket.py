"""WebSocket 状态推送路由（PROMPT-API-002）。

提供 /ws/sim/{instance_id}/status WebSocket 端点，
实时推送仿真实例状态（位置、速度、FPS、场景进度等）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from hunter_sim.api.deps import decode_token, get_api_settings, get_default_tenant, get_user_role
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)
router = APIRouter()

# WebSocket 自定义关闭码（4000+ 为应用层保留段）
_WS_UNAUTHORIZED = 4401
_WS_FORBIDDEN = 4403
_ADMIN_ROLE = "admin"


class WebSocketConnectionManager:
    """管理多客户端对同一实例状态的 WebSocket 订阅。

    每个 instance_id 对应一组活跃连接，
    状态更新通过 asyncio.Queue 广播给所有订阅者。
    """

    def __init__(self) -> None:
        # instance_id -> list of active WebSocket connections
        self._connections: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, instance_id: str, websocket: WebSocket) -> None:
        """接受并注册 WebSocket 连接。"""
        await websocket.accept()
        async with self._lock:
            if instance_id not in self._connections:
                self._connections[instance_id] = []
            self._connections[instance_id].append(websocket)
        logger.info(f"WebSocket connected: instance={instance_id}, total={len(self._connections[instance_id])}")

    async def disconnect(self, instance_id: str, websocket: WebSocket) -> None:
        """移除已断开的连接。"""
        async with self._lock:
            conns = self._connections.get(instance_id, [])
            if websocket in conns:
                conns.remove(websocket)
            if not conns:
                self._connections.pop(instance_id, None)
        logger.info(f"WebSocket disconnected: instance={instance_id}")

    async def broadcast(self, instance_id: str, payload: dict[str, Any]) -> None:
        """向指定实例的所有订阅者推送消息。

        Args:
            instance_id: 实例 ID。
            payload: 要推送的 JSON 数据。
        """
        async with self._lock:
            connections = list(self._connections.get(instance_id, []))

        message = json.dumps(payload, default=str)
        disconnected: list[WebSocket] = []

        for ws in connections:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)

        # 清理断开的连接
        for ws in disconnected:
            await self.disconnect(instance_id, ws)

    def get_connection_count(self, instance_id: str) -> int:
        """返回指定实例的活跃连接数。"""
        return len(self._connections.get(instance_id, []))


# 全局连接管理器（由 main.py 在启动时注入到 app.state）
_ws_manager = WebSocketConnectionManager()


def get_ws_manager() -> WebSocketConnectionManager:
    """获取全局 WebSocket 管理器单例。"""
    return _ws_manager


def _extract_token(websocket: WebSocket) -> str:
    """从查询参数或 Authorization 头提取 JWT（浏览器 WebSocket 无法自定义头）。"""
    token = websocket.query_params.get("token", "")
    if token:
        return token
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return ""


async def _authorize(websocket: WebSocket, instance_id: str) -> bool:
    """握手鉴权：校验 JWT 与实例归属，失败时发送 4401/4403 关闭帧。

    中间件已将 ``/ws/`` 加入白名单（WebSocket 握手无法携带 Bearer 头），
    因此鉴权必须在此处完成，否则任何人都能订阅任意实例状态。
    """
    token = _extract_token(websocket)
    if not token:
        await websocket.close(code=_WS_UNAUTHORIZED, reason="Missing token")
        return False
    try:
        payload = decode_token(token, get_api_settings())
    except HTTPException as exc:
        await websocket.close(code=_WS_UNAUTHORIZED, reason="Invalid or expired token")
        logger.warning(f"WebSocket auth failed for instance {instance_id}: {exc.detail}")
        return False

    user_id = str(payload.get("sub", ""))
    tenant = str(payload.get("tenant") or get_default_tenant())
    role = get_user_role(payload)
    manager = getattr(websocket.app.state, "instance_manager", None)
    instance = manager.get_instance(instance_id) if manager is not None else None
    if instance is not None and role != _ADMIN_ROLE:
        # V2.5.0：跨租户订阅一律拒绝；同租户非本人同样拒绝
        if instance.tenant != tenant:
            await websocket.close(code=_WS_FORBIDDEN, reason="Forbidden")
            logger.warning(
                f"WebSocket tenant denied: tenant '{tenant}' -> instance {instance_id} "
                f"(tenant '{instance.tenant}')"
            )
            return False
        if instance.user_id and instance.user_id != user_id:
            await websocket.close(code=_WS_FORBIDDEN, reason="Forbidden")
            logger.warning(f"WebSocket access denied: user '{user_id}' -> instance {instance_id}")
            return False
    return True


@router.websocket("/ws/sim/{instance_id}/status")
async def websocket_instance_status(websocket: WebSocket, instance_id: str) -> None:
    """WebSocket /api/v1/sim/ws/sim/{instance_id}/status

    订阅指定仿真实例的实时状态推送。
    消息格式：
    {
        "type": "status_update",
        "instance_id": "xxx",
        "timestamp": 1234567890.123,
        "data": {
            "status": "running",
            "fps": 50,
            "sim_time": 10.5,
            "vehicle": {
                "x": 1.0, "y": 2.0, "z": 0.0,
                "yaw": 0.5, "speed": 3.2
            },
            "scene": {
                "scene_id": "xxx",
                "progress": 0.5
            }
        }
    }
    """
    # 握手鉴权：中间件无法覆盖 WS，必须在 accept 前完成，否则任何人都能订阅任意实例
    if not await _authorize(websocket, instance_id):
        return
    await _ws_manager.connect(instance_id, websocket)
    try:
        # 发送连接确认消息
        await websocket.send_text(
            json.dumps(
                {
                    "type": "connected",
                    "instance_id": instance_id,
                    "timestamp": time.time(),
                    "message": "Subscribed to instance status updates",
                },
                default=str,
            )
        )

        # 保持连接，等待客户端消息（心跳或取消订阅）
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                msg = json.loads(data) if data else {}
                msg_type = msg.get("type", "")
                if msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong", "timestamp": time.time()}))
                elif msg_type == "unsubscribe":
                    break
            except asyncio.TimeoutError:
                # 60 秒无消息，发送心跳
                try:
                    await websocket.send_text(json.dumps({"type": "heartbeat", "timestamp": time.time()}))
                except Exception:
                    break
            except json.JSONDecodeError:
                pass  # 忽略无效 JSON

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug(f"WebSocket error for instance {instance_id}: {exc}")
    finally:
        await _ws_manager.disconnect(instance_id, websocket)
