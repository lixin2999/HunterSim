"""RingBuffer 和工具函数单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

import io
import logging
import re
import threading
from typing import Any, Iterator

import pytest

from hunter_sim.common.utils import (
    _THIRD_PARTY_LOGGERS,  # noqa: PLC2701  第三方日志接管行为需直接验证
    ACCESS_LOG_POLLING_ENV,
    RingBuffer,
    _AccessLogNoiseFilter,  # noqa: PLC2701  日志降噪行为需直接验证
    _install_access_log_policy,  # noqa: PLC2701
    _install_third_party_logging,  # noqa: PLC2701
    _make_text_formatter,  # noqa: PLC2701
    _RequestIdFilter,  # noqa: PLC2701  文本格式依赖它注入 request_id 字段
    normalize_angle_rad,
    smoothstep,
)


class TestRingBuffer:
    """RingBuffer 功能测试。"""

    def test_push_and_latest(self, ring_buffer: RingBuffer[int]) -> None:
        """测试基本 push 和 latest 操作。"""
        ring_buffer.push(1)
        ring_buffer.push(2)
        ring_buffer.push(3)
        assert ring_buffer.latest() == 3

    def test_overflow_keeps_latest(self) -> None:
        """超出容量时只保留最新 N 帧。"""
        buf: RingBuffer[int] = RingBuffer(max_size=3)
        for i in range(10):
            buf.push(i)
        frames = buf.get_range(5)
        assert len(frames) <= 3
        assert frames[-1] == 9

    def test_empty_latest_returns_none(self) -> None:
        """空缓冲区 latest() 返回 None。"""
        buf: RingBuffer[int] = RingBuffer(max_size=5)
        assert buf.latest() is None

    def test_thread_safety(self) -> None:
        """多线程并发写入时不崩溃。"""
        buf: RingBuffer[int] = RingBuffer(max_size=100)
        errors: list[Exception] = []

        def writer(start: int) -> None:
            try:
                for i in range(100):
                    buf.push(start + i)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i * 1000,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert not errors
        assert buf.latest() is not None

    def test_len(self) -> None:
        buf: RingBuffer[int] = RingBuffer(max_size=5)
        assert buf.size == 0
        buf.push(42)
        assert buf.size == 1


class TestNormalizeAngleRad:
    """角度归一化测试。"""

    @pytest.mark.parametrize(
        ("input_angle", "expected"),
        [
            (0.0, 0.0),
            (3.1416, pytest.approx(-3.14159, abs=0.01)),  # 略大於 pi -> 接近 -pi
            (-3.14159, pytest.approx(-3.14159, abs=0.01)),
            (2 * 3.14159, pytest.approx(0.0, abs=0.01)),
            (4.0, pytest.approx(4.0 - 2 * 3.14159, abs=0.01)),
        ],
    )
    def test_normalize(self, input_angle: float, expected: float) -> None:
        result = normalize_angle_rad(input_angle)
        assert result == expected
        assert -3.1416 <= result <= 3.1416


class TestSmoothstep:
    """Hermite 平滑插值测试。"""

    def test_boundaries(self) -> None:
        assert smoothstep(0.0) == pytest.approx(0.0, abs=1e-6)
        assert smoothstep(1.0) == pytest.approx(1.0, abs=1e-6)

    def test_midpoint(self) -> None:
        result = smoothstep(0.5)
        assert 0.4 < result < 0.6  # 约 0.5

    def test_monotonic(self) -> None:
        prev = -1.0
        for t in [0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0]:
            val = smoothstep(t)
            assert val >= prev
            prev = val


def _access_record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    """构造一条 uvicorn 访问日志形态的 LogRecord（V3.6.0 降噪验证用）。

    同时走一次 ``_RequestIdFilter``，与真实 handler 链上一致（文本格式有
    %(request_id)s 字段，不过滤直接 format 会 KeyError）。
    """
    record = logging.LogRecord(
        name="uvicorn.access",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    _RequestIdFilter().filter(record)
    return record


class TestLogFormatterCarriesThreadName:
    """V3.6.0：文本日志必须带线程名——原生崩溃排查要先判定「这条出自哪个线程」。"""

    def test_thread_name_present_in_output(self) -> None:
        formatter = _make_text_formatter()
        seen: list[str] = []

        def _emit() -> None:
            record = _access_record("tick from worker", level=logging.WARNING)
            seen.append(formatter.format(record))

        thread = threading.Thread(target=_emit, name="SceneRunner-scene-1")
        thread.start()
        thread.join(5.0)

        assert "[SceneRunner-scene-1]" in seen[0]

    def test_main_thread_output_also_tagged(self) -> None:
        formatted = _make_text_formatter().format(_access_record("probe"))

        assert "[MainThread]" in formatted


class TestAccessLogNoiseFilter:
    """V3.6.0：成功的面板轮询降为计数，错误一律保留。"""

    def test_suppresses_successful_polling_get(self) -> None:
        filt = _AccessLogNoiseFilter()

        assert not filt.filter(
            _access_record(
                '127.0.0.1:5000 - "GET /viz/api/v1/instances/inst-1/actors HTTP/1.1" 200 OK'
            )
        )
        assert not filt.filter(
            _access_record('127.0.0.1:5000 - "GET /health?full=1 HTTP/1.1" 204 No Content')
        )
        assert filt._suppressed == 2  # noqa: SLF001

    def test_keeps_errors_and_non_get(self) -> None:
        filt = _AccessLogNoiseFilter()

        assert filt.filter(
            _access_record(
                '127.0.0.1:5000 - "GET /viz/api/v1/instances/inst-1/actors HTTP/1.1" 503 '
                "Service Unavailable"
            )
        )
        assert filt.filter(
            _access_record('127.0.0.1:5000 - "POST /api/v1/scenes/load HTTP/1.1" 200 OK')
        )
        assert filt.filter(
            _access_record('127.0.0.1:5000 - "GET /api/v1/download/bag.tgz HTTP/1.1" 200 OK')
        )
        assert filt._suppressed == 0  # noqa: SLF001

    def test_keeps_warning_level_records(self) -> None:
        filt = _AccessLogNoiseFilter()
        record = _access_record(
            '127.0.0.1:5000 - "GET /viz/state HTTP/1.1" 200 OK', level=logging.WARNING
        )

        assert filt.filter(record)

    def test_non_matching_message_is_kept(self) -> None:
        filt = _AccessLogNoiseFilter()

        assert filt.filter(_access_record("Application startup complete"))

    def test_env_override_disables_noise_reduction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ACCESS_LOG_POLLING_ENV, "1")
        filt = _AccessLogNoiseFilter()

        assert filt.filter(
            _access_record('127.0.0.1:5000 - "GET /metrics HTTP/1.1" 200 OK')
        )
        assert filt._suppressed == 0  # noqa: SLF001

    def test_periodic_summary_reports_suppressed_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        filt = _AccessLogNoiseFilter(summary_interval_s=1.0)
        filt._next_summary_at = 0.0  # noqa: SLF001  立即触发汇总
        record = _access_record('127.0.0.1:5000 - "GET /metrics HTTP/1.1" 200 OK')

        with caplog.at_level(logging.INFO, logger="uvicorn.access"):
            assert not filt.filter(record)

        assert "抑制 1 条面板轮询请求" in caplog.text
        assert filt._suppressed == 0  # noqa: SLF001  汇总后归零

    def test_policy_install_is_idempotent_and_skips_debug(self) -> None:
        access = logging.getLogger("uvicorn.access")
        before = list(access.filters)
        try:
            _install_access_log_policy(logging.INFO)
            _install_access_log_policy(logging.INFO)
            installed = [f for f in access.filters if isinstance(f, _AccessLogNoiseFilter)]
            assert len(installed) == 1

            _install_access_log_policy(logging.DEBUG)
            assert len([f for f in access.filters if isinstance(f, _AccessLogNoiseFilter)]) == 1
        finally:
            access.filters[:] = before

    def test_policy_skipped_entirely_at_debug_level(self) -> None:
        access = logging.getLogger("uvicorn.access")
        before = list(access.filters)
        try:
            access.filters.clear()
            _install_access_log_policy(logging.DEBUG)

            assert not any(isinstance(f, _AccessLogNoiseFilter) for f in access.filters)
        finally:
            access.filters[:] = before


# ── uvicorn 原生日志接管 ──────────────────────────────────────────────────────


def _snapshot(name: str) -> tuple[list[logging.Handler], bool, int]:
    """记录 logger 当前出口/传播/级别，供用例后恢复（日志是进程级状态）。"""
    logger = logging.getLogger(name)
    return list(logger.handlers), logger.propagate, logger.level


def _restore(saved: dict[str, tuple[list[logging.Handler], bool, int]]) -> None:
    for name, (handlers, propagate, level) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.propagate = propagate
        logger.setLevel(level)


class TestUvicornLoggingRouting:
    """V3.6.0：uvicorn 自有格式（``INFO:     ...``）必须接进统一出口。

    现场日志里 uvicorn 行没有时间戳/线程名，既不能按时间轴对齐崩溃瞬间，
    也不受访问日志降噪约束——因为它们根本没走我们的 handler/formatter。
    """

    @pytest.fixture
    def sink(self) -> Iterator[dict[str, Any]]:
        """隔离全局日志状态，并把 ``hunter_sim`` 父 logger 接到内存 sink。"""
        names = ("hunter_sim", *_THIRD_PARTY_LOGGERS)
        saved = {name: _snapshot(name) for name in names}
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(_make_text_formatter())
        handler.addFilter(_RequestIdFilter())
        parent = logging.getLogger("hunter_sim")
        parent.handlers[:] = [handler]
        parent.propagate = False
        try:
            yield {"stream": stream, "parent": parent}
        finally:
            _restore(saved)

    def test_third_party_lines_carry_timestamp_and_thread(self, sink: dict[str, Any]) -> None:
        _install_third_party_logging(logging.INFO)

        logging.getLogger("uvicorn.error").info("Application startup complete")
        logging.getLogger("uvicorn.access").info(
            '127.0.0.1:5000 - "GET /viz/api/status HTTP/1.1" 200 OK'
        )

        text = sink["stream"].getvalue()
        assert "Application startup complete" in text
        assert '"GET /viz/api/status HTTP/1.1" 200 OK' in text
        assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", text), text
        assert "[MainThread]" in text
        assert "[uvicorn.error]" in text

    def test_only_the_root_of_the_tree_holds_handlers(self, sink: dict[str, Any]) -> None:  # noqa: ARG002
        """``uvicorn`` 为汇聚点（propagate=False），子 logger 靠传播去重。"""
        _install_third_party_logging(logging.INFO)

        uvicorn = logging.getLogger("uvicorn")
        assert uvicorn.propagate is False
        assert len(uvicorn.handlers) == 1
        for name in ("uvicorn.error", "uvicorn.access"):
            child = logging.getLogger(name)
            assert child.handlers == []
            assert child.propagate is True

    def test_install_is_idempotent(self, sink: dict[str, Any]) -> None:  # noqa: ARG002
        _install_third_party_logging(logging.INFO)
        first = list(logging.getLogger("uvicorn").handlers)

        _install_third_party_logging(logging.INFO)
        _install_third_party_logging(logging.INFO)

        assert logging.getLogger("uvicorn").handlers == first
        assert len(first) == 1

    def test_existing_uvicorn_dict_config_handlers_are_dropped(
        self, sink: dict[str, Any]
    ) -> None:
        """旧 dictConfig 残留的 handler 必须摘除，否则同一行输出两次。"""
        stray_buffer = io.StringIO()
        stray = logging.StreamHandler(stray_buffer)
        logging.getLogger("uvicorn.access").addHandler(stray)

        _install_third_party_logging(logging.INFO)
        logging.getLogger("uvicorn.access").info(
            '127.0.0.1:5000 - "GET /health HTTP/1.1" 200 OK'
        )

        assert stray not in logging.getLogger("uvicorn.access").handlers
        assert stray_buffer.getvalue() == ""
        assert "GET /health" in sink["stream"].getvalue()
