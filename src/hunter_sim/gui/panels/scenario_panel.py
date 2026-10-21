"""场景运行面板（V2.2.0，PySide6）。

对标需求：加载 `.xosc`（OpenSCENARIO）或选内置原子场景名 → 一键启动/停止
ScenarioRunner 子进程（由 `ScenarioRunnerLauncher` 在主窗口中驱动）。面板只发信号，
不直接管理进程，便于解耦与测试。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)


class ScenarioPanel(QGroupBox):
    """ScenarioRunner 场景启停。"""

    startRequested = Signal(str, str)  # (xosc_path, scenario_name)，其一可为空串
    stopRequested = Signal()

    def __init__(
        self,
        built_in_scenarios: Optional[list[str]] = None,
        parent: Optional[QGroupBox] = None,
    ) -> None:
        super().__init__("Scenario Runner", parent)
        layout = QVBoxLayout(self)

        xosc_row = QHBoxLayout()
        self._xosc = QLineEdit()
        self._xosc.setPlaceholderText("OpenSCENARIO (.xosc) 路径")
        self._browse = QPushButton("浏览...")
        self._browse.clicked.connect(self._on_browse)
        xosc_row.addWidget(self._xosc, 1)
        xosc_row.addWidget(self._browse)
        layout.addLayout(xosc_row)

        name_row = QHBoxLayout()
        self._scenario = QLineEdit()
        self._scenario.setPlaceholderText("内置原子场景名（如 FollowLeadingVehicle）")
        name_row.addWidget(QLabel("Scenario:"))
        name_row.addWidget(self._scenario, 1)
        layout.addLayout(name_row)

        btn_row = QHBoxLayout()
        self._start = QPushButton("Start Scenario")
        self._stop = QPushButton("Stop")
        self._stop.setProperty("danger", True)
        self._start.clicked.connect(self._on_start)
        self._stop.clicked.connect(self.stopRequested.emit)
        btn_row.addWidget(self._start)
        btn_row.addWidget(self._stop)
        layout.addLayout(btn_row)

        self._status = QLabel("Idle")
        layout.addWidget(self._status)

    def set_status(self, text: str) -> None:
        """更新场景运行状态。"""
        self._status.setText(text)

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 OpenSCENARIO 文件", "", "OpenSCENARIO (*.xosc)")
        if path:
            self._xosc.setText(path)

    def _on_start(self) -> None:
        self.startRequested.emit(self._xosc.text().strip(), self._scenario.text().strip())
