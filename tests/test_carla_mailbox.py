"""CARLA 单线程信箱回归测试（V3.6.0）。

覆盖「原生崩溃彻底解决」的结构性约束：

1. owner 线程内联执行（不增加 tick 延迟）；
2. 非 owner 线程只能排队由 owner 代执行，超时降级返回 fallback；
3. 装配类长事务（``load_world`` / 场景 setup）在端点被占用时必须**等锁失败**而不是
   被排进别人的 tick 循环；
4. ``require_owner`` 的违例可见性（告警 / 严格模式抛错）；
5. tick 线程异常退出（未 relinquish）后的失效接管，端点不得永久降级。
"""

from __future__ import annotations

import threading
import time
from typing import Iterator, List, Optional

import pytest

from hunter_sim.engine.carla_mailbox import (
    _Task,  # noqa: SLF001  用于直接构造排队态，验证 drain 的身份校验
    CarlaMailbox,
    CarlaMailboxTimeout,
    CarlaThreadViolation,
    get_mailbox,
    reset_mailboxes,
)

_SENTINEL = object()
_WAIT_S = 5.0


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_mailboxes()
    yield
    reset_mailboxes()


class _OwnerThread:
    """模拟场景 tick 线程：声明 owner，可选周期 drain 排队任务。"""

    def __init__(self, mailbox: CarlaMailbox, *, drain: bool = False) -> None:
        self.mailbox = mailbox
        self.drain = drain
        self.thread = threading.Thread(target=self._run, name="FakeTickThread")
        self.claimed = threading.Event()
        self.done = threading.Event()
        self._stop = threading.Event()
        self._hold: Optional[threading.Event] = None
        self.executed = 0
        self.claim_ok = False

    def start(self, hold: Optional[threading.Event] = None) -> "_OwnerThread":
        """启动并等待 owner 声明完成；``hold`` 非空时阻塞在 hold 上（不 drain）。"""
        self._hold = hold
        self.thread.start()
        assert self.claimed.wait(_WAIT_S)
        return self

    def _run(self) -> None:
        self.claim_ok = self.mailbox.claim(self.thread.name, timeout=2.0)
        self.claimed.set()
        if not self.claim_ok:
            return
        if self._hold is not None:
            self._hold.wait(_WAIT_S)
        elif self.drain:
            while not self._stop.wait(0.02):
                self.executed += self.mailbox.drain()
        else:
            self._stop.wait(_WAIT_S)
        self.executed += self.mailbox.drain(max_tasks=32)
        self.mailbox.relinquish()
        self.done.set()

    def stop(self) -> None:
        self._stop.set()
        if self._hold is not None:
            self._hold.set()
        assert self.thread.join(_WAIT_S) is None or not self.thread.is_alive()


# ── 内联 / 排队 ────────────────────────────────────────────────────────────────


class TestInlineAndQueued:
    def test_run_without_owner_executes_inline(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")

        result = mailbox.run(lambda: threading.current_thread().name, name="read")

        assert result == threading.current_thread().name  # 未换线程
        assert mailbox.stats["inline"] == 1
        assert mailbox.pending == 0

    def test_owner_claim_holds_endpoint_lock(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        release = threading.Event()
        owner = _OwnerThread(mailbox).start(hold=release)
        assert owner.claim_ok and mailbox.is_claimed

        # owner 持锁期间，任何其他线程都拿不到端点锁
        assert mailbox._gate.acquire(0.1) is False  # noqa: SLF001
        release.set()
        owner.stop()

    def test_non_owner_call_executed_by_owner_thread(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        owner = _OwnerThread(mailbox, drain=True).start()
        threads: List[str] = []

        def _record() -> str:
            name = threading.current_thread().name
            threads.append(name)
            return name

        try:
            result = mailbox.run(_record, name="ego_state", timeout=2.0)
        finally:
            owner.stop()

        assert result == "FakeTickThread"  # 由 owner 代执行
        assert mailbox.stats["queued"] >= 1
        assert mailbox.stats["executed"] >= 1

    def test_non_owner_falls_back_when_owner_busy(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        release = threading.Event()
        owner = _OwnerThread(mailbox).start(hold=release)
        executed_calls: List[str] = []

        try:
            result = mailbox.run(
                lambda: executed_calls.append("x"),
                name="read",
                timeout=0.1,
                fallback=_SENTINEL,
            )
            assert result is _SENTINEL
            assert not executed_calls
            assert mailbox.stats["timeout"] == 1
        finally:
            release.set()
            owner.stop()

        # owner 退出时把已 abandoned 的任务直接丢弃，不再无谓执行
        assert not executed_calls
        assert mailbox.stats["dropped"] >= 1

    def test_run_required_raises_when_owner_busy(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        release = threading.Event()
        owner = _OwnerThread(mailbox).start(hold=release)
        try:
            with pytest.raises(CarlaMailboxTimeout, match="did not drain"):
                mailbox.run_required(lambda: None, name="scene_stop", timeout=0.1)
        finally:
            release.set()
            owner.stop()

    def test_owner_exception_propagates_to_waiter(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        owner = _OwnerThread(mailbox, drain=True).start()

        def _boom() -> None:
            raise ValueError("carla rpc failed")

        try:
            with pytest.raises(ValueError, match="carla rpc failed"):
                mailbox.run(_boom, name="read", timeout=2.0, fallback=_SENTINEL)
        finally:
            owner.stop()


# ── 装配长事务：不得排进别人的循环 ─────────────────────────────────────────────


class TestAssemblyExclusion:
    def test_allow_queue_false_refuses_to_queue_on_live_owner(self) -> None:
        """load_world 语义：端点被 tick 线程占住时等锁超时失败，而不是入队。"""
        mailbox = CarlaMailbox("127.0.0.1:2000")
        release = threading.Event()
        owner = _OwnerThread(mailbox, drain=True).start(hold=release)
        executed: List[str] = []

        try:
            with pytest.raises(CarlaMailboxTimeout, match="is busy"):
                mailbox.run_required(
                    lambda: executed.append("load_world"),
                    name="load_world",
                    timeout=0.2,
                    allow_queue=False,
                )
        finally:
            release.set()
            owner.stop()

        assert not executed  # 既没内联，也没被 owner drain 执行
        assert mailbox.pending == 0

    def test_gate_contention_without_owner_returns_fallback(self) -> None:
        """无 owner 但端点被他人长事务占用：必须回落 fallback（而不是 None）。

        这是 ``stop()``/``_restore_world_async`` 用 ``_NOT_EXECUTED`` 哨兵判定
        「到底有没有执行」的前提；返回 None 会让收尾被误判为已完成。
        """
        mailbox = CarlaMailbox("127.0.0.1:2000")
        inside = threading.Event()
        release = threading.Event()

        def _long_transaction() -> str:
            inside.set()
            assert release.wait(_WAIT_S)
            return "done"

        holder = threading.Thread(
            target=lambda: mailbox.run(_long_transaction, name="load_world"),
            name="Assembler",
        )
        holder.start()
        assert inside.wait(_WAIT_S)
        try:
            assert mailbox.run(
                lambda: "executed", name="scene_stop", timeout=0.1, fallback=_SENTINEL
            ) is _SENTINEL
            # 投递语义同样不能谎报：锁被占时 submit 返回 False
            assert mailbox.submit(lambda: None, name="scene_stop") is False
        finally:
            release.set()
            holder.join(_WAIT_S)

    def test_inline_lock_timeout_reports_holder_thread(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        inside = threading.Event()
        release = threading.Event()

        def _long_transaction() -> None:
            inside.set()
            release.wait(_WAIT_S)

        holder = threading.Thread(
            target=lambda: mailbox.run(_long_transaction, name="load_world"),
            name="Assembler",
        )
        holder.start()
        assert inside.wait(_WAIT_S)
        try:
            with pytest.raises(CarlaMailboxTimeout, match="held by thread 'Assembler'"):
                mailbox.run_required(lambda: None, name="load_world", timeout=0.1)
        finally:
            release.set()
            holder.join(_WAIT_S)


# ── submit / drain ────────────────────────────────────────────────────────────


class TestSubmitAndDrain:
    def test_submit_queues_and_owner_drains(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        owner = _OwnerThread(mailbox, drain=True).start()
        executed: List[str] = []

        try:
            assert mailbox.submit(
                lambda: executed.append(threading.current_thread().name),
                name="scene_stop",
            )
            deadline = time.monotonic() + _WAIT_S
            while not executed and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            owner.stop()

        assert executed == ["FakeTickThread"]

    def test_submit_runs_inline_for_owner(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        executed: List[str] = []

        assert mailbox.claim(threading.current_thread().name, timeout=0.0)
        try:
            assert mailbox.submit(
                lambda: executed.append(threading.current_thread().name), name="stop"
            )
        finally:
            mailbox.relinquish()

        assert executed == [threading.current_thread().name]

    def test_drain_from_non_owner_is_noop(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        executed: List[str] = []
        mailbox._queue.append(_Task(call=lambda: executed.append("x"), name="probe"))  # noqa: SLF001

        assert mailbox.drain() == 0
        assert not executed


# ── 失效接管：tick 线程异常退出 ────────────────────────────────────────────────


class TestStaleOwnerRecovery:
    def test_dead_owner_relinquish_is_idempotent(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")

        def _claim_only() -> None:
            mailbox.claim("DeadTick", timeout=1.0)  # 声明后直接退出，不归还

        thread = threading.Thread(target=_claim_only, name="DeadTick")
        thread.start()
        thread.join(_WAIT_S)
        assert mailbox.is_claimed

        assert mailbox.relinquish() is None  # 非 owner 调用：幂等忽略
        assert mailbox.is_claimed

    def test_endpoint_recovers_after_owner_thread_dies(self, caplog: pytest.LogCaptureFixture) -> None:
        """owner 线程死亡未归还 → 后续调用接管端点锁，不得永久降级。"""
        mailbox = CarlaMailbox("127.0.0.1:2000")

        def _claim_only() -> None:
            mailbox.claim("DeadTick", timeout=1.0)

        thread = threading.Thread(target=_claim_only, name="DeadTick")
        thread.start()
        thread.join(_WAIT_S)

        with caplog.at_level("WARNING"):
            assert mailbox.run(lambda: "ok", name="ego_state", timeout=1.0) == "ok"

        assert "dead thread" in caplog.text

    def test_new_owner_claim_after_stale_takeover(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")

        def _claim_only() -> None:
            mailbox.claim("DeadTick", timeout=1.0)

        thread = threading.Thread(target=_claim_only, name="DeadTick")
        thread.start()
        thread.join(_WAIT_S)

        assert mailbox.claim("NewTick", timeout=0.5) is True
        assert mailbox.owner_name == "NewTick"
        mailbox.relinquish()
        assert not mailbox.is_claimed


# ── require_owner 硬拦截 ──────────────────────────────────────────────────────


class TestRequireOwner:
    def test_allows_any_thread_when_unclaimed(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        mailbox.require_owner("world.tick")  # 单线程 CLI/测试路径保持原行为

    def test_owner_thread_passes(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        assert mailbox.claim("Tick", timeout=0.0)
        try:
            mailbox.require_owner("world.tick")
        finally:
            mailbox.relinquish()

    def test_off_owner_call_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        release = threading.Event()
        owner = _OwnerThread(mailbox).start(hold=release)
        try:
            with caplog.at_level("WARNING"):
                mailbox.require_owner("world.get_actors")
        finally:
            release.set()
            owner.stop()

        assert "off-owner CARLA access" in caplog.text
        assert "MainThread" in caplog.text

    def test_warning_is_rate_limited(self, caplog: pytest.LogCaptureFixture) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        release = threading.Event()
        owner = _OwnerThread(mailbox).start(hold=release)
        try:
            with caplog.at_level("WARNING"):
                for _ in range(50):
                    mailbox.require_owner("world.get_actors")
            assert caplog.text.count("off-owner CARLA access") == 1
            mailbox._warn_at.clear()  # noqa: SLF001  解除限频后应再次输出
            mailbox.require_owner("world.get_actors")
            assert caplog.text.count("off-owner CARLA access") == 2
        finally:
            release.set()
            owner.stop()

    def test_strict_mode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        release = threading.Event()
        owner = _OwnerThread(mailbox).start(hold=release)
        monkeypatch.setenv("CARLA_MAILBOX_STRICT", "1")
        try:
            with pytest.raises(CarlaThreadViolation, match="off-owner CARLA access"):
                mailbox.require_owner("world.get_actors")
        finally:
            release.set()
            owner.stop()


# ── 注册表 ────────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_same_key_returns_same_instance(self) -> None:
        assert get_mailbox("127.0.0.1:2000") is get_mailbox("127.0.0.1:2000")

    def test_different_keys_are_isolated(self) -> None:
        first = get_mailbox("127.0.0.1:2000")
        second = get_mailbox("127.0.0.1:2001")
        assert first is not second
        assert first.claim("Tick", timeout=0.0)
        try:
            assert second.claim("Tick2", timeout=0.0)
        finally:
            second.relinquish()
            first.relinquish()

    def test_empty_key_falls_back_to_default(self) -> None:
        assert get_mailbox("").key == "default"

    def test_stats_counters_are_not_double_counted(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        owner = _OwnerThread(mailbox, drain=True).start()
        try:
            mailbox.run(lambda: None, name="ego_state", timeout=2.0)
            deadline = time.monotonic() + _WAIT_S
            while mailbox.stats["executed"] < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            owner.stop()

        stats = mailbox.stats
        assert stats["queued"] == 1
        assert stats["executed"] == 1
        assert stats["inline"] == 0  # 排队路径不计入内联


# ── direct_call：无 owner 直读串行（V3.8.1 teardown 并发窗口回归）────────────────


class TestDirectCall:
    def test_direct_call_without_owner_executes_inline(self) -> None:
        mailbox = CarlaMailbox("127.0.0.1:2000")
        assert mailbox.direct_call(lambda: 42, name="read") == 42
        assert mailbox.stats["inline"] == 1

    def test_direct_call_degrades_when_alive_owner_holds_endpoint(self) -> None:
        # 有存活的他人 owner：只读兜底必须降级，不能撞正在 tick 的连接
        mailbox = CarlaMailbox("127.0.0.1:2000")
        release = threading.Event()
        owner = _OwnerThread(mailbox).start(hold=release)
        try:
            called = {"hit": False}

            def _fn() -> str:
                called["hit"] = True
                return "rpc"

            assert mailbox.direct_call(_fn, name="ego_state", fallback="cache") == "cache"
            assert called["hit"] is False
        finally:
            release.set()
            owner.stop()

    def test_direct_call_serializes_concurrent_readers_after_owner_release(self) -> None:
        # V3.8.1 现场：tick 线程 relinquish 后无 owner，多线程并发直读曾裸发 RPC。
        # direct_call 必须经端点锁串行，任何时刻最多一个线程在执行 RPC。
        mailbox = CarlaMailbox("127.0.0.1:2000")
        inflight = {"now": 0, "max": 0}
        guard = threading.Lock()

        def _rpc() -> None:
            with guard:
                inflight["now"] += 1
                inflight["max"] = max(inflight["max"], inflight["now"])
            time.sleep(0.02)
            with guard:
                inflight["now"] -= 1

        threads = [
            threading.Thread(target=mailbox.direct_call, args=(_rpc,), kwargs={"name": "read"})
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(_WAIT_S)

        assert inflight["max"] == 1  # 严格串行，绝不并发发 RPC

    def test_direct_call_owner_thread_reentrant_no_deadlock(self) -> None:
        # owner 线程内嵌再取端点锁（_get_ego_state 里再走 _actor_from_world）不锁死
        mailbox = CarlaMailbox("127.0.0.1:2000")
        assert mailbox.claim(threading.current_thread().name, timeout=1.0)
        try:
            def _outer() -> int:
                return mailbox.direct_call(lambda: 1, name="inner", fallback=-1)

            assert mailbox.direct_call(_outer, name="outer", fallback=-1) == 1
        finally:
            mailbox.relinquish()
