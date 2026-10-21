"""桌面客户端启动入口（V2.2.0）。

`main()` 惰性导入 PySide6 并创建 `QApplication` + `MainWindow`，支持
`python -m hunter_sim.gui.app` 与安装后的 `hunter-sim-gui` 命令。缺少 PySide6 时
给出清晰提示而非崩溃（遵循可选依赖惰性导入/降级规范）。
"""

from __future__ import annotations

import sys


def main() -> int:
    """启动桌面客户端；返回进程退出码。"""
    try:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    except ImportError:
        sys.stderr.write(
            "未安装 PySide6，无法启动桌面客户端。\n"
            "请安装 GUI 额外依赖：pip install -e '.[gui]'\n"
        )
        return 1

    from hunter_sim.common.models import HunterSimSettings  # noqa: PLC0415
    from hunter_sim.gui.main_window import MainWindow  # noqa: PLC0415

    app = QApplication(sys.argv)
    app.setApplicationName("HunterSim Desktop Client")
    window = MainWindow(HunterSimSettings())
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
