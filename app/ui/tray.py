"""UI：Mac 顶部菜单栏常驻图标（#8）。

功能：
- 顶部菜单栏常驻图标（圆形色块，运行时变红）
- 一键开始/停止字幕
- 打开字幕设置
- 显示当前模型信息
- 通过回调与主窗口交互（主窗口提供 on_toggle / on_settings / model_info）
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

logger = logging.getLogger(__name__)


def _make_icon(color: str = "#4A90D9") -> QIcon:
    """生成一个简单圆形图标（避免依赖外部图片资源）。"""
    pix = QPixmap(22, 22)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QBrush(QColor(color)))
    painter.setPen(QPen(Qt.PenStyle.NoPen))
    painter.drawEllipse(2, 2, 18, 18)
    painter.end()
    return QIcon(pix)


class TrayController:
    """菜单栏托盘控制器。"""

    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_settings: Callable[[], None],
        model_info: Callable[[], str],
    ) -> None:
        self._on_toggle = on_toggle
        self._on_settings = on_settings
        self._model_info = model_info

        self._tray = QSystemTrayIcon(_make_icon(), None)
        self._tray.setToolTip("live-caption 实时字幕")

        self._menu = QMenu()
        self._toggle_action = QAction("开始字幕")
        self._toggle_action.triggered.connect(self._on_toggle)
        self._menu.addAction(self._toggle_action)

        self._settings_action = QAction("字幕设置…")
        self._settings_action.triggered.connect(self._on_settings)
        self._menu.addAction(self._settings_action)

        self._info_action = QAction("模型: 加载中…")
        self._info_action.setEnabled(False)
        self._menu.addAction(self._info_action)

        self._menu.addSeparator()
        quit_action = QAction("退出")
        quit_action.triggered.connect(self._quit)
        self._menu.addAction(quit_action)

        self._tray.setContextMenu(self._menu)
        self._tray.show()

    def set_running(self, running: bool, error: str = "") -> None:
        """更新运行状态（菜单文案 + 图标颜色）。"""
        self._toggle_action.setText("停止字幕" if running else "开始字幕")
        self._tray.setIcon(_make_icon("#E74C3C" if running else "#4A90D9"))
        if error:
            self.show_message("字幕无法启动", error)

    def refresh_model_info(self) -> None:
        self._info_action.setText(f"模型: {self._model_info()}")

    def _quit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def show_message(self, title: str, msg: str) -> None:
        self._tray.showMessage(title, msg, QSystemTrayIcon.MessageIcon.Warning, 5000)
