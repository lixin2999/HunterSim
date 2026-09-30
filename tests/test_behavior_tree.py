"""行为树单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from typing import Any

from hunter_sim.traffic_sim.actor_behaviors import ActorAction, ActorBehavior
from hunter_sim.traffic_sim.behavior_tree import (
    BehaviorLeaf,
    BehaviorTree,
    NodeStatus,
    Parallel,
    Selector,
    Sequence,
    TriggerGate,
)


class _CountBehavior(ActorBehavior):
    """运行 N 帧后完成的行为。"""

    def __init__(self, ticks_to_finish: int, action: ActorAction, behavior_id: str = "cnt") -> None:
        super().__init__(behavior_id)
        self._n = ticks_to_finish
        self._count = 0
        self._action = action
        self.started = False

    def start(self) -> None:
        super().start()
        self.started = True

    def update(self, elapsed_time: float, actor: Any, ego_vehicle: Any, delta_seconds: float) -> ActorAction:
        self._count += 1
        if self._count >= self._n:
            self._action.is_stop = True  # 保留配置字段并标记完成（SUCCESS）
        return self._action


class _StubTrigger:
    def __init__(self, fire_after: int) -> None:
        self._fire_after = fire_after
        self._calls = 0

    def check(self, elapsed_time: float, actor: Any, ego_vehicle: Any) -> bool:
        self._calls += 1
        return self._calls > self._fire_after


class _BoomTrigger:
    def check(self, elapsed_time: float, actor: Any, ego_vehicle: Any) -> bool:
        raise RuntimeError("trigger error")


def _leaf(ticks: int, speed: float = 5.0) -> BehaviorLeaf:
    return BehaviorLeaf(_CountBehavior(ticks, ActorAction(target_speed_ms=speed)))


class TestBehaviorLeaf:
    def test_start_once_and_run(self) -> None:
        b = _CountBehavior(2, ActorAction(target_speed_ms=4.0))
        leaf = BehaviorLeaf(b)
        leaf.tick(0.0, None, None, 0.02)
        assert b.started is True
        assert leaf.status == NodeStatus.RUNNING

    def test_success_on_stop(self) -> None:
        leaf = _leaf(1)
        leaf.tick(0.0, None, None, 0.02)
        assert leaf.status == NodeStatus.SUCCESS

    def test_reset(self) -> None:
        b = _CountBehavior(5, ActorAction())
        leaf = BehaviorLeaf(b)
        leaf.tick(0.0, None, None, 0.02)
        leaf.reset()
        assert leaf.status == NodeStatus.IDLE
        assert b.is_active is False


class TestSequence:
    def test_runs_children_in_order(self) -> None:
        seq = Sequence([_leaf(1), _leaf(1), _leaf(1)])
        seq.tick(0.0, None, None, 0.02)
        assert seq.status == NodeStatus.SUCCESS

    def test_stops_on_running(self) -> None:
        seq = Sequence([_leaf(1), _leaf(3)])
        seq.tick(0.0, None, None, 0.02)  # first success, second running
        assert seq.status == NodeStatus.RUNNING

    def test_reset(self) -> None:
        seq = Sequence([_leaf(1), _leaf(2)])
        seq.tick(0.0, None, None, 0.02)
        seq.reset()
        assert seq.status == NodeStatus.IDLE


class TestSelector:
    def test_returns_first_non_failure(self) -> None:
        sel = Selector([_leaf(1)])
        sel.tick(0.0, None, None, 0.02)
        assert sel.status == NodeStatus.SUCCESS

    def test_all_children_none_success_failure(self) -> None:
        # leaf 永远 RUNNING（ticks_to_finish 大）→ selector 保持 RUNNING
        sel = Selector([_leaf(10)])
        sel.tick(0.0, None, None, 0.02)
        assert sel.status == NodeStatus.RUNNING

    def test_reset(self) -> None:
        sel = Selector([_leaf(1)])
        sel.tick(0.0, None, None, 0.02)
        sel.reset()
        assert sel.status == NodeStatus.IDLE


class TestParallel:
    def test_merges_fields(self) -> None:
        p = Parallel([
            BehaviorLeaf(_CountBehavior(1, ActorAction(target_speed_ms=3.0))),
            BehaviorLeaf(_CountBehavior(1, ActorAction(brake=0.7))),
        ])
        action = p.tick(0.0, None, None, 0.02)
        assert action.target_speed_ms == 3.0
        assert action.brake == 0.7

    def test_running_when_any_running(self) -> None:
        p = Parallel([_leaf(1), _leaf(5)])
        p.tick(0.0, None, None, 0.02)
        assert p.status == NodeStatus.RUNNING

    def test_reset(self) -> None:
        p = Parallel([_leaf(1)])
        p.tick(0.0, None, None, 0.02)
        p.reset()
        assert p.status == NodeStatus.IDLE


class TestTriggerGate:
    def test_idle_until_triggered(self) -> None:
        gate = TriggerGate(_StubTrigger(fire_after=0), _leaf(1))
        # 第一次 check 返回 True -> triggered，执行子节点
        gate.tick(0.0, None, None, 0.02)
        assert gate.status in (NodeStatus.SUCCESS, NodeStatus.RUNNING)

    def test_idle_before_trigger(self) -> None:
        gate = TriggerGate(_StubTrigger(fire_after=5), _leaf(1))
        action = gate.tick(0.0, None, None, 0.02)
        assert gate.status == NodeStatus.IDLE
        assert action.is_stop is False

    def test_trigger_error_failure(self) -> None:
        gate = TriggerGate(_BoomTrigger(), _leaf(1))
        gate.tick(0.0, None, None, 0.02)
        assert gate.status == NodeStatus.FAILURE

    def test_reset(self) -> None:
        gate = TriggerGate(_StubTrigger(0), _leaf(1))
        gate.tick(0.0, None, None, 0.02)
        gate.reset()
        assert gate.status == NodeStatus.IDLE


class TestBehaviorTree:
    def test_tick_and_complete(self) -> None:
        tree = BehaviorTree(_leaf(1), name="t")
        tree.tick(0.0, None, None, 0.02)
        assert tree.is_complete is True

    def test_not_complete_while_running(self) -> None:
        tree = BehaviorTree(_leaf(5))
        tree.tick(0.0, None, None, 0.02)
        assert tree.is_complete is False

    def test_reset(self) -> None:
        tree = BehaviorTree(_leaf(1))
        tree.tick(0.0, None, None, 0.02)
        tree.reset()
        assert tree.is_complete is False
