"""仿真控制面板（V2.2.0，PySide6）。

对标截图 "Simulation Control" + "Control Mode"：Start/Pause/Stop 切仿真运行；
Autopilot/Manual 单选切车辆控制来源；VIL/SIL 模式切换（`SimMode`）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from hunter_sim.common.models import SimMode


class ControlPanel(QGroupBox):
    """仿真运行/控制模式/在环模式控制。"""

    startRequested = Signal()
    pauseRequested = Signal()
    stopRequested = Signal()
    autopilotToggled = Signal(bool)   # True=Autopilot, False=Manual
    modeChanged = Signal(str)         # SimMode.value

    def __init__(self, parent: Optional[QGroupBox] = None) -> None:
        super().__init__("Simulation Control", parent)
        layout = QVBoxLayout(self)

        run_row = QHBoxLayout()
        self._start = QPushButton("Start")
        self._pause = QPushButton("Pause")
        self._stop = QPushButton("Stop")
        self._stop.setProperty("danger", True)
        self._start.clicked.connect(self.startRequested.emit)
        self._pause.clicked.connect(self.pauseRequested.emit)
        self._stop.clicked.connect(self.stopRequested.emit)
        for btn in (self._start, self._pause, self._stop):
            run_row.addWidget(btn)
        layout.addLayout(run_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Control Mode:"))
        self._autopilot = QRadioButton("Autopilot")
        self._manual = QRadioButton("Manual")
        self._autopilot.setChecked(True)
        self._ctrl_group = QButtonGroup(self)
        self._ctrl_group.addButton(self._autopilot)
        self._ctrl_group.addButton(self._manual)
        self._autopilot.toggled.connect(lambda on: self.autopilotToggled.emit(on))
        mode_row.addWidget(self._autopilot)
        mode_row.addWidget(self._manual)
        layout.addLayout(mode_row)

        hil_row = QHBoxLayout()
        hil_row.addWidget(QLabel("HIL Mode:"))
        self._vil = QRadioButton("VIL")
        self._sil = QRadioButton("SIL")
        self._sil.setChecked(True)
        self._hil_group = QButtonGroup(self)
        self._hil_group.addButton(self._vil)
        self._hil_group.addButton(self._sil)
        self._vil.toggled.connect(
            lambda on: self.modeChanged.emit(SimMode.VIL.value if on else SimMode.SIL.value)
        )
        hil_row.addWidget(self._vil)
        hil_row.addWidget(self._sil)
        layout.addLayout(hil_row)

        self._status = QLabel("Idle")
        layout.addWidget(self._status)

    @property
    def is_autopilot(self) -> bool:
        """当前是否 Autopilot 模式。"""
        return self._autopilot.isChecked()

    def set_status(self, text: str) -> None:
        """更新运行状态标签。"""
        self._status.setText(text)
