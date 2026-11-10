"""CARLA 端点并发替身（V3.6.0）。

``hunter_sim.engine.carla_mailbox`` 的 owner 判定依赖 **真实线程身份**
（``threading.enumerate()`` + ``is_alive()``），因此「端点被 tick 线程占住」这类
竞态窗口只能用真线程复现，SimpleNamespace/MagicMock 替身造不出来。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from hunter_sim.engine.carla_mailbox import CarlaMailbox

_WAIT_S = 5.0


class _HeldOwner:
    """持有端点 owner 的替身线程（可选周期 drain 排队任务）。"""

    def __init__(self, mailbox: CarlaMailbox, *, name: str, drain: bool) -> None:
        self.mailbox = mailbox
        self.name = name
        self.drain = drain
        self.claim_ok = False
        self.executed = 0
        self._claimed = threading.Event()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def _run(self) -> None:
        self.claim_ok = self.mailbox.claim(self.name, timeout=2.0)
        self._claimed.set()
        if not self.claim_ok:
            return
        if self.drain:
            while not self._stop.wait(0.02):
                self.executed += self.mailbox.drain()
        else:
            self._stop.wait(30.0)
        self.executed += self.mailbox.drain(max_tasks=32)
        self.mailbox.relinquish()

    def start(self) -> "_HeldOwner":
        self.thread.start()
        assert self._claimed.wait(_WAIT_S), "owner 线程未启动"
        assert self.claim_ok, "owner 声明失败（端点应处于空闲态）"
        return self

    def join(self) -> None:
        self._stop.set()
        self.thread.join(_WAIT_S)


@contextmanager
def held_endpoint_owner(
    mailbox: CarlaMailbox, *, name: str = "HeldTick", drain: bool = False
) -> Iterator[_HeldOwner]:
    """在 with 块期间由一个真实线程独占端点（复现「上一场景仍在 tick」）。

    Args:
        mailbox: 目标端点信箱。
        name: 替身线程名（会出现在 owner_name 与日志里）。
        drain: True 表示该线程周期执行排队任务（模拟正常 tick 循环）；
            False 表示只占不还（模拟 setup 长事务/卡住的循环）。
    """
    owner = _HeldOwner(mailbox, name=name, drain=drain).start()
    try:
        yield owner
    finally:
        owner.join()


def endpoint_owner_thread_died_without_release(
    mailbox: CarlaMailbox, *, name: str = "DeadTick"
) -> Optional[int]:
    """让一个线程声明 owner 后直接退出（tick 线程被异常打断的现场）。

    Returns:
        已死线程的 ident（用于断言 owner 声明尚未归还）。
    """
    result: dict[str, Optional[int]] = {"ident": None}

    def _claim_and_die() -> None:
        mailbox.claim(name, timeout=2.0)
        result["ident"] = threading.get_ident()

    thread = threading.Thread(target=_claim_and_die, name=name, daemon=True)
    thread.start()
    thread.join(_WAIT_S)
    assert thread.ident == result["ident"], "owner 线程未按预期退出"
    return thread.ident
