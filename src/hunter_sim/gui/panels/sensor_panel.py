"""传感器面板（V2.2.0，PySide6）：Add Sensor。

对标截图 "Add Sensor" 区：Type（相机/其他传感器蓝图）、Attach Type（SpringArmGhost/
Rigid）、Preset（预置安装位）、Resolution、Parent（已生成 actor id）、Rel X/Y/Z、
Pitch、FOV、Attach Sensor。选项与预置来自 `sim_presets`（无 Qt、可单测）。
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
    QSpinBox,
    QVBoxLayout,
)

from hunter_sim.gui.sim_presets import (
    ALL_SENSOR_BLUEPRINTS,
    ATTACH_TYPES,
    PRESET_NAMES,
    RESOLUTIONS,
)


class SensorPanel(QGroupBox):
    """传感器挂载参数与操作。"""

    attachRequested = Signal(
        str,  # bp_name
        str,  # preset
        int,  # width
        int,  # height
        float,  # fov
        int,  # parent_id
        float,  # rel_x
        float,  # rel_y
        float,  # rel_z
        float,  # pitch
    )

    def __init__(self, parent: Optional[QGroupBox] = None) -> None:
        super().__init__("Add Sensor", parent)
        layout = QVBoxLayout(self)

        self._type = QComboBox()
        for label in ALL_SENSOR_BLUEPRINTS:
            self._type.addItem(label)
        self._attach = QComboBox()
        self._attach.addItems(list(ATTACH_TYPES))
        self._preset = QComboBox()
        self._preset.addItems(["Custom", *PRESET_NAMES])
        self._resolution = QComboBox()
        self._resolution.addItems(list(RESOLUTIONS))

        self._parent = QSpinBox(minimum=0, maximum=1000000, value=0)
        self._rel_x = QDoubleSpinBox(minimum=-100.0, maximum=100.0, decimals=2, value=0.0)
        self._rel_y = QDoubleSpinBox(minimum=-100.0, maximum=100.0, decimals=2, value=0.0)
        self._rel_z = QDoubleSpinBox(minimum=-100.0, maximum=100.0, decimals=2, value=1.6)
        self._pitch = QDoubleSpinBox(minimum=-90.0, maximum=90.0, decimals=1, value=0.0)
        self._fov = QDoubleSpinBox(minimum=10.0, maximum=170.0, decimals=1, value=90.0)

        # 选非 Custom 预置时锁定相对位姿（由 SimWorker 端应用预置，这里仅作视觉提示）
        self._preset.currentTextChanged.connect(self._on_preset_changed)

        form = QFormLayout()
        form.addRow("Type:", self._type)
        form.addRow("Attach Type:", self._attach)
        form.addRow("Preset:", self._preset)
        form.addRow("Resolution:", self._resolution)
        form.addRow("Parent:", self._parent)
        form.addRow("Rel X:", self._rel_x)
        form.addRow("Rel Y:", self._rel_y)
        form.addRow("Rel Z:", self._rel_z)
        form.addRow("Pitch:", self._pitch)
        form.addRow("FOV:", self._fov)
        layout.addLayout(form)

        row = QHBoxLayout()
        self._attach_btn = QPushButton("Attach Sensor")
        self._attach_btn.clicked.connect(self._on_attach)
        row.addWidget(self._attach_btn)
        layout.addLayout(row)

    def set_parents(self, actor_ids: list[int]) -> None:
        """刷新 Parent 下拉可选的 actor id（保留当前输入）。"""
        self._parent.setMaximum(max(1000000, max(actor_ids, default=0)))

    def _on_preset_changed(self, text: str) -> None:
        custom = text == "Custom"
        for w in (self._rel_x, self._rel_y, self._rel_z, self._pitch):
            w.setEnabled(custom)

    def _on_attach(self) -> None:
        label = self._type.currentText()
        bp_name = ALL_SENSOR_BLUEPRINTS.get(label, label)
        width, height = RESOLUTIONS[self._resolution.currentText()]
        preset = "" if self._preset.currentText() == "Custom" else self._preset.currentText()
        self.attachRequested.emit(
            bp_name,
            preset,
            width,
            height,
            self._fov.value(),
            self._parent.value(),
            self._rel_x.value(),
            self._rel_y.value(),
            self._rel_z.value(),
            self._pitch.value(),
        )
