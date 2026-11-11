"""CARLA 客户端单线程信箱（V3.6.0）。

LibCarla 的 Python ``Client`` / ``World`` / ``Actor`` **都不是线程安全的**：两个线程
同时对同一个连接发请求（RPC）会让原生侧崩溃，表现为进程**无 traceback** 直接退出
（``0xC00000FD`` 栈溢出 / ``0xC0000409`` fail-fast / ``0xC0000005`` 访问违例）。
V3.5.x 逐个消除已定位的并发路径，属于「事后自愈」；本模块把收敛做成**结构性约束**：

1. 每个 CARLA 端点（``host:rpc_port``）有且只有一个 **owner 线程**（即场景 tick 线程）；
2. owner 线程内的调用**内联执行**（不排队、不加额外延迟，保持 50Hz）；
3. 非 owner 线程的调用**只能投递给 owner 串行执行**，并带超时等待结果；
4. 拿不到结果时**降级返回缓存/默认值**，绝不在错误线程上碰 CARLA 对象；
5. 绕过信箱直接发 RPC 时 ``require_owner()`` 会输出**可定位的告警**（线程名 + 操作名），
   严格模式（``CARLA_MAILBOX_STRICT=true``）下直接抛 :class:`CarlaThreadViolation`，
   把「进程无声消失」换成「一条 Python 异常 + 栈」。

与 :mod:`hunter_sim.execution.plane` 的引擎锁关系：引擎锁串行化「装配/释放」这类
长事务，信箱串行化**每一次 RPC**；``tick`` 线程只参与信箱，因此信箱同时补齐了
引擎锁覆盖不到运行循环的缺口。加锁顺序固定为 **引擎锁 → 信箱**，反向不得嵌套。
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Optional

from hunter_sim.common.metrics import CARLA_MAILBOX_TASKS_TOTAL, CARLA_OFFOWNER_RPC_TOTAL
from hunter_sim.common.utils import get_logger

logger = get_logger(__name__)

#: 非 owner 线程等待 owner 执行结果的默认超时（秒）：读状态类调用宁可降级读数
_DEFAULT_WAIT_TIMEOUT_S = 2.0

#: 未声明 owner 时，内联执行等待端点空闲的默认超时（秒）
_DEFAULT_INLINE_TIMEOUT_S = 2.0

#: owner 每轮 drain 最多执行的任务数（避免尾部请求拖慢 tick）
_DRAIN_BATCH = 8

#: 违例告警限频窗口（秒）：50Hz 循环里同一路径反复违例会形成日志风暴
_WARN_INTERVAL_S = 30.0

#: 严格模式开关（默认关闭：生产降级，测试/排查时抛异常）
_STRICT_ENV = "CARLA_MAILBOX_STRICT"


class CarlaMailboxTimeout(RuntimeError):
    """CARLA 调用未能在超时内由 owner 线程执行（调用方应降级，不得重试硬撞）。"""


class CarlaThreadViolation(RuntimeError):
    """在非 owner 线程上直接访问 CARLA 对象（严格模式下的硬拦截）。"""


def _thread_alive(ident: Optional[int]) -> bool:
    """线程标识是否对应一个仍存活的线程（``None`` 视为不存在）。"""
    if ident is None:
        return False
    for thread in threading.enumerate():
        if thread.ident == ident:
            return thread.is_alive()
    return False


class _EndpointLock:
    """按线程身份计数的可重入端点锁（同一时刻只允许一个线程对端点发 RPC）。

    为什么不直接用 :class:`threading.RLock`：RLock 由**已退出线程**持有时永远不会
    释放。tick 线程如果在 ``claim`` 之后被异常打断（没走到 ``relinquish``），端点
    就会永久不可用——所有调用只拿 fallback、装配一直超时。本锁在 ``acquire`` 时
    发现持有线程已死就地接管，把「永久降级」换成「一次接管 + 一条告警」。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._holder: Optional[int] = None
        self._holder_name = ""
        self._depth = 0

    @property
    def holder_name(self) -> str:
        with self._cond:
            return self._holder_name

    def acquire(self, timeout: float) -> bool:
        """获取锁；同线程重入直接计数。超时或持有线程存活且未释放时返回 False。"""
        ident = threading.get_ident()
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while True:
                if self._holder is None or self._holder == ident:
                    self._holder = ident
                    self._holder_name = threading.current_thread().name
                    self._depth += 1
                    return True
                if not _thread_alive(self._holder):
                    # 持有者线程已死：强制接管（tick 线程异常退出的唯一自愈路径）
                    logger.warning(
                        "CarlaMailbox endpoint lock held by dead thread '%s'; "
                        "thread '%s' takes it over",
                        self._holder_name,
                        threading.current_thread().name,
                    )
                    self._holder = None
                    self._holder_name = ""
                    self._depth = 0
                    continue
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return False
                self._cond.wait(min(0.2, remain))

    def release(self) -> None:
        """释放一层；非持有线程调用抛 ``RuntimeError``（与 RLock 语义一致）。"""
        ident = threading.get_ident()
        with self._cond:
            if self._holder != ident:
                raise RuntimeError("endpoint lock released by a non-holder thread")
            self._depth -= 1
            if self._depth <= 0:
                self._depth = 0
                self._holder = None
                self._holder_name = ""
            self._cond.notify_all()


@dataclass
class _Task:
    """一次待 owner 执行的调用。"""

    call: Callable[[], Any]
    name: str
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: Optional[BaseException] = None
    abandoned: bool = False
    queued_at: float = field(default_factory=time.monotonic)


class CarlaMailbox:
    """单端点 CARLA 调用信箱。

    Args:
        key: 端点标识（``host:rpc_port``），仅用于日志与指标标签。
        wait_timeout_s: 非 owner 线程等待结果的默认超时（秒）。
        inline_timeout_s: 未声明 owner 时，内联执行等锁的默认超时（秒）。
    """

    def __init__(
        self,
        key: str,
        wait_timeout_s: float = _DEFAULT_WAIT_TIMEOUT_S,
        inline_timeout_s: float = _DEFAULT_INLINE_TIMEOUT_S,
    ) -> None:
        self.key = key
        self._wait_timeout_s = max(0.05, float(wait_timeout_s))
        self._inline_timeout_s = max(0.05, float(inline_timeout_s))
        # 所有内联执行共用的端点锁：保证「同一时刻只有一个线程在发 RPC」
        self._gate = _EndpointLock()
        self._cond = threading.Condition()
        self._queue: Deque[_Task] = deque()
        self._owner_ident: Optional[int] = None
        self._owner_name: str = ""
        self._owner_depth = 0
        self._warn_at: dict[str, float] = {}
        self._warn_count: dict[str, int] = {}
        self._stats: dict[str, int] = {
            "inline": 0,
            "queued": 0,
            "executed": 0,
            "timeout": 0,
            "dropped": 0,
        }

    # ── owner 声明 ──────────────────────────────────────────────────────────

    def claim(self, thread_name: str = "", timeout: float = 0.0) -> bool:
        """把当前线程声明为该端点的唯一 owner（tick 线程进入循环时调用）。

        owner **在持有期间一直占住端点锁**，因此其他线程的 ``run`` 只能排队或降级，
        而装配/释放这类长事务在 owner 未归还前会等锁超时并报错——这恰好是需要的：
        上一个场景的 tick 线程还在跑时，绝不允许对同一个 world 发 ``load_world``。

        已被其他**存活**线程占用时最多等 ``timeout`` 秒；占用线程已死（解释器退出
        等极端情况）则不抢占端点锁，而是等锁超时后返回 False，由调用方显式失败。

        Returns:
            True 表示本线程已成为 owner 并持有端点锁。
        """
        ident = threading.get_ident()
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while True:
                if self._owner_ident == ident:
                    self._owner_depth += 1
                    self._gate.acquire(self._inline_timeout_s)
                    return True
                holder = self._owner_ident
                if holder is None or not _thread_alive(holder):
                    if holder is not None:
                        logger.warning(
                            "CarlaMailbox[%s] owner '%s' is no longer alive; "
                            "waiting for its endpoint lock before a new owner can claim",
                            self.key,
                            self._owner_name or holder,
                        )
                    break
                if time.monotonic() >= deadline:
                    return False
                self._cond.wait(min(0.2, max(0.01, deadline - time.monotonic())))
            remain = deadline - time.monotonic() if timeout > 0 else self._inline_timeout_s
        if not self._gate.acquire(max(0.05, remain)):
            return False
        with self._cond:
            self._owner_ident = ident
            self._owner_name = thread_name or threading.current_thread().name
            self._owner_depth = 1
            logger.info(f"CarlaMailbox[{self.key}] owner claimed: {self._owner_name}")
        return True

    def relinquish(self) -> None:
        """owner 线程归还所有权 + 端点锁（tick 线程退出时调用，与 ``claim`` 成对）。"""
        dropped = 0
        with self._cond:
            if self._owner_ident != threading.get_ident():
                return  # 非 owner 调用：幂等忽略
            self._owner_depth -= 1
            if self._owner_depth > 0:
                self._gate.release()
                return
            self._owner_ident = None
            self._owner_name = ""
            dropped = len(self._queue)
            self._queue.clear()
            self._cond.notify_all()
        try:
            self._gate.release()
        except RuntimeError:  # noqa: PERF203  配对异常只能说明调用点写错，留痕迹
            logger.warning(f"CarlaMailbox[{self.key}] owner release found no held lock")
        self._stats["dropped"] += dropped
        if dropped:
            logger.warning(
                f"CarlaMailbox[{self.key}] owner released with {dropped} queued task(s) "
                "dropped (callers got fallback values)"
            )
        else:
            logger.info(f"CarlaMailbox[{self.key}] owner released")

    @property
    def owner_name(self) -> str:
        """当前 owner 线程名（未声明时为空串）。"""
        with self._cond:
            return self._owner_name

    @property
    def is_claimed(self) -> bool:
        """是否已有 owner（含 owner 已死但未归还的情况）。"""
        with self._cond:
            return self._owner_ident is not None

    @property
    def is_owner(self) -> bool:
        """当前线程是否 owner。"""
        with self._cond:
            return self._owner_ident == threading.get_ident()

    @property
    def pending(self) -> int:
        """排队中的调用数（owner 每轮 drain 消费）。"""
        with self._cond:
            return len(self._queue)

    @property
    def stats(self) -> dict[str, int]:
        """执行分布快照（排障用：queued/timeout 非零说明仍有跨线程调用）。"""
        with self._cond:
            return dict(self._stats)

    # ── 调用派发 ────────────────────────────────────────────────────────────

    def run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "",
        timeout: Optional[float] = None,
        fallback: Any = None,
    ) -> Any:
        """串行执行一次 CARLA 调用，拿不到执行机会时返回 ``fallback``。

        owner 线程（或未声明 owner 且抢到端点锁）→ **内联执行**；其他线程 → 入队由
        owner 执行并等待结果，超时后返回 ``fallback``（不重试、不在本线程执行）。

        Args:
            fn: 待执行可调用对象（内部才允许触碰 CARLA 对象）。
            *args: 传给 ``fn`` 的位置参数。
            name: 操作名（日志与指标标签，如 ``world.tick``/``scene_load``）。
            timeout: 等待上限（秒）；None 用默认值。
            fallback: 未能执行时的返回值。

        Returns:
            ``fn()`` 的结果，或 ``fallback``。
        """
        op = name or getattr(fn, "__name__", "carla_call")
        if self._can_run_inline():
            return self._inline(fn, args, op, timeout, fallback=fallback)
        return self._queued(fn, args, op, timeout, fallback)

    def run_required(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "",
        timeout: Optional[float] = None,
        allow_queue: bool = True,
    ) -> Any:
        """必须执行的调用（装配/收尾）：未获得执行机会时抛 :class:`CarlaMailboxTimeout`。

        ``fn`` 自身抛出的异常按原样向调用方传播（调用方决定如何降级）。

        Args:
            allow_queue: 默认 True（非 owner 线程排队等 owner 代执行）。**装配类长
                事务必须传 False**：``load_world``/场景 setup 绝不能被排进另一个
                场景的 tick 循环里执行——那会让两个场景的生命周期互相交错。传
                False 时只允许内联抢锁，抢不到就明确超时失败。
        """
        op = name or getattr(fn, "__name__", "carla_call")
        if self._can_run_inline() or not allow_queue:
            return self._inline(fn, args, op, timeout, raise_timeout=True)
        sentinel = object()
        result = self._queued(fn, args, op, timeout, fallback=sentinel)
        if result is sentinel:
            raise CarlaMailboxTimeout(
                f"CARLA call '{op}' was not executed on engine '{self.key}': "
                f"owner '{self.owner_name}' did not drain within the timeout"
            )
        return result

    def submit(self, fn: Callable[..., Any], *args: Any, name: str = "") -> bool:
        """只投递不等待（收尾类幂等操作）。

        Returns:
            True 表示已交给 owner（或本线程已内联执行完）；False 表示端点无
            存活 owner 且未抢到端点锁，调用被丢弃。
        """
        op = name or getattr(fn, "__name__", "carla_call")
        if self._can_run_inline():
            try:
                self._inline(fn, args, op, None, raise_timeout=True)
            except CarlaMailboxTimeout as exc:
                # 锁被他人长事务持有（如上一个场景收尾）：不伪装成已投递
                self._warn_once(op, str(exc))
                return False
            except Exception as exc:  # noqa: BLE001  投递语义：执行失败只记日志
                logger.warning(f"CarlaMailbox[{self.key}] inline submit '{op}' failed: {exc}")
            return True
        with self._cond:
            if not _thread_alive(self._owner_ident):
                # owner 声明未归还且线程已死：排队无人消费，明确丢弃（调用方降级）
                self._stats["dropped"] += 1
                CARLA_MAILBOX_TASKS_TOTAL.labels(
                    engine=self.key, op=op, result="dropped"
                ).inc()
                return False
            self._queue.append(_Task(call=lambda: fn(*args), name=op))
            self._stats["queued"] += 1
            self._cond.notify_all()
        CARLA_MAILBOX_TASKS_TOTAL.labels(engine=self.key, op=op, result="queued").inc()
        return True

    def drain(self, max_tasks: int = _DRAIN_BATCH) -> int:
        """owner 线程执行排队中的调用（每 tick 调用一次，返回执行条数）。

        等待方已超时的任务直接跳过（结果无人接收，执行它只会白白占用 tick 时间）。
        """
        with self._cond:
            if self._owner_ident != threading.get_ident():
                return 0
            batch: list[_Task] = []
            while self._queue and len(batch) < max(1, int(max_tasks)):
                batch.append(self._queue.popleft())
        executed = 0
        for task in batch:
            if task.abandoned:
                self._stats["dropped"] += 1
                CARLA_MAILBOX_TASKS_TOTAL.labels(
                    engine=self.key, op=task.name, result="dropped"
                ).inc()
                continue
            try:
                task.value = task.call()
            except BaseException as exc:  # noqa: BLE001  异常回传给等待方
                task.error = exc
            finally:
                executed += 1
                task.done.set()
            self._stats["executed"] += 1
            CARLA_MAILBOX_TASKS_TOTAL.labels(engine=self.key, op=task.name, result="ok").inc()
        return executed

    # ── 硬拦截 ──────────────────────────────────────────────────────────────

    def can_access_directly(self, op: str) -> bool:
        """当前线程能否**直接**碰 CARLA 对象（owner / 无 owner / owner 已死）。

        与 :meth:`require_owner` 的区别：本方法给「只读降级类」调用点用，返回
        False 时调用方**必须自己退到缓存/空值**，而不是继续发 RPC。因为告警
        不能阻止崩溃：LibCarla 不会因为日志里写了线程名就变成线程安全。

        Returns:
            True 表示可以内联访问；False 表示已由本方法记录违例（告警/指标，
            严格模式下抛 :class:`CarlaThreadViolation`），调用方应降级。
        """
        if self._can_run_inline():
            return True
        CARLA_OFFOWNER_RPC_TOTAL.labels(engine=self.key, op=op).inc()
        if _strict_mode_enabled():
            raise CarlaThreadViolation(self._violation_text(op))
        self._warn_once(op, self._violation_text(op) + "; caller must fall back to cache")
        return False

    def direct_call(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "",
        timeout: Optional[float] = None,
        fallback: Any = None,
    ) -> Any:
        """安全执行一次「只读兜底」类 CARLA 调用：读缓存降级 + 端点锁串行。

        与 :meth:`can_access_directly` 的区别——后者只做「能不能碰」的布尔判定，
        调用方随后仍在**不持锁**的情况下发 RPC。一旦端点已无 owner（tick 线程收尾
        后 ``relinquish`` 释放所有权、HTTP/executor 线程仍在轮询），多个线程会并发
        操作同一 Client，触发 0xC0000409 原生 fail-fast。本方法在真正发 RPC 前获取
        端点锁：owner 线程重入计数、无 owner 线程互斥串行，从而彻底封死该窗口。

        Args:
            fn: 真正触碰 CARLA 对象的可调用对象（通常是读缓存兜底前的 RPC）。
            *args: 传给 ``fn`` 的位置参数。
            name: 操作名（日志/指标标签）。
            timeout: 等锁上限（秒）；None 用默认内联超时。
            fallback: 未获授权（有存活他线程 owner）或未抢到端点锁时的返回值。

        Returns:
            ``fn()`` 的结果，或 ``fallback``。
        """
        op = name or getattr(fn, "__name__", "carla_direct")
        if not self._can_run_inline():
            # 有存活的他人 owner：读操作必须降级到缓存，不能撞正在 tick 的连接
            CARLA_OFFOWNER_RPC_TOTAL.labels(engine=self.key, op=op).inc()
            if _strict_mode_enabled():
                raise CarlaThreadViolation(self._violation_text(op))
            self._warn_once(op, self._violation_text(op) + "; caller must fall back to cache")
            return fallback
        limit = self._inline_timeout_s if timeout is None else max(0.05, float(timeout))
        if not self._gate.acquire(limit):
            # 无 owner 但端点被另一线程（如释放事务 apply_settings）占住：
            # 宁可降级读数，也不能与之并发发 RPC
            self._stats["timeout"] += 1
            CARLA_MAILBOX_TASKS_TOTAL.labels(engine=self.key, op=op, result="timeout").inc()
            self._warn_once(
                op,
                f"CarlaMailbox[{self.key}] direct read '{op}' skipped: endpoint busy "
                f"(held by thread '{self._gate.holder_name}'); returning fallback",
            )
            return fallback
        try:
            result = fn(*args)
        finally:
            self._gate.release()
        self._stats["inline"] += 1
        CARLA_MAILBOX_TASKS_TOTAL.labels(engine=self.key, op=op, result="ok").inc()
        return result

    def require_owner(self, op: str) -> None:
        """断言「当前线程有权直接访问 CARLA 对象」（供未走信箱的历史调用点自检）。

        未声明 owner 时任何线程都可访问（单线程测试/CLI 路径保持原行为）；已声明
        owner 且调用线程不是 owner 时按严格模式抛错或限频告警。**有缓存可退的调用点
        请改用** :meth:`can_access_directly`，本方法适用于「只能由 owner 经信箱进入」
        的内部实体（setup / teardown / apply_settings）。
        """
        with self._cond:
            owner = self._owner_ident
            ident = threading.get_ident()
        if owner is None or owner == ident:
            return
        CARLA_OFFOWNER_RPC_TOTAL.labels(engine=self.key, op=op).inc()
        strict = _strict_mode_enabled()
        if not self._should_warn(op):
            if strict:
                raise CarlaThreadViolation(self._violation_text(op))
            return
        logger.warning(self._violation_text(op) + ("" if strict else " (suppressed by design)"))
        if strict:
            raise CarlaThreadViolation(self._violation_text(op))

    # ── 内部实现 ────────────────────────────────────────────────────────────

    def _can_run_inline(self) -> bool:
        """本线程能否内联执行：无 owner / 本线程是 owner / owner 线程已死（接管）。

        owner 线程已死但没来得及 ``relinquish()``（tick 线程被异常打断）时，若不容忍
        接管，端点会永久降级为「所有调用只返回缓存」；这里把失效声明就地清掉，
        并让接管线程打一条 WARNING，留下可追查的现场痕迹。
        """
        ident = threading.get_ident()
        with self._cond:
            owner = self._owner_ident
            if owner is None or owner == ident:
                return True
            if _thread_alive(owner):
                return False
            stale_name = self._owner_name
            self._owner_ident = None
            self._owner_name = ""
            self._owner_depth = 0
            self._cond.notify_all()
        logger.warning(
            "CarlaMailbox[%s] stale owner '%s' (dead thread) cleared; thread '%s' takes "
            "over CARLA access inline",
            self.key,
            stale_name or owner,
            threading.current_thread().name,
        )
        return True

    def _inline(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        op: str,
        timeout: Optional[float],
        raise_timeout: bool = False,
        fallback: Any = None,
    ) -> Any:
        limit = self._inline_timeout_s if timeout is None else max(0.05, float(timeout))
        if not self._gate.acquire(limit):
            self._stats["timeout"] += 1
            CARLA_MAILBOX_TASKS_TOTAL.labels(engine=self.key, op=op, result="timeout").inc()
            text = (
                f"CARLA engine '{self.key}' is busy (call '{op}' not started within "
                f"{limit:.1f}s; endpoint held by thread '{self._gate.holder_name}')"
            )
            if raise_timeout:
                raise CarlaMailboxTimeout(text)
            logger.warning(f"{text}; returning fallback")
            # 必须回落到调用方的哨兵值而不是 None：调用点（scene_stop /
            # restore_world_async）靠 fallback 区分「没执行」与「执行了但返回空」
            return fallback
        try:
            result = fn(*args)
        finally:
            self._gate.release()
        self._stats["inline"] += 1
        CARLA_MAILBOX_TASKS_TOTAL.labels(engine=self.key, op=op, result="ok").inc()
        return result

    def _queued(
        self,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        op: str,
        timeout: Optional[float],
        fallback: Any,
    ) -> Any:
        limit = self._wait_timeout_s if timeout is None else max(0.05, float(timeout))
        task = _Task(call=lambda: fn(*args), name=op)
        with self._cond:
            if not _thread_alive(self._owner_ident):
                # owner 已死/未启动：排队无人消费，直接降级
                self._stats["timeout"] += 1
                CARLA_MAILBOX_TASKS_TOTAL.labels(engine=self.key, op=op, result="dropped").inc()
                self._warn_once(
                    op,
                    f"CarlaMailbox[{self.key}] call '{op}' dropped: no live owner thread "
                    f"(current thread '{threading.current_thread().name}')",
                )
                return fallback
            self._queue.append(task)
            self._stats["queued"] += 1
            self._cond.notify_all()
        if not task.done.wait(timeout=limit):
            task.abandoned = True
            self._stats["timeout"] += 1
            CARLA_MAILBOX_TASKS_TOTAL.labels(engine=self.key, op=op, result="timeout").inc()
            self._warn_once(
                op,
                f"CarlaMailbox[{self.key}] call '{op}' timed out after {limit:.1f}s waiting "
                f"for owner '{self.owner_name}' (returning fallback value)",
            )
            return fallback
        if task.error is not None:
            raise task.error
        CARLA_MAILBOX_TASKS_TOTAL.labels(engine=self.key, op=op, result="ok").inc()
        return task.value

    def _violation_text(self, op: str) -> str:
        return (
            f"CarlaMailbox[{self.key}] off-owner CARLA access: thread "
            f"'{threading.current_thread().name}' called '{op}' while owner is "
            f"'{self.owner_name}' (LibCarla is not thread-safe; route this call "
            "through mailbox.run())"
        )

    def _should_warn(self, op: str) -> bool:
        now = time.monotonic()
        if now - self._warn_at.get(op, 0.0) < _WARN_INTERVAL_S:
            self._warn_count[op] = self._warn_count.get(op, 0) + 1
            return False
        self._warn_at[op] = now
        self._warn_count[op] = 0
        return True

    def _warn_once(self, op: str, text: str) -> None:
        if self._should_warn(op):
            suppressed = self._warn_count.get(op, 0)
            suffix = f" (+{suppressed} suppressed)" if suppressed else ""
            logger.warning(f"{text}{suffix}")


# ── 端点级注册表 ────────────────────────────────────────────────────────────────

_MAILBOXES: dict[str, CarlaMailbox] = {}
_REGISTRY_LOCK = threading.Lock()


def get_mailbox(key: str = "default") -> CarlaMailbox:
    """取端点对应的信箱（懒创建，进程内唯一）。"""
    resolved = str(key or "default")
    with _REGISTRY_LOCK:
        mailbox = _MAILBOXES.get(resolved)
        if mailbox is None:
            mailbox = CarlaMailbox(resolved)
            _MAILBOXES[resolved] = mailbox
        return mailbox


def reset_mailboxes() -> None:
    """清空注册表（仅测试使用：避免用例间残留 owner）。"""
    with _REGISTRY_LOCK:
        _MAILBOXES.clear()


def _strict_mode_enabled() -> bool:
    """严格模式：违例直接抛异常（``CARLA_MAILBOX_STRICT=1/true/yes``）。"""
    return str(os.environ.get(_STRICT_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "CarlaMailbox",
    "CarlaMailboxTimeout",
    "CarlaThreadViolation",
    "get_mailbox",
    "reset_mailboxes",
]
