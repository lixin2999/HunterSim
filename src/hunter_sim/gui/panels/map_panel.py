"""地图面板（V2.2.0，PySide6）：内置地图选择 + Load Map + Import .xodr。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from hunter_sim.engine.map_manager import BUILTIN_MAPS


class MapPanel(QGroupBox):
    """地图选择与导入。"""

    loadRequested = Signal(str)     # 内置地图名
    importRequested = Signal(str)   # .xodr 路径

    def __init__(self, default_map: str = "Town03", parent: Optional[QGroupBox] = None) -> None:
        super().__init__("Map Selection", parent)
        layout = QVBoxLayout(self)

        self._combo = QComboBox()
        self._combo.addSortedItems(sorted(BUILTIN_MAPS))
        idx = self._combo.findText(default_map)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)

        row = QHBoxLayout()
        self._load_btn = QPushButton("Load Map")
        self._import_btn = QPushButton("Import .xodr")
        self._load_btn.clicked.connect(lambda: self.loadRequested.emit(self._combo.currentText()))
        self._import_btn.clicked.connect(self._on_import)
        row.addWidget(self._load_btn)
        row.addWidget(self._import_btn)

        self._current = QLabel(f"Current: {default_map}")
        layout.addWidget(self._current)
        layout.addWidget(self._combo)
        layout.addLayout(row)

    def set_current(self, map_name: str) -> None:
        """回填当前地图标签。"""
        self._current.setText(f"Current: {map_name}")

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入 OpenDRIVE 地图", "", "OpenDRIVE (*.xodr)")
        if path:
            self.importRequested.emit(path)
