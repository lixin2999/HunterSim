"""CARLA 实时画面视图（V2.2.0，PySide6）。

对标截图右侧 "CARLA LIVE STREAM" 区：顶部 SOURCE 下拉切换要显示的相机传感器，
无信号时显示 "Waiting for Signal..."。按 sensor_id 缓存最新帧，切换即时生效。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class StreamView(QWidget):
    """相机画面显示组件。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._frames: dict[int, QImage] = {}
        self._current: Optional[int] = None

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("CARLA LIVE STREAM")
        title.setProperty("section", "title")
        self._source_label = QLabel("SOURCE:")
        self._source_combo = QComboBox()
        self._source_combo.setMinimumWidth(160)
        self._source_combo.currentTextChanged.connect(self._on_source_changed)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._source_label)
        header.addWidget(self._source_combo)
        layout.addLayout(header)

        self._canvas = QLabel("Waiting for Signal...")
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.setMinimumSize(640, 360)
        self._canvas.setStyleSheet("background:#0b0d10;color:#6b7280;font-family:Consolas,monospace;")
        layout.addWidget(self._canvas, 1)

    def on_frame(self, sensor_id: int, image: QImage) -> None:
        """接收一帧（由 SimWorker.frameReady 转接）。"""
        self._frames[sensor_id] = image
        if self._current is None:
            self._current = sensor_id
        if sensor_id == self._current:
            self._render(image)

    def set_sources(self, sensor_ids: list[int]) -> None:
        """刷新 SOURCE 下拉（新增/移除相机传感器时）。"""
        current_text = self._source_combo.currentText()
        self._source_combo.blockSignals(True)
        self._source_combo.clear()
        for sid in sensor_ids:
            self._source_combo.addItem(str(sid))
        self._source_combo.blockSignals(False)
        # 尽量保持原选择
        if current_text and self._source_combo.findText(current_text) >= 0:
            self._source_combo.setCurrentText(current_text)
        if sensor_ids:
            self._current = int(self._source_combo.currentText())
        else:
            self._current = None
            self._frames.clear()
            self._canvas.setText("Waiting for Signal...")

    def _on_source_changed(self, text: str) -> None:
        if not text:
            return
        self._current = int(text)
        frame = self._frames.get(self._current)
        if frame is not None:
            self._render(frame)

    def _render(self, image: QImage) -> None:
        scaled = image.scaled(
            self._canvas.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self._canvas.setPixmap(QPixmap.fromImage(scaled))
