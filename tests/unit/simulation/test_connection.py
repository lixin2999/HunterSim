"""simulation.connection 单元测试（注入 mock 客户端工厂，无需真实 CARLA）。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hunter_sim.core.config import ConnectionConfig
from hunter_sim.core.exceptions import CarlaConnectionError
from hunter_sim.simulation.connection import CarlaConnectionManagerImpl
from hunter_sim.simulation.protocols import CarlaConnectionManager


def _config(**overrides: object) -> ConnectionConfig:
    base = {
        "host": "127.0.0.1",
        "port": 2000,
        "timeout_seconds": 2.0,
        "max_retries": 5,
        "traffic_manager_port": 8000,
    }
    base.update(overrides)
    return ConnectionConfig(**base)  # type: ignore[arg-type]


def _healthy_client() -> MagicMock:
    client = MagicMock(name="client")
    client.get_server_version.return_value = "0.9.16"
    client.get_world.return_value = MagicMock(name="world")
    client.load_world.return_value = MagicMock(name="loaded_world")
    return client


def _manager(client_factory: object, **cfg: object) -> CarlaConnectionManagerImpl:
    return CarlaConnectionManagerImpl(
        _config(**cfg),
        client_factory=client_factory,  # type: ignore[arg-type]
        base_backoff_seconds=0.0,
        max_backoff_seconds=0.0,
    )


def test_satisfies_protocol() -> None:
    manager = _manager(MagicMock())
    assert isinstance(manager, CarlaConnectionManager)


async def test_connect_success_and_lazy_world() -> None:
    client = _healthy_client()
    manager = _manager(lambda host, port, timeout: client)
    await manager.connect()
    assert manager.is_connected is True
    assert manager.get_client() is client

    world = manager.get_world()
    assert world is client.get_world.return_value
    client.get_world.assert_called_once()
    # 命中缓存，不再二次调用
    manager.get_world()
    client.get_world.assert_called_once()


async def test_connect_retries_then_succeeds() -> None:
    client = _healthy_client()
    calls = {"n": 0}

    def _factory(host: str, port: int, timeout: float) -> MagicMock:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("server not ready")
        return client

    manager = _manager(_factory)
    await manager.connect()
    assert manager.is_connected is True
    assert calls["n"] == 3


async def test_connect_exhausts_retries_and_raises() -> None:
    factory = MagicMock(side_effect=RuntimeError("down"))
    manager = _manager(factory, max_retries=3)
    with pytest.raises(CarlaConnectionError):
        await manager.connect()
    assert factory.call_count == 3
    assert manager.is_connected is False


async def test_load_world_updates_cache() -> None:
    client = _healthy_client()
    manager = _manager(lambda host, port, timeout: client)
    await manager.connect()
    loaded = await manager.load_world("Town01")
    assert loaded is client.load_world.return_value
    assert manager.get_world() is loaded


def test_get_client_before_connect_raises() -> None:
    manager = _manager(MagicMock())
    with pytest.raises(CarlaConnectionError):
        manager.get_client()


async def test_disconnect_resets_state() -> None:
    client = _healthy_client()
    manager = _manager(lambda host, port, timeout: client)
    await manager.connect()
    await manager.disconnect()
    assert manager.is_connected is False


async def test_async_context_manager() -> None:
    client = _healthy_client()
    async with _manager(lambda host, port, timeout: client) as manager:
        assert manager.is_connected is True
    assert manager.is_connected is False
