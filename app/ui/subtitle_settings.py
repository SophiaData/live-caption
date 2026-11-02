"""UI：字幕设置面板（字号 / 透明度 / 位置重置）。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

from .subtitle_window import MAX_FONT, MAX_OPACITY, MIN_FONT, MIN_OPACITY, SubtitleWindow

# 成本说明（Issue #17 硬要求：开关必须用人话写清代价，不许隐瞒）
DRAFT_LABEL = "边说边出草稿字幕（说话时先出字，说完修正这一行）"
DRAFT_COST_NOTE = "开启后识别调用次数会增加（约多 2~4 倍）；译文仍只在句子说完后调用一次。"


class SubtitleSettingsDialog(QDialog):
    """字幕样式设置：字号大小、透明度、位置重置、边说边出开关。即时生效。"""

    def __init__(
        self,
        subtitle: SubtitleWindow,
        parent: QWidget | None = None,
        draft_enabled: bool = True,
        on_draft_change: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("字幕设置")
        self.setModal(True)
        self.resize(400, 240)
        self._subtitle = subtitle
        self._on_draft_change = on_draft_change

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

        # 边说边出草稿字幕（Issue #17）：开关 + 成本说明，即时生效
        self._draft_check = QCheckBox(DRAFT_LABEL)
        self._draft_check.setChecked(draft_enabled)
        self._draft_check.toggled.connect(self._on_draft)
        layout.addRow(self._draft_check)
        self._draft_note = QLabel(DRAFT_COST_NOTE)
        self._draft_note.setWordWrap(True)
        layout.addRow(self._draft_note)

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

    def _on_draft(self, checked: bool) -> None:
        """开关即时生效：主窗口负责写偏好并转给正在跑的管道。"""
        if self._on_draft_change is not None:
            self._on_draft_change(checked)
