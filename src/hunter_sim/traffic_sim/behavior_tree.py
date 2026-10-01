"""行为树模块（PROMPT-ENG-005-B）。

实现行为树（BehaviorTree）框架，支持：
- 顺序执行（Sequence）
- 并行执行（Parallel）
- 条件分支（Selector / Conditional）
- 触发条件门控（TriggerGate）

用于交通参与者的复杂行为序列编排。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from hunter_sim.common.utils import get_logger
from hunter_sim.traffic_sim.actor_behaviors import ActorAction, ActorBehavior

logger = get_logger(__name__)


class NodeStatus(str, Enum):
    """行为树节点执行状态。"""

    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"


class BehaviorNode(ABC):
    """行为树节点抽象基类。

    Args:
        name: 节点名称（调试用）。
    """

    def __init__(self, name: str = "") -> None:
        self.name = name or self.__class__.__name__
        self.status: NodeStatus = NodeStatus.IDLE

    @abstractmethod
    def tick(
        self,
        elapsed_time: float,
        actor: Any,
        ego_vehicle: Any,
        delta_seconds: float,
    ) -> ActorAction:
        """执行本节点并返回行为指令。

        Args:
            elapsed_time: 自场景开始的时间（秒）。
            actor: CARLA Actor 对象。
            ego_vehicle: 自车对象（用于距离/速度计算）。
            delta_seconds: 仿真步长（秒）。

        Returns:
            ActorAction 指令。
        """
        ...

    def reset(self) -> None:
        """重置节点状态（场景重启时调用）。"""
        self.status = NodeStatus.IDLE


class BehaviorLeaf(BehaviorNode):
    """行为叶子节点，包装一个 ActorBehavior 实例。

    Args:
        behavior: 具体行为对象。
        name: 节点名称。
    """

    def __init__(self, behavior: ActorBehavior, name: str = "") -> None:
        super().__init__(name or f"Leaf({behavior.behavior_id})")
        self._behavior = behavior
        self._started: bool = False

    def tick(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        if not self._started:
            self._behavior.start()
            self._started = True
        action = self._behavior.update(elapsed_time, actor, ego_vehicle, delta_seconds)
        self.status = NodeStatus.SUCCESS if action.is_stop else NodeStatus.RUNNING
        return action

    def reset(self) -> None:
        super().reset()
        self._behavior.stop()
        self._started = False


class Sequence(BehaviorNode):
    """顺序节点：依次执行子节点，遇到 RUNNING 停止，全部 SUCCESS 则 SUCCESS。

    Args:
        children: 子节点列表。
        name: 节点名称。
    """

    def __init__(self, children: list[BehaviorNode], name: str = "Sequence") -> None:
        super().__init__(name)
        self._children = children
        self._current: int = 0

    def tick(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        last_action = ActorAction()
        while self._current < len(self._children):
            child = self._children[self._current]
            action = child.tick(elapsed_time, actor, ego_vehicle, delta_seconds)
            last_action = action
            if child.status == NodeStatus.RUNNING:
                self.status = NodeStatus.RUNNING
                return action
            if child.status == NodeStatus.FAILURE:
                self.status = NodeStatus.FAILURE
                return action
            self._current += 1

        self.status = NodeStatus.SUCCESS if self._current >= len(self._children) else NodeStatus.RUNNING
        return last_action

    def reset(self) -> None:
        super().reset()
        self._current = 0
        for child in self._children:
            child.reset()


class Selector(BehaviorNode):
    """选择节点（条件分支）：依次尝试子节点，返回第一个非 FAILURE 结果。

    Args:
        children: 子节点列表。
        name: 节点名称。
    """

    def __init__(self, children: list[BehaviorNode], name: str = "Selector") -> None:
        super().__init__(name)
        self._children = children
        self._current: int = 0

    def tick(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        last_action = ActorAction()
        while self._current < len(self._children):
            child = self._children[self._current]
            action = child.tick(elapsed_time, actor, ego_vehicle, delta_seconds)
            last_action = action
            if child.status in (NodeStatus.RUNNING, NodeStatus.SUCCESS):
                self.status = child.status
                return action
            self._current += 1

        self.status = NodeStatus.FAILURE
        return last_action

    def reset(self) -> None:
        super().reset()
        self._current = 0
        for child in self._children:
            child.reset()


class Parallel(BehaviorNode):
    """并行节点：同时执行所有子节点，合并输出（取最后一个非默认 action）。

    常用于同时控制速度和转向的复合行为。

    Args:
        children: 子节点列表。
        name: 节点名称。
    """

    def __init__(self, children: list[BehaviorNode], name: str = "Parallel") -> None:
        super().__init__(name)
        self._children = children

    def tick(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        merged = ActorAction()
        all_done = True
        for child in self._children:
            action = child.tick(elapsed_time, actor, ego_vehicle, delta_seconds)
            # 合并：取各字段最大值
            merged.target_speed_ms = max(merged.target_speed_ms, action.target_speed_ms)
            merged.steering = merged.steering or action.steering
            merged.brake = max(merged.brake, action.brake)
            merged.throttle = max(merged.throttle, action.throttle)
            merged.is_stop = merged.is_stop and action.is_stop
            if child.status == NodeStatus.RUNNING:
                all_done = False

        self.status = NodeStatus.SUCCESS if all_done else NodeStatus.RUNNING
        return merged

    def reset(self) -> None:
        super().reset()
        for child in self._children:
            child.reset()


class TriggerGate(BehaviorNode):
    """触发门控节点：当触发条件满足前返回 IDLE，满足后执行子节点。

    Args:
        trigger: 触发条件对象（需实现 check() -> bool）。
        child: 被门控的子节点。
        name: 节点名称。
    """

    def __init__(self, trigger: Any, child: BehaviorNode, name: str = "TriggerGate") -> None:
        super().__init__(name)
        self._trigger = trigger
        self._child = child
        self._triggered: bool = False

    def tick(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        if not self._triggered:
            try:
                if self._trigger.check(elapsed_time, actor, ego_vehicle):
                    self._triggered = True
                    logger.debug(f"TriggerGate '{self.name}' activated")
                else:
                    self.status = NodeStatus.IDLE
                    return ActorAction()
            except Exception as exc:
                logger.warning(f"Trigger check error: {exc}")
                self.status = NodeStatus.FAILURE
                return ActorAction()

        action = self._child.tick(elapsed_time, actor, ego_vehicle, delta_seconds)
        self.status = self._child.status
        return action

    def reset(self) -> None:
        super().reset()
        self._triggered = False
        self._child.reset()


class BehaviorTree:
    """行为树根容器。

    管理一棵行为树的完整生命周期，提供 tick() 和 reset() 接口。

    Args:
        root: 根节点。
        name: 行为树名称。
    """

    def __init__(self, root: BehaviorNode, name: str = "BehaviorTree") -> None:
        self._root = root
        self.name = name
        self._start_time: float = 0.0
        logger.debug(f"BehaviorTree '{name}' created")

    def tick(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        """推进行为树一个仿真步，返回本帧行为指令。

        Args:
            elapsed_time: 自场景开始的经过时间（秒）。
            actor: 本行为树控制的 CARLA Actor。
            ego_vehicle: 自车 Actor。
            delta_seconds: 步长（秒）。

        Returns:
            本帧 ActorAction。
        """
        return self._root.tick(elapsed_time, actor, ego_vehicle, delta_seconds)

    def reset(self) -> None:
        """重置整棵行为树（场景重启时调用）。"""
        self._root.reset()
        logger.debug(f"BehaviorTree '{self.name}' reset")

    @property
    def is_complete(self) -> bool:
        """行为树是否已全部执行完毕。"""
        return self._root.status in (NodeStatus.SUCCESS, NodeStatus.FAILURE)
