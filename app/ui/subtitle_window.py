"""UI：字幕悬浮窗（#5 最简版，样式打磨留给 #7）。

置顶无边框半透明小窗，显示当前一句英文字幕。只显当前一句（老板裁决）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class SubtitleWindow(QWidget):
    """置顶字幕窗：只显示当前一句。"""

    # 跨线程安全更新（识别线程 → 主线程）
    text_arrived = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(900, 120)
        self.setWindowTitle("live-caption 字幕")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        self._label = QLabel("（字幕待出现…）")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            "QLabel { color: white; font-size: 28px; font-weight: bold;"
            " background-color: rgba(0,0,0,180); border-radius: 8px; padding: 10px; }"
        )
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        self.text_arrived.connect(self._apply_text)

        # 默认放屏幕底部居中
        self._position_at_bottom()

    def _position_at_bottom(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move((geo.width() - self.width()) // 2, geo.height() - self.height() - 60)

    def show_text(self, text: str) -> None:
        """线程安全：可从任意线程调用（内部走 Signal）。"""
        self.text_arrived.emit(text)

    def _apply_text(self, text: str) -> None:
        self._label.setText(text)
