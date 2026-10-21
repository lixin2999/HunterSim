"""桌面客户端主窗口（V2.2.0，PySide6）。

QMainWindow 装配左侧控制面板列（连接/地图/Actor/传感器/控制/场景）、右侧
CARLA 实时画面 + 日志控制台，底部状态栏（Carla Status / System）。所有阻塞 CARLA
调用委托给 `SimWorker` 工作线程，GUI 线程仅通过信号/命令投递交互。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from hunter_sim.common.models import HunterSimSettings, VehicleControlCommand
from hunter_sim.gui.panels.actor_panel import ActorPanel
from hunter_sim.gui.panels.connection_panel import ConnectionPanel
from hunter_sim.gui.panels.control_panel import ControlPanel
from hunter_sim.gui.panels.map_panel import MapPanel
from hunter_sim.gui.panels.scenario_panel import ScenarioPanel
from hunter_sim.gui.panels.sensor_panel import SensorPanel
from hunter_sim.gui.scenario_launcher import ScenarioRunnerLauncher
from hunter_sim.gui.sim_worker import SimWorker
from hunter_sim.gui.theme import stylesheet
from hunter_sim.gui.views.stream_view import StreamView
from hunter_sim.gui.widgets.log_console import LogConsole

# 手动模式键盘 -> 控制轴映射
_KEY_MAP: dict[int, str] = {
    Qt.Key.Key_W: "throttle",
    Qt.Key.Key_S: "brake",
    Qt.Key.Key_A: "left",
    Qt.Key.Key_D: "right",
}


class MainWindow(QMainWindow):
    """CarlaSceneX 风格桌面仿真客户端主窗口。"""

    def __init__(self, settings: Optional[HunterSimSettings] = None) -> None:
        super().__init__()
        self._settings = settings or HunterSimSettings()
        self._mode = self._settings.gui  # 便捷引用
        self._actor_ids: list[int] = []
        self._pressed: set[str] = set()

        self.setWindowTitle("HUNTER SE — CARLA 仿真客户端")
        self.resize(1280, 800)
        self.setStyleSheet(stylesheet(self._settings.gui.theme))

        self._worker = SimWorker(self)
        self._launcher: Optional[ScenarioRunnerLauncher] = None

        self._build_panels()
        self._build_layout()
        self._build_menu()
        self._build_statusbar()
        self._connect_signals()

        self._worker.start()

    # ── 组装 ────────────────────────────────────────────────────────────
    def _build_panels(self) -> None:
        carla = self._settings.carla
        self._conn = ConnectionPanel(host=carla.host, rpc_port=carla.rpc_port)
        self._map = MapPanel(default_map=self._settings.gui.default_map)
        self._actor = ActorPanel()
        self._sensor = SensorPanel()
        self._control = ControlPanel()
        self._scenario = ScenarioPanel()
        self._stream = StreamView()
        self._log = LogConsole()

    def _build_layout(self) -> None:
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        for panel in (self._conn, self._map, self._actor, self._sensor, self._control, self._scenario):
            left_layout.addWidget(panel)
        left_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self._stream)
        right.addWidget(self._log)
        right.setSizes([520, 240])

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(scroll)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 900])
        self.setCentralWidget(splitter)

    def _build_menu(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        self._act_disconnect = QAction("Disconnect", self)
        self._act_disconnect.setShortcut(QKeySequence("Ctrl+D"))
        self._act_disconnect.triggered.connect(self._on_disconnect)
        act_exit = QAction("Exit", self)
        act_exit.setShortcut(QKeySequence("Ctrl+Q"))
        act_exit.triggered.connect(self.close)
        file_menu.addAction(self._act_disconnect)
        file_menu.addSeparator()
        file_menu.addAction(act_exit)

        features = menubar.addMenu("Features")
        for label, slot in (
            ("Map", lambda: self._map.setFocus()),
            ("Actor", lambda: self._actor.setFocus()),
            ("Sensor", lambda: self._sensor.setFocus()),
            ("Scenario", lambda: self._scenario.setFocus()),
        ):
            act = QAction(label, self)
            act.triggered.connect(slot)
            features.addAction(act)

    def _build_statusbar(self) -> None:
        self._status_carla = QLabel("Carla Status: Offline")
        self._status_system = QLabel("System: Ready")
        bar = self.statusBar()
        bar.addPermanentWidget(self._status_carla)
        bar.addPermanentWidget(self._status_system)
        bar.showMessage("就绪", 5000)

    def _connect_signals(self) -> None:
        # 面板 -> Worker
        self._conn.connectRequested.connect(self._on_connect)
        self._map.loadRequested.connect(self._worker.request_load_map)
        self._map.importRequested.connect(self._worker.request_import_xodr)
        self._actor.filterRequested.connect(self._worker.request_blueprints)
        self._actor.spawnRequested.connect(self._worker.request_spawn)
        self._actor.clearRequested.connect(self._worker.request_clear)
        self._sensor.attachRequested.connect(self._worker.request_attach_sensor)
        self._control.startRequested.connect(self._worker.request_start)
        self._control.pauseRequested.connect(self._worker.request_pause)
        self._control.stopRequested.connect(self._worker.request_stop)
        self._control.autopilotToggled.connect(self._on_autopilot)
        self._control.modeChanged.connect(self._on_mode_changed)
        self._scenario.startRequested.connect(self._on_scenario_start)
        self._scenario.stopRequested.connect(self._on_scenario_stop)

        # Worker -> 面板/视图
        self._worker.connected.connect(self._on_connected)
        self._worker.connectionFailed.connect(self._on_connection_failed)
        self._worker.disconnected.connect(lambda: self._status_carla.setText("Carla Status: Offline"))
        self._worker.frameReady.connect(self._stream.on_frame)
        self._worker.stateUpdated.connect(self._on_state)
        self._worker.logLine.connect(self._log.append_line)
        self._worker.errorOccurred.connect(self._on_error)
        self._worker.actorsChanged.connect(self._on_actors_changed)
        self._worker.sensorsChanged.connect(self._stream.set_sources)
        self._worker.blueprintsReady.connect(self._actor.set_blueprints)
        self._worker.mapChanged.connect(self._map.set_current)

    # ── 槽：连接 ────────────────────────────────────────────────────────
    def _on_connect(self, host: str, port: int, timeout: float) -> None:
        self._settings.carla.host = host
        self._settings.carla.rpc_port = port
        self._worker.set_tm_port(self._conn.tm_port)
        self._status_system.setText("System: Connecting...")
        self._worker.request_connect(host, port, timeout)

    def _on_connected(self, version: str) -> None:
        self._status_carla.setText("Carla Status: Online")
        self._status_system.setText(f"System: Connected (CARLA {version})")
        self._conn.on_connect_result(True)

    def _on_connection_failed(self, err: str) -> None:
        self._conn.on_connect_result(False)
        self._status_system.setText("System: Connect failed")
        self._log.append_line(f"连接失败：{err}")

    def _on_disconnect(self) -> None:
        self._worker.request_stop()
        self._status_carla.setText("Carla Status: Offline")

    # ── 槽：状态/ Actor / 模式 ──────────────────────────────────────────
    def _on_state(self, state: object) -> None:
        assert isinstance(state, dict)
        self._status_system.setText(
            f"System: x={state['x']} y={state['y']} v={state['speed']}km/h"
        )

    def _on_error(self, err: str) -> None:
        self._log.append_line(f"ERROR: {err}")

    def _on_actors_changed(self, ids: object) -> None:
        assert isinstance(ids, list)
        self._actor_ids = ids
        self._sensor.set_parents(ids)

    def _on_autopilot(self, enabled: bool) -> None:
        self._worker.request_set_autopilot(enabled)

    def _on_mode_changed(self, mode: str) -> None:
        self._worker.request_set_sync(True)
        self._log.append_line(f"切换到 {mode.upper()} 模式")

    # ── 槽：ScenarioRunner ──────────────────────────────────────────────
    def _on_scenario_start(self, xosc: str, scenario: str) -> None:
        if self._launcher is None:
            self._launcher = ScenarioRunnerLauncher(
                self._settings.carla,
                scenario_runner_path=self._settings.gui.scenario_runner_path,
            )
        path = Path(xosc) if xosc else None
        name = scenario if scenario else None
        if path is None and name is None:
            self._log.append_line("场景启动需指定 .xosc 或内置场景名")
            return
        if self._launcher.start(openscenario=path, scenario=name, sync=True):
            self._scenario.set_status("Running")
            self._log.append_line(f"ScenarioRunner 已启动：{xosc or scenario}")

    def _on_scenario_stop(self) -> None:
        if self._launcher is not None:
            self._launcher.stop()
            self._scenario.set_status("Idle")
            self._log.append_line("ScenarioRunner 已停止")

    # ── 手动键盘控制（Manual 模式）──────────────────────────────────────
    def keyPressEvent(self, event: object) -> None:  # noqa: N802  Qt 命名
        axis = _KEY_MAP.get(int(event.key()))  # type: ignore[attr-defined]
        if axis is not None and not self._control.is_autopilot:
            self._pressed.add(axis)
            self._emit_manual_control()
            return
        super().keyPressEvent(event)  # type: ignore[arg-type]

    def keyReleaseEvent(self, event: object) -> None:  # noqa: N802  Qt 命名
        axis = _KEY_MAP.get(int(event.key()))  # type: ignore[attr-defined]
        if axis is not None:
            self._pressed.discard(axis)
            self._emit_manual_control()
            return
        super().keyReleaseEvent(event)  # type: ignore[arg-type]

    def _emit_manual_control(self) -> None:
        steer = (0.6 if "left" in self._pressed else 0.0) - (0.6 if "right" in self._pressed else 0.0)
        cmd = VehicleControlCommand(
            throttle=1.0 if "throttle" in self._pressed else 0.0,
            brake=1.0 if "brake" in self._pressed else 0.0,
            steer=steer,
        )
        self._worker.request_control(cmd)

    # ── 关闭 ────────────────────────────────────────────────────────────
    def closeEvent(self, event: object) -> None:  # noqa: N802  Qt 命名
        if self._launcher is not None:
            self._launcher.stop()
        self._worker.shutdown()
        self._worker.wait(3000)
        super().closeEvent(event)  # type: ignore[arg-type]
