"""container.py 依赖注入容器单元测试。"""

from __future__ import annotations

import pytest

from hunter_sim.container import Container


class _Service:
    def __init__(self) -> None:
        self.value = 42


class _Other:
    pass


def test_register_and_resolve_singleton() -> None:
    c = Container()
    c.register(_Service, _Service)
    first = c.resolve(_Service)
    second = c.resolve(_Service)
    assert isinstance(first, _Service)
    assert first is second  # 默认单例


def test_register_instance_used_directly() -> None:
    c = Container()
    injected = _Service()
    c.register_instance(_Service, injected)
    assert c.resolve(_Service) is injected


def test_resolve_unregistered_raises() -> None:
    c = Container()
    with pytest.raises(KeyError):
        c.resolve(_Other)


def test_contains() -> None:
    c = Container()
    assert _Service not in c
    c.register(_Service, _Service)
    assert _Service in c
