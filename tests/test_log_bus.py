"""可视化日志总线测试（V3.1.0）。

覆盖可视化客户端「仿真日志」面板的数据源：环形缓冲、级别/关键字过滤、
断线续传序号、订阅投递、root logger 旁路装配与摘除，以及日志出口对
异常 payload 的容错（日志链路不得反噬业务）。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from hunter_sim.visualization.logbus import LogBus, LogRecord, normalize_level


def _bus(**kwargs: Any) -> LogBus:
    return LogBus(**kwargs)


class TestNormalizeLevel:
    def test_level_names_are_case_insensitive(self) -> None:
        assert normalize_level("info") == logging.INFO
        assert normalize_level(" Warning ") == logging.WARNING
        assert normalize_level("ERROR") == logging.ERROR

    def test_aliases_and_unknown(self) -> None:
        assert normalize_level("warn") == logging.WARNING
        assert normalize_level("fatal") == logging.CRITICAL
        assert normalize_level("nonsense") == logging.DEBUG  # 未识别即不过滤

    def test_none_and_int(self) -> None:
        assert normalize_level(None) == logging.DEBUG
        assert normalize_level(30) == 30


class TestPublishAndSnapshot:
    def test_sequence_is_monotonic_and_snapshot_keeps_order(self) -> None:
        bus = _bus(max_records=10)
        first = bus.publish("INFO", "a", "one")
        second = bus.publish("WARNING", "b", "two")

        assert (first.seq, second.seq) == (1, 2)
        assert [item.message for item in bus.snapshot(limit=10, level="DEBUG")] == ["one", "two"]

    def test_capacity_keeps_only_latest(self) -> None:
        bus = _bus(max_records=2)
        for index in range(5):
            bus.publish("INFO", "a", f"msg-{index}")

        assert bus.capacity == 2
        assert bus.size == 2
        assert [item.message for item in bus.snapshot(limit=10)] == ["msg-3", "msg-4"]

    def test_non_positive_capacity_falls_back(self) -> None:
        assert _bus(max_records=0).capacity == 2000

    def test_level_filter(self) -> None:
        bus = _bus()
        bus.publish("DEBUG", "a", "trace")
        bus.publish("INFO", "a", "info")
        bus.publish("ERROR", "a", "boom")

        assert [item.level for item in bus.snapshot(limit=10, level="INFO")] == ["INFO", "ERROR"]
        assert [item.level for item in bus.snapshot(limit=10, level="ERROR")] == ["ERROR"]

    def test_contains_is_case_insensitive(self) -> None:
        bus = _bus()
        bus.publish("INFO", "a", "Instance Created")
        bus.publish("INFO", "a", "other")

        assert [item.message for item in bus.snapshot(limit=10, contains="created")] == [
            "Instance Created"
        ]

    def test_since_seq_resumes_after_disconnect(self) -> None:
        bus = _bus()
        bus.publish("INFO", "a", "one")
        cursor = bus.publish("INFO", "a", "two").seq
        bus.publish("INFO", "a", "three")

        assert [item.message for item in bus.snapshot(limit=10, since_seq=cursor)] == ["three"]

    def test_limit_caps_tail(self) -> None:
        bus = _bus()
        for index in range(5):
            bus.publish("INFO", "a", f"m{index}")

        assert [item.message for item in bus.snapshot(limit=2)] == ["m3", "m4"]
        assert bus.snapshot(limit=0) == []

    def test_counts_and_clear(self) -> None:
        bus = _bus()
        bus.publish("INFO", "a", "x")
        bus.publish("ERROR", "a", "y")
        counts = bus.counts()

        assert counts["INFO"] == 1
        assert counts["ERROR"] == 1
        assert counts["total"] == 2
        assert set(counts) == {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "total"}

        bus.clear()
        assert bus.size == 0
        assert bus.snapshot(limit=10) == []
        assert bus.counts()["total"] == 2  # 累计计数不清零（面板展示的是生命周期统计）

    def test_unknown_level_recorded_as_info(self) -> None:
        bus = _bus()
        assert bus.publish("VERBOSE", "a", "x").level == "INFO"

    def test_to_dict_rounds_timestamp(self) -> None:
        bus = _bus()
        payload = bus.publish("INFO", "a", "x", ts=1000.123456).to_dict()

        assert payload["seq"] == 1
        assert payload["ts"] == 1000.123
        assert payload["level"] == "INFO"
        assert payload["logger"] == "a"


class TestStdlibBridge:
    def test_publish_record_converts_log_record(self) -> None:
        bus = _bus()
        record = logging.LogRecord(
            name="hunter_sim.demo",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed %s",
            args=("hard",),
            exc_info=None,
        )
        converted = bus.publish_record(record)

        assert isinstance(converted, LogRecord)
        assert converted is not None
        assert converted.message == "failed hard"
        assert converted.level == "ERROR"
        assert converted.logger == "hunter_sim.demo"

    def test_broken_format_falls_back_to_raw_template(self) -> None:
        bus = _bus()
        record = logging.LogRecord(
            name="hunter_sim.demo",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="%d",
            args=("not-a-number",),
            exc_info=None,
        )

        assert bus.publish_record(record) is not None
        assert bus.snapshot(limit=1)[0].message == "%d"  # 格式化失败退回原始模板

    def test_attach_captures_logger_records(self) -> None:
        bus = _bus(max_records=5)
        name = "hunter_sim.tests.logbus.attach"
        bus.attach(name)
        try:
            assert bus.attached is True
            logging.getLogger(name).warning("hello %s", "bus")
        finally:
            bus.detach()

        assert any("hello bus" in item.message for item in bus.snapshot(limit=5))
        assert bus.attached is False

    def test_detach_uses_attached_logger_name(self) -> None:
        """构造名与 attach 名不一致时，detach 必须摘除真正挂载的那个 logger。"""
        bus = _bus(logger="hunter_sim.tests.logbus.original")
        bus.attach("hunter_sim.tests.logbus.override")
        bus.detach()

        assert logging.getLogger("hunter_sim.tests.logbus.override").handlers == []

    def test_attach_and_detach_are_idempotent(self) -> None:
        bus = _bus()
        bus.attach("hunter_sim.tests.logbus.idem")
        bus.attach("hunter_sim.tests.logbus.idem")
        bus.detach()
        bus.detach()

        assert bus.attached is False

    def test_handler_swallows_bus_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """总线内部异常不得冒泡到 logging（否则业务日志会连带抛错）。"""
        bus = _bus()
        name = "hunter_sim.tests.logbus.swallow"

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("bus down")

        bus.attach(name)
        monkeypatch.setattr(bus, "publish_record", boom)
        try:
            logging.getLogger(name).warning("still safe")  # 不得抛异常
        finally:
            bus.detach()

        assert bus.size == 0


class TestSubscription:
    def test_subscriber_receives_and_close_stops_delivery(self) -> None:
        bus = _bus()
        received: list[str] = []
        subscription = bus.subscribe(lambda record: received.append(record.message))

        bus.publish("INFO", "a", "one")
        subscription.close()
        bus.publish("INFO", "a", "two")

        assert received == ["one"]

    def test_subscriber_exception_does_not_break_publish(self) -> None:
        bus = _bus()

        def boom(record: LogRecord) -> None:
            raise RuntimeError("subscriber down")

        subscription = bus.subscribe(boom)
        try:
            entry = bus.publish("INFO", "a", "still recorded")
        finally:
            subscription.close()

        assert entry.message == "still recorded"
        assert bus.size == 1
