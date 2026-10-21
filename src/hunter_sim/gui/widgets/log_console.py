"""带时间戳的只读日志窗（V2.2.0，PySide6）。"""

from __future__ import annotations

import time

from PySide6.QtWidgets import QPlainTextEdit


class LogConsole(QPlainTextEdit):
    """只读日志控制台，自动加 `[HH:MM:SS]` 前缀并限制最大行数。"""

    def __init__(self, max_blocks: int = 2000, parent: object = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(max_blocks)

    def append_line(self, text: str) -> None:
        """追加一行日志（带时间戳）。"""
        stamp = time.strftime("%H:%M:%S", time.localtime())
        self.appendPlainText(f"[{stamp}] {text}")
