"""UI：字幕悬浮窗（#6 双行版：原文 + 译文）。

置顶无边框半透明小窗，只显当前一句：上面原文（小），下面译文（大）。
样式打磨（拖动/透明度/位置记忆）留给 #7。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class SubtitleWindow(QWidget):
    """置顶双语字幕窗：原文 + 译文两行，只显当前一句。"""

    # 跨线程安全更新（识别线程 → 主线程）
    text_arrived = Signal(str, str)  # (原文, 译文)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(960, 160)
        self.setWindowTitle("live-caption 字幕")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(4)

        # 译文（主，大号）
        self._trans_label = QLabel("（字幕待出现…）")
        self._trans_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._trans_label.setWordWrap(True)
        self._trans_label.setStyleSheet(
            "QLabel { color: white; font-size: 30px; font-weight: bold;"
            " background-color: rgba(0,0,0,185); border-radius: 10px; padding: 8px 14px; }"
        )

        # 原文（次，小号）
        self._orig_label = QLabel("")
        self._orig_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._orig_label.setWordWrap(True)
        self._orig_label.setStyleSheet(
            "QLabel { color: #dddddd; font-size: 18px;"
            " background-color: rgba(0,0,0,150); border-radius: 8px; padding: 4px 10px; }"
        )

        layout.addWidget(self._trans_label)
        layout.addWidget(self._orig_label)

        self.text_arrived.connect(self._apply_text)
        self._position_at_bottom()

    def _position_at_bottom(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move((geo.width() - self.width()) // 2, geo.height() - self.height() - 60)

    def show_text(self, original: str, translation: str) -> None:
        """线程安全：可从任意线程调用（内部走 Signal）。"""
        self.text_arrived.emit(original, translation)

    def _apply_text(self, original: str, translation: str) -> None:
        # 翻译为空时（失败/未启用翻译），只显示原文
        if translation:
            self._trans_label.setText(translation)
            self._orig_label.setText(original)
            self._orig_label.setVisible(True)
        else:
            self._trans_label.setText(original)
            self._orig_label.setText("")
            self._orig_label.setVisible(False)
