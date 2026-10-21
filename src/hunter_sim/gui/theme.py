"""桌面客户端主题样式表（V2.2.0，无 Qt 依赖）。

提供深色（对标 CarlaSceneX 暗色界面）与浅色两套 QSS 字符串，由 `main_window`
按 `GuiSettings.theme` 选择并 `setStyleSheet`。纯字符串，便于单测/维护。
"""

from __future__ import annotations

_DARK = """
QWidget { background: #1e2228; color: #d8dee9; font-family: 'Segoe UI','Microsoft YaHei'; font-size: 13px; }
QGroupBox { border: 1px solid #3b4250; border-radius: 6px; margin-top: 12px; padding: 8px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #7fb3ff; }
QPushButton { background: #2f3540; border: 1px solid #4a5262; border-radius: 4px; padding: 6px 12px; }
QPushButton:hover { background: #3b4350; }
QPushButton:pressed { background: #4a5568; }
QPushButton:disabled { color: #6b7280; background: #262b33; }
QPushButton[danger="true"] { background: #5a2530; border-color: #7d3341; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #17191e; border: 1px solid #3b4250; border-radius: 4px; padding: 4px;
}
QComboBox QAbstractItemView { background: #17191e; border: 1px solid #3b4250; selection-background-color: #2f6fed; }
QLabel[section="title"] { color: #7fb3ff; font-weight: bold; }
QPlainTextEdit, QTextEdit { background: #12141a; border: 1px solid #3b4250; color: #c8d0dc; }
QStatusBar { background: #17191e; color: #9aa4b2; }
QSplitter::handle { background: #2b303a; }
"""

_LIGHT = """
QWidget { background: #f4f5f7; color: #1f2328; font-family: 'Segoe UI','Microsoft YaHei'; font-size: 13px; }
QGroupBox { border: 1px solid #d0d7de; border-radius: 6px; margin-top: 12px; padding: 8px; font-weight: bold; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #0969da; }
QPushButton { background: #eef1f4; border: 1px solid #d0d7de; border-radius: 4px; padding: 6px 12px; }
QPushButton:hover { background: #e4e8ec; }
QPushButton[danger="true"] { background: #ffebe9; border-color: #ff818266; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #ffffff; border: 1px solid #d0d7de; border-radius: 4px; padding: 4px;
}
QLabel[section="title"] { color: #0969da; font-weight: bold; }
QPlainTextEdit, QTextEdit { background: #ffffff; border: 1px solid #d0d7de; }
QStatusBar { background: #eaeef2; }
"""

_THEMES: dict[str, str] = {"dark": _DARK, "light": _LIGHT}


def stylesheet(theme: str = "dark") -> str:
    """返回指定主题的 QSS；未知主题回落深色。"""
    return _THEMES.get(theme, _DARK)
