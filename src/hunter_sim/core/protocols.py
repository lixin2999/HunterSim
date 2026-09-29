"""跨层服务接口契约（``typing.Protocol``）。

各层专属的 Protocol 定义在对应层的 ``protocols.py``；此处仅集中定义跨层通用、
与具体仿真后端无关的核心契约（当前为事件总线）。

依赖注入约定（§5.2）：上层通过构造函数接收下列 Protocol，不直接实例化实现类。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from hunter_sim.core.events import Event


@runtime_checkable
class EventBus(Protocol):
    """模块间松耦合通信的事件总线契约。"""

    def publish(self, event: Event) -> None:
        """发布一个事件，同步派发给所有订阅者。"""
        ...

    def subscribe(
        self, event_type: type[Event], callback: Callable[[Event], None]
    ) -> Callable[[], None]:
        """订阅指定类型事件。

        Args:
            event_type: 事件类型。订阅基类（如 ``Event``）将收到其所有子类型事件。
            callback: 收到事件时的回调。

        Returns:
            一个无参的退订函数，调用后移除该订阅。
        """
        ...


__all__ = ["EventBus"]
