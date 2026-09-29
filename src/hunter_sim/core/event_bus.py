"""事件总线参考实现（进程内、线程安全）。

CARLA 回调运行在独立线程（§10 约束 4），因此本实现使用 ``threading.RLock``
保护订阅表，保证跨线程 publish/subscribe 的一致性。单个回调抛出异常不会中断
派发，异常被捕获并记录日志，符合"算法/回调异常不中断主流程"的容错要求。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress

from loguru import logger

from hunter_sim.core.events import Event


class InMemoryEventBus:
    """满足 :class:`~hunter_sim.core.protocols.EventBus` 契约的进程内实现。

    订阅按注册类型进行层次匹配：发布事件时，所有注册类型为该事件类型或其基类
    的回调都会被调用。
    """

    def __init__(self) -> None:
        """初始化空订阅表。"""
        self._lock = threading.RLock()
        self._subscribers: dict[type[Event], list[Callable[[Event], None]]] = defaultdict(list)

    def subscribe(
        self, event_type: type[Event], callback: Callable[[Event], None]
    ) -> Callable[[], None]:
        """注册回调并返回退订函数。"""
        with self._lock:
            self._subscribers[event_type].append(callback)

        def _unsubscribe() -> None:
            with self._lock, suppress(ValueError):
                self._subscribers[event_type].remove(callback)

        return _unsubscribe

    def publish(self, event: Event) -> None:
        """按类型层次派发事件，回调异常被隔离记录。"""
        with self._lock:
            matched: list[Callable[[Event], None]] = [
                cb
                for registered_type, callbacks in self._subscribers.items()
                if isinstance(event, registered_type)
                for cb in callbacks
            ]
        for callback in matched:
            try:
                callback(event)
            except Exception:  # 回调异常必须隔离，不能中断派发
                logger.bind(component="event_bus").exception(
                    "事件回调执行失败: event={}", type(event).__name__
                )

    def clear(self) -> None:
        """移除所有订阅（主要用于测试隔离）。"""
        with self._lock:
            self._subscribers.clear()


__all__ = ["InMemoryEventBus"]
