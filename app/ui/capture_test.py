"""UI：捕获测试页。

验收用：选择设备 → 开始捕获 → 音量条跳动 → 显示每秒块数。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..capture.capture import CaptureError, SystemAudioCapture
from ..capture.devices import BLOCK_MS


class CaptureTestWindow(QMainWindow):
    """捕获测试窗口：验证系统声音真的进了管道。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("live-caption · 捕获测试")
        self.resize(420, 240)
        self._capture = SystemAudioCapture()
        self._last_blocks = 0

        central = QWidget()
        layout = QVBoxLayout(central)

        top = QHBoxLayout()
        top.addWidget(QLabel("捕获设备:"))
        self._device_combo = QComboBox()
        top.addWidget(self._device_combo, stretch=1)
        layout.addLayout(top)

        self._toggle_btn = QPushButton("开始捕获")
        layout.addWidget(self._toggle_btn)

        self._level_label = QLabel("音量: ▁▁▁▁▁▁▁▁▁▁")
        self._level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._level_label)

        self._stats_label = QLabel("块数: 0（目标每秒约 100）")
        self._stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._stats_label)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        self.setCentralWidget(central)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_stats)

        self._toggle_btn.clicked.connect(self._toggle)
        self._refresh_devices()

    def _refresh_devices(self) -> None:
        import sounddevice as sd

        self._device_combo.clear()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                self._device_combo.addItem(f"{d['name']}", i)

    def _toggle(self) -> None:
        if self._capture.running:
            self._capture.stop()
            self._timer.stop()
            self._toggle_btn.setText("开始捕获")
            self._error_label.setText("")
            return
        self._error_label.setText("")
        device_index = self._device_combo.currentData()
        try:
            device = self._capture.start(device_index=device_index)
            self.setWindowTitle(f"live-caption · 捕获中: {device.name}")
            self._last_blocks = 0
            self._timer.start()
            self._toggle_btn.setText("停止捕获")
        except CaptureError as e:
            self._error_label.setText(str(e))

    def _update_stats(self) -> None:
        blocks = self._capture.blocks_received
        per_sec = blocks - self._last_blocks
        self._last_blocks = blocks
        self._stats_label.setText(
            f"块数: {blocks}（本秒 +{per_sec}，目标每秒约 {1000 // BLOCK_MS}）"
        )

    def set_level(self, level: float) -> None:
        """由音量回调驱动（跨线程安全由 Qt 信号保证时可改为信号；测试页用轮询近似）。"""
        bars = min(10, int(level * 10) + (1 if level > 0 else 0))
        self._level_label.setText("音量: " + "▁" * (10 - bars) + "▇" * bars)

    def closeEvent(self, event: object) -> None:  # noqa: N802 - Qt 命名
        self._capture.stop()
        super().closeEvent(event)  # type: ignore[arg-type]
