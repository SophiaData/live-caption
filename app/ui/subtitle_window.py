"""UI：字幕悬浮窗（#7 打磨版：拖动 / 透明度 / 字号 / 记住位置）。

特性：
- 无边框、始终置顶（#5 已有）
- 鼠标按住可拖动到任意位置
- 位置持久化到配置（重启后回到原处）
- 透明度、字号可由设置调整（通过 apply_style 接口，设置面板调用）
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)

# 样式可调参数范围
MIN_FONT = 14
MAX_FONT = 48
MIN_OPACITY = 0.3
MAX_OPACITY = 1.0


class SubtitleWindow(QWidget):
    """置顶双语字幕窗：可拖动、透明度/字号可调、记住位置。"""

    text_arrived = Signal(str, str)  # (原文, 译文)
    position_changed = Signal(int, int)  # (x, y) 拖动结束时发出，供持久化

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # macOS：让窗口浮在所有 app 之上（含全屏应用、菜单栏层）
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.resize(960, 170)
        self.setWindowTitle("live-caption 字幕")
        self.setToolTip("拖动可移动位置；右键可关闭")

        self._font_size = 30
        self._opacity = 0.9
        self._drag_offset: QPoint | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(4)

        self._trans_label = QLabel("（字幕待出现…）")
        self._trans_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._trans_label.setWordWrap(True)

        self._orig_label = QLabel("")
        self._orig_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._orig_label.setWordWrap(True)

        layout.addWidget(self._trans_label)
        layout.addWidget(self._orig_label)

        self.text_arrived.connect(self._apply_text)
        self._refresh_styles()
        self._position_at_bottom()

        # macOS：周期性把窗口抬到最高层级，防止被其他置顶 app 遮挡
        self._raise_timer = QTimer(self)
        self._raise_timer.setInterval(1500)
        self._raise_timer.timeout.connect(self._keep_on_top)
        self._raise_timer.start()

    def _keep_on_top(self) -> None:
        """macOS：把窗口层级提到浮动层，并允许出现在全屏应用之上。"""
        try:
            import objc
            from AppKit import NSFloatingWindowLevel
            from Foundation import NSObject

            ns_view = objc.objc_object(c_void_p=self.winId().__int__())
            ns_window = ns_view.window()  # winId 是 content view，要取其 window
            if ns_window is None:
                return
            # 浮在所有普通窗口之上（含全屏）
            ns_window.setLevel_(NSFloatingWindowLevel + 1)
            # collectionBehavior: 可出现在所有 Space、全屏辅助、参与全屏
            # 1(CanJoinAllSpaces) | 128(FullScreenAuxiliary) | 256(FullScreenPrimary)
            ns_window.setCollectionBehavior_(1 | 128)
            _ = NSObject  # 保留 import 供 PyObjC 环境
        except Exception:  # noqa: BLE001 - 非 Mac 或无 PyObjC 时静默跳过
            pass

    # ---------- 样式 ----------

    def _refresh_styles(self) -> None:
        """按当前字号/透明度刷新文字样式。"""
        fs = self._font_size
        orig_fs = max(MIN_FONT, int(fs * 0.6))
        alpha = int(255 * self._opacity)
        bg_alpha = int(alpha * 0.75)
        self._trans_label.setStyleSheet(
            f"QLabel {{ color: white; font-size: {fs}px; font-weight: bold;"
            f" background-color: rgba(0,0,0,{bg_alpha}); border-radius: 10px;"
            f" padding: 8px 14px; }}"
        )
        self._orig_label.setStyleSheet(
            f"QLabel {{ color: #dddddd; font-size: {orig_fs}px;"
            f" background-color: rgba(0,0,0,{int(bg_alpha * 0.8)}); border-radius: 8px;"
            f" padding: 4px 10px; }}"
        )

    def apply_style(self, font_size: int | None = None, opacity: float | None = None) -> None:
        """设置面板调用：调整字号/透明度。"""
        if font_size is not None:
            self._font_size = max(MIN_FONT, min(MAX_FONT, font_size))
        if opacity is not None:
            self._opacity = max(MIN_OPACITY, min(MAX_OPACITY, opacity))
        self._refresh_styles()

    @property
    def font_size(self) -> int:
        return self._font_size

    @property
    def opacity(self) -> float:
        return self._opacity

    # ---------- 位置 ----------

    def _position_at_bottom(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move((geo.width() - self.width()) // 2, geo.height() - self.height() - 60)

    def restore_position(self, x: int, y: int) -> None:
        """恢复保存的位置（设置面板/启动时调用）。"""
        self.move(x, y)

    def current_pos(self) -> tuple[int, int]:
        p = self.pos()
        return p.x(), p.y()

    # ---------- 拖动 ----------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt 命名
        if self._drag_offset is not None:
            self._drag_offset = None
            x, y = self.current_pos()
            self.position_changed.emit(x, y)  # 通知持久化
            event.accept()

    # ---------- 文本 ----------

    def show_text(self, original: str, translation: str) -> None:
        """线程安全：任意线程可调。"""
        self.text_arrived.emit(original, translation)

    def _apply_text(self, original: str, translation: str) -> None:
        if translation:
            self._trans_label.setText(translation)
            self._orig_label.setText(original)
            self._orig_label.setVisible(True)
        else:
            self._trans_label.setText(original)
            self._orig_label.setText("")
            self._orig_label.setVisible(False)
