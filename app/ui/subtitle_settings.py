"""UI：字幕设置面板（字号 / 透明度 / 位置重置）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

from .subtitle_window import MAX_FONT, MAX_OPACITY, MIN_FONT, MIN_OPACITY, SubtitleWindow


class SubtitleSettingsDialog(QDialog):
    """字幕样式设置：字号大小、透明度、位置重置。即时生效。"""

    def __init__(self, subtitle: SubtitleWindow, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("字幕设置")
        self.setModal(True)
        self.resize(400, 240)
        self._subtitle = subtitle

        layout = QFormLayout(self)

        # 字号
        self._font_slider = QSlider(Qt.Orientation.Horizontal)
        self._font_slider.setRange(MIN_FONT, MAX_FONT)
        self._font_slider.setValue(subtitle.font_size)
        self._font_val = QLabel(f"{subtitle.font_size}px")
        self._font_slider.valueChanged.connect(self._on_font)
        row = QHBoxLayout()
        row.addWidget(self._font_slider)
        row.addWidget(self._font_val)
        layout.addRow("字号:", row)

        # 透明度
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(int(MIN_OPACITY * 100), int(MAX_OPACITY * 100))
        self._opacity_slider.setValue(int(subtitle.opacity * 100))
        self._opacity_val = QLabel(f"{int(subtitle.opacity * 100)}%")
        self._opacity_slider.valueChanged.connect(self._on_opacity)
        row2 = QHBoxLayout()
        row2.addWidget(self._opacity_slider)
        row2.addWidget(self._opacity_val)
        layout.addRow("透明度:", row2)

        # 位置重置
        self._reset_pos_btn = QPushButton("重置到屏幕底部")
        self._reset_pos_btn.clicked.connect(self._on_reset_pos)
        layout.addRow(self._reset_pos_btn)

        # 关闭
        self._close_btn = QPushButton("完成")
        self._close_btn.clicked.connect(self.accept)
        layout.addRow(self._close_btn)

    def _on_font(self, value: int) -> None:
        self._font_val.setText(f"{value}px")
        self._subtitle.apply_style(font_size=value)

    def _on_opacity(self, value: int) -> None:
        self._opacity_val.setText(f"{value}%")
        self._subtitle.apply_style(opacity=value / 100.0)

    def _on_reset_pos(self) -> None:
        self._subtitle._position_at_bottom()
        self._subtitle.position_changed.emit(*self._subtitle.current_pos())
