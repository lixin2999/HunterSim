"""Windows 桌面仿真客户端（V2.2.0，CarlaSceneX 风格）。

PySide6 原生桌面应用，进程内直连 CARLA Server 与 ScenarioRunner，支撑 VIL/SIL
自动驾驶仿真测试训练。GUI 组件依赖 PySide6，按需惰性导入；本包的**非界面逻辑**
（`scenario_launcher`、`camera_bridge`、`sim_presets`）不依赖 Qt，可独立单测。

启动：`python -m hunter_sim.gui.app`（或安装后的 `hunter-sim-gui` 命令）。
"""

from __future__ import annotations

from hunter_sim.gui.scenario_launcher import ScenarioRunnerLauncher

__all__ = ["ScenarioRunnerLauncher"]
