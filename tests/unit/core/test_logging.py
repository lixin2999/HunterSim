"""core.logging 单元测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hunter_sim.core.logging import configure_logging, logger, run_context


def _capture_sink() -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []

    def _sink(message: Any) -> None:
        records.append(message.record["extra"])

    handler_id = logger.add(_sink, level="DEBUG")
    return records, handler_id


def test_configure_logging_returns_none() -> None:
    assert configure_logging(level="DEBUG") is None


def test_run_context_binds_run_id_and_frame_id() -> None:
    configure_logging(level="DEBUG")
    records, handler_id = _capture_sink()
    try:
        with run_context(run_id="run-42", frame_id=7):
            logger.info("hello")
    finally:
        logger.remove(handler_id)

    assert records[-1]["run_id"] == "run-42"
    assert records[-1]["frame_id"] == "7"


def test_file_sink_created(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(level="INFO", log_dir=log_dir)
    logger.info("written to file")
    logger.remove()
    files = list(log_dir.glob("huntersim_*.log"))
    assert files, "应在 log_dir 生成日志文件"
    assert "written to file" in files[0].read_text(encoding="utf-8")
