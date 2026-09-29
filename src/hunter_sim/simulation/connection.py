"""模块 1.1：CARLA 连接管理器实现。

- 封装 ``carla.Client``，支持超时、指数退避自动重连、连接状态监控。
- ``World`` 懒加载与缓存；``load_world`` 用于切换地图。
- 通过 ``client_factory`` 注入实现与真实 CARLA 的解耦，便于单测（无需服务端）。
- 实现异步上下文管理器：进入时连接、退出时断开。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any

from hunter_sim.core.config import ConnectionConfig
from hunter_sim.core.exceptions import CarlaConnectionError
from hunter_sim.core.logging import logger

if TYPE_CHECKING:
    import carla

ClientFactory = Callable[[str, int, float], Any]


def _default_client_factory(host: str, port: int, timeout: float) -> carla.Client:
    """默认工厂：惰性导入 ``carla`` 并构造客户端。

    仅在连接真实服务端时调用，使模块导入不强依赖 CARLA 已安装。
    """
    import carla

    client = carla.Client(host, port)
    client.set_timeout(timeout)
    return client


class CarlaConnectionManagerImpl:
    """满足 :class:`~hunter_sim.simulation.protocols.CarlaConnectionManager` 契约。"""

    def __init__(
        self,
        config: ConnectionConfig,
        *,
        client_factory: ClientFactory | None = None,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 8.0,
    ) -> None:
        """初始化连接管理器。

        Args:
            config: 连接参数（host / port / 超时 / 重试次数等）。
            client_factory: ``(host, port, timeout) -> client`` 工厂，默认使用真实
                ``carla.Client``；测试可注入 mock 工厂。
            base_backoff_seconds: 指数退避基数。
            max_backoff_seconds: 退避上限。
        """
        self._config = config
        self._client_factory = client_factory or _default_client_factory
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._client: Any | None = None
        self._world: Any | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        """当前是否处于已连接状态。"""
        return self._connected

    async def connect(self) -> None:
        """建立连接，失败时指数退避重试（最多 ``max_retries`` 次）。

        Raises:
            CarlaConnectionError: 所有重试均失败。
        """
        if self._connected:
            return

        attempts = max(1, self._config.max_retries)
        last_exc: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                client = await asyncio.to_thread(
                    self._client_factory,
                    self._config.host,
                    self._config.port,
                    self._config.timeout_seconds,
                )
                # 触发一次 RPC 探活，确保服务端可达。
                version = await asyncio.to_thread(client.get_server_version)
                self._client = client
                self._world = None
                self._connected = True
                logger.bind(component="connection").info(
                    "已连接 CARLA {}:{} (server={})", self._config.host, self._config.port, version
                )
                return
            except Exception as exc:  # 网络 / 构造异常统一进入退避重试
                last_exc = exc
                if attempt < attempts:
                    delay = min(self._max_backoff, self._base_backoff * (2 ** (attempt - 1)))
                    logger.bind(component="connection").warning(
                        "连接 CARLA 失败 (attempt {}/{}): {}，{:.2f}s 后重试",
                        attempt,
                        attempts,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise CarlaConnectionError(
            f"连接 CARLA {self._config.host}:{self._config.port} 失败，"
            f"已重试 {attempts} 次: {last_exc}"
        ) from last_exc

    async def disconnect(self) -> None:
        """断开连接并清理缓存状态。"""
        self._connected = False
        self._world = None
        self._client = None
        logger.bind(component="connection").info("已断开 CARLA 连接")

    def get_client(self) -> Any:
        """返回底层客户端。

        Raises:
            CarlaConnectionError: 尚未连接。
        """
        if not self._connected or self._client is None:
            raise CarlaConnectionError("尚未连接 CARLA，请先调用 connect()")
        return self._client

    def get_world(self, *, reload: bool = False) -> Any:
        """返回当前世界，懒加载并缓存。

        Args:
            reload: 为 True 时强制刷新缓存。
        """
        client = self.get_client()
        if reload or self._world is None:
            self._world = client.get_world()
        return self._world

    async def load_world(self, map_name: str) -> Any:
        """切换地图并刷新世界缓存。"""
        client = self.get_client()
        world = await asyncio.to_thread(client.load_world, map_name)
        self._world = world
        logger.bind(component="connection").info("已加载地图: {}", map_name)
        return world

    async def __aenter__(self) -> CarlaConnectionManagerImpl:
        """进入上下文：建立连接并返回自身。"""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """退出上下文：断开连接并清理资源。"""
        await self.disconnect()


__all__ = ["CarlaConnectionManagerImpl", "ClientFactory"]
