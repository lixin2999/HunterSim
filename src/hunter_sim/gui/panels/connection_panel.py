"""连接面板（V2.2.0，PySide6）：CARLA host/端口/超时 + Connect。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)


class ConnectionPanel(QGroupBox):
    """CARLA 连接参数与连接按钮。"""

    connectRequested = Signal(str, int, float)  # host, rpc_port, timeout

    def __init__(self, host: str = "127.0.0.1", rpc_port: int = 2000, parent: Optional[QGroupBox] = None) -> None:
        super().__init__("Connect to Carla", parent)
        layout = QVBoxLayout(self)

        self._host = QLineEdit(host)
        self._rpc = QSpinBox(minimum=1024, maximum=65535, value=rpc_port)
        self._stream = QSpinBox(minimum=1024, maximum=65535, value=rpc_port + 1)
        self._tm = QSpinBox(minimum=1024, maximum=65535, value=8000)
        self._timeout = QDoubleSpinBox(minimum=1.0, maximum=120.0, value=10.0, suffix=" s")

        form = QFormLayout()
        form.addRow("Host:", self._host)
        form.addRow("RPC Port:", self._rpc)
        form.addRow("Stream Port:", self._stream)
        form.addRow("TM Port:", self._tm)
        form.addRow("Timeout:", self._timeout)
        layout.addLayout(form)

        row = QHBoxLayout()
        self._connect_btn = QPushButton("Connect")
        self._status = QLabel("Offline")
        self._connect_btn.clicked.connect(self._on_connect)
        row.addWidget(self._connect_btn)
        row.addWidget(self._status)
        layout.addLayout(row)

    @property
    def tm_port(self) -> int:
        """Traffic Manager 端口。"""
        return self._tm.value()

    def set_status(self, text: str, ok: bool) -> None:
        """更新连接状态标签。"""
        self._status.setText("Online" if ok else "Offline")

    def _on_connect(self) -> None:
        self._connect_btn.setEnabled(False)
        self._status.setText("Connecting...")
        self.connectRequested.emit(self._host.text().strip(), self._rpc.value(), self._timeout.value())

    def on_connect_result(self, ok: bool) -> None:
        """连接结果回填（主窗口调用）。"""
        self._connect_btn.setEnabled(True)
        self.set_status("", ok)
