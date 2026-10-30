"""进程内日志总线（可视化客户端「仿真日志」面板的数据源）。

设计口径：

- **只读旁路**：总线通过 ``logging.Handler`` 旁听日志，不改变任何输出行为；
  处理器内部吞掉自身异常（``except Exception``），日志出口不得反噬业务；
- **有界环形缓冲**：只保留最近 ``max_records`` 条，长时间运行不会无界增长；
- **历史 + 增量**：``snapshot()`` 给页面首屏历史，``subscribe()`` 给增量推送，
  两条路径共用同一份 ``LogRecord`` 结构；
- **挂到 root logger**：既覆盖 ``hunter_sim.*`` 业务日志，也覆盖 uvicorn 等
  第三方日志（子 logger 的记录会向父级传播，故只挂一次即可，不会重复）。

总线实例由控制面应用在 lifespan 内创建并 ``attach()``，请求结束时 ``detach()``，
因此测试中的每个 ``TestClient`` 上下文互不干扰。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

# 级别名 → 数值（避免使用 logging 私有表）
_LEVEL_RANK: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_LEVEL_ALIASES: dict[str, str] = {
    "WARN": "WARNING",
    "FATAL": "CRITICAL",
}


def normalize_level(level: str | int | None) -> int:
    """把级别名/数值归一化为 ``logging`` 整型级别。

    Args:
        level: 级别名（大小写不敏感）、整型级别或 None。

    Returns:
        整型级别；无法识别时返回 ``logging.DEBUG``（即不过滤）。
    """
    if level is None:
        return logging.DEBUG
    if isinstance(level, int):
        return level
    text = level.strip().upper()
    text = _LEVEL_ALIASES.get(text, text)
    return _LEVEL_RANK.get(text, logging.DEBUG)


@dataclass(frozen=True)
class LogRecord:
    """单条日志快照（不可变）。

    Attributes:
        seq: 全局单调递增序号（客户端据此去重/续传）。
        ts: 产生时间（Unix 秒）。
        level: 级别名（DEBUG/INFO/WARNING/ERROR/CRITICAL）。
        logger: logger 名称。
        message: 已渲染的消息正文。
        thread: 线程名（排障用）。
    """

    seq: int
    ts: float
    level: str
    logger: str
    message: str
    thread: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好的字典。"""
        return {
            "seq": self.seq,
            "ts": round(self.ts, 3),
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
            "thread": self.thread,
        }

    def passes(self, min_level: int, contains: str = "") -> bool:
        """是否满足级别与关键字过滤条件。"""
        if _LEVEL_RANK.get(self.level, logging.NOTSET) < min_level:
            return False
        if contains and contains.lower() not in self.message.lower():
            return False
        return True


class Subscription:
    """增量订阅句柄（``close()`` 幂等）。"""

    def __init__(self, bus: "LogBus", sink: Callable[[LogRecord], None]) -> None:
        self._bus = bus
        self._sink = sink
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        """取消订阅（重复调用安全）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._bus._unsubscribe(self)

    def deliver(self, record: LogRecord) -> None:
        """投递一条记录（总线内部调用，异常不外抛）。"""
        with self._lock:
            if self._closed:
                return
        try:
            self._sink(record)
        except Exception:  # noqa: BLE001  订阅方异常不得影响日志输出
            return


class _BusHandler(logging.Handler):
    """把 ``LogRecord`` 旁路写入总线的处理器。"""

    def __init__(self, bus: "LogBus") -> None:
        super().__init__(level=logging.DEBUG)
        self._bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        """转换为总线记录（自身异常静默，避免日志风暴）。"""
        try:
            self._bus.publish_record(record)
        except Exception:  # noqa: BLE001  日志出口不得反噬业务
            return


class LogBus:
    """线程安全的日志环形缓冲 + 增量分发。

    Args:
        max_records: 环形缓冲容量（条）；``<=0`` 时按 2000 处理。
        logger: 挂载的 logger 名称（默认 ``root``，覆盖全量日志）。
    """

    def __init__(self, max_records: int = 2000, logger: str = "") -> None:
        capacity = int(max_records) if int(max_records) > 0 else 2000
        self._records: deque[LogRecord] = deque(maxlen=capacity)
        self._capacity = capacity
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: list[Subscription] = []
        self._handler: Optional[_BusHandler] = None
        self._logger_name = logger
        self._attached_name = logger
        self._counts: dict[str, int] = {name: 0 for name in _LEVEL_RANK}

    # ── 装配 ────────────────────────────────────────────────────────────────

    def attach(self, logger: str | None = None) -> None:
        """挂载到 logger（默认构造时的 logger，``""`` 表示 root）；幂等。"""
        if self._handler is not None:
            return
        name = logger if logger is not None else self._logger_name
        handler = _BusHandler(self)
        logging.getLogger(name).addHandler(handler)
        self._handler = handler
        self._attached_name = name

    def detach(self) -> None:
        """摘除处理器（幂等）。"""
        handler, self._handler = self._handler, None
        if handler is None:
            return
        try:
            logging.getLogger(self._attached_name).removeHandler(handler)
        except Exception:  # noqa: BLE001  退出路径不得因日志装配失败而报错
            return
        handler.close()

    @property
    def attached(self) -> bool:
        """是否已挂载到 logger。"""
        return self._handler is not None

    @property
    def capacity(self) -> int:
        """环形缓冲容量（条）。"""
        return self._capacity

    # ── 写入 ────────────────────────────────────────────────────────────────

    def publish_record(self, record: logging.LogRecord) -> Optional[LogRecord]:
        """把 stdlib ``LogRecord`` 转成总线记录并广播。"""
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001  格式化失败时退回原始模板
            message = str(record.msg)
        return self.publish(
            level=record.levelname,
            logger=record.name,
            message=message,
            thread=record.threadName or "",
            ts=record.created,
        )

    def publish(
        self,
        level: str,
        logger: str,
        message: str,
        thread: str = "",
        ts: float | None = None,
    ) -> LogRecord:
        """写入一条记录并分发给订阅者。

        Args:
            level: 级别名（未知级别按 INFO 记录）。
            logger: logger 名称。
            message: 消息正文。
            thread: 线程名。
            ts: 产生时间（None 取当前时间）。

        Returns:
            入队的不可变记录。
        """
        name = (level or "INFO").strip().upper()
        if name not in _LEVEL_RANK:
            name = "INFO"
        with self._lock:
            self._seq += 1
            entry = LogRecord(
                seq=self._seq,
                ts=float(ts if ts is not None else time.time()),
                level=name,
                logger=logger,
                message=message,
                thread=thread,
            )
            self._records.append(entry)
            self._counts[name] = self._counts.get(name, 0) + 1
            subscribers = list(self._subscribers)
        for subscription in subscribers:
            subscription.deliver(entry)
        return entry

    # ── 读取 ────────────────────────────────────────────────────────────────

    def snapshot(
        self,
        limit: int = 200,
        level: str | int | None = None,
        contains: str = "",
        since_seq: int = 0,
    ) -> list[LogRecord]:
        """取最近若干条记录（旧→新）。

        Args:
            limit: 最多返回条数（``<=0`` 返回空列表）。
            level: 最低级别（名称或整型）。
            contains: 关键字过滤（大小写不敏感，空表示不过滤）。
            since_seq: 只返回序号大于该值的记录（用于断线续传）。

        Returns:
            过滤后的记录列表。
        """
        if limit <= 0:
            return []
        min_level = normalize_level(level)
        with self._lock:
            items = list(self._records)
        filtered = [
            item
            for item in items
            if item.seq > since_seq and item.passes(min_level, contains)
        ]
        return filtered[-limit:]

    def counts(self) -> dict[str, int]:
        """各级别累计条数（含 ``total``）。"""
        with self._lock:
            snapshot = dict(self._counts)
        snapshot["total"] = sum(snapshot.values())
        return snapshot

    @property
    def size(self) -> int:
        """当前缓冲条数。"""
        with self._lock:
            return len(self._records)

    def clear(self) -> None:
        """清空缓冲（订阅关系保留）。"""
        with self._lock:
            self._records.clear()

    # ── 订阅 ────────────────────────────────────────────────────────────────

    def subscribe(self, sink: Callable[[LogRecord], None]) -> Subscription:
        """注册增量订阅（调用方负责 ``close()``）。"""
        subscription = Subscription(self, sink)
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def _unsubscribe(self, subscription: Subscription) -> None:
        """移除订阅（幂等）。"""
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)


__all__ = ["LogBus", "LogRecord", "Subscription", "normalize_level"]
