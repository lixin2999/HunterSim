"""轻量级依赖注入容器。

上层模块通过构造函数接收 Protocol 接口，本容器负责按接口注册/解析具体实现。
脚手架阶段仅提供线程安全的类型->实例注册表，后续层实现后在此绑定。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast

T = TypeVar("T")


class Container:
    """极简服务定位器 / 依赖注入容器。

    支持按类型注册工厂或单例，并在解析时惰性构造。容器本身线程安全。
    """

    def __init__(self) -> None:
        """初始化空容器（无全局状态，实例间互不干扰）。"""
        self._lock = threading.RLock()
        self._factories: dict[type[Any], Callable[[], Any]] = {}
        self._singletons: dict[type[Any], Any] = {}
        self._transient: set[type[Any]] = set()

    def register(
        self, interface: type[T], factory: Callable[[], T], *, singleton: bool = True
    ) -> None:
        """注册接口对应的实现工厂。

        Args:
            interface: Protocol / 抽象类型键。
            factory: 无参可调用对象，返回实现实例。
            singleton: 为 True 时缓存实例，仅构造一次；为 False 时每次解析新建。
        """
        with self._lock:
            self._factories[interface] = factory
            self._singletons.pop(interface, None)
            if singleton:
                self._transient.discard(interface)
            else:
                self._transient.add(interface)

    def register_instance(self, interface: type[T], instance: T) -> None:
        """直接注册已构造好的实例（便于测试注入 Mock）。"""
        with self._lock:
            self._factories[interface] = lambda: instance
            self._singletons[interface] = instance

    def resolve(self, interface: type[T]) -> T:
        """解析接口实例。

        Args:
            interface: 已注册的接口类型。

        Returns:
            对应的实现实例。

        Raises:
            KeyError: 接口未注册。
        """
        with self._lock:
            if interface in self._singletons:
                return cast(T, self._singletons[interface])
            factory = self._factories.get(interface)
            if factory is None:
                raise KeyError(f"未注册的接口: {interface!r}")
            instance = cast(T, factory())
            if interface not in self._transient:
                self._singletons[interface] = instance
            return instance

    def __contains__(self, interface: type[Any]) -> bool:
        """判断接口是否已注册。"""
        with self._lock:
            return interface in self._factories


__all__ = ["Container"]
