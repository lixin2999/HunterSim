"""Actor 面板（V2.2.0，PySide6）：蓝图类型 + 位姿 + Spawn Actor + Clear Scene。

对标截图 "Add Actor" 区：Type 下拉列出车辆/行人蓝图（由 SimWorker 异步过滤蓝图库
经 `blueprintsReady` 回填），X/Y/Z 数值框，Spawn Actor 生成、Clear Scene 清空。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
)

_ACTOR_FILTERS: tuple[str, ...] = ("vehicle.*", "walker.*", "vehicle.bh.*", "static.prop.*")


class ActorPanel(QGroupBox):
    """Actor 生成与清理。"""

    spawnRequested = Signal(str, float, float, float)  # blueprint, x, y, z
    clearRequested = Signal()
    filterRequested = Signal(str)  # 请求按前缀过滤蓝图库

    def __init__(self, parent: Optional[QGroupBox] = None) -> None:
        super().__init__("Add Actor", parent)
        layout = QVBoxLayout(self)

        self._type = QComboBox()
        self._type.setEditable(True)
        self._filter = QComboBox()
        self._filter.addItems(list(_ACTOR_FILTERS))
        self._filter.currentTextChanged.connect(self.filterRequested.emit)

        self._x = QDoubleSpinBox(minimum=-10000.0, maximum=10000.0, decimals=2, value=0.0)
        self._y = QDoubleSpinBox(minimum=-10000.0, maximum=10000.0, decimals=2, value=0.0)
        self._z = QDoubleSpinBox(minimum=-100.0, maximum=1000.0, decimals=2, value=0.5)

        form = QFormLayout()
        form.addRow("Filter:", self._filter)
        form.addRow("Type:", self._type)
        form.addRow("X:", self._x)
        form.addRow("Y:", self._y)
        form.addRow("Z:", self._z)
        layout.addLayout(form)

        row = QHBoxLayout()
        self._spawn_btn = QPushButton("Spawn Actor")
        self._clear_btn = QPushButton("Clear Scene")
        self._clear_btn.setProperty("danger", True)
        self._spawn_btn.clicked.connect(self._on_spawn)
        self._clear_btn.clicked.connect(self.clearRequested.emit)
        row.addWidget(self._spawn_btn)
        row.addWidget(self._clear_btn)
        layout.addLayout(row)

    def set_blueprints(self, names: list[str]) -> None:
        """用 SimWorker 过滤结果回填 Type 下拉。"""
        current = self._type.currentText()
        self._type.blockSignals(True)
        self._type.clear()
        self._type.addItems(names)
        self._type.blockSignals(False)
        if current:
            self._type.setCurrentText(current)

    def _on_spawn(self) -> None:
        blueprint = self._type.currentText().strip()
        if not blueprint:
            return
        self.spawnRequested.emit(blueprint, self._x.value(), self._y.value(), self._z.value())
