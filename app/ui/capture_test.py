"""UI：捕获测试页。

验收用：选择设备 → 开始捕获 → 音量条跳动 → 显示每秒块数。
音频线程的音量通过 Qt 信号转发到主线程（禁止跨线程直改 UI）。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal
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


class LevelRelay(QObject):
    """把音频线程的音量回调转成 Qt 信号（自动排队到主线程）。"""

    levelChanged = Signal(float)

    def emit_level(self, level: float) -> None:
        """供音频回调线程调用；信号跨线程自动走 queued connection。"""
        self.levelChanged.emit(level)


class CaptureTestWindow(QMainWindow):
    """捕获测试窗口：验证系统声音真的进了管道。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("live-caption · 捕获测试")
        self.resize(420, 240)
        self._capture = SystemAudioCapture()
        self._last_blocks = 0
        self._max_level_recent = 0.0

        self._relay = LevelRelay()
        self._relay.levelChanged.connect(self._on_level)

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
            self._capture.set_level_callback(self._relay.emit_level)
            device = self._capture.start(device_index=device_index)
            self.setWindowTitle(f"live-caption · 捕获中: {device.name}")
            self._last_blocks = 0
            self._timer.start()
            self._toggle_btn.setText("停止捕获")
        except CaptureError as e:
            self._error_label.setText(str(e))

    def _on_level(self, level: float) -> None:
        """主线程槽：更新音量条（信号驱动，不轮询）。"""
        self._max_level_recent = max(self._max_level_recent, level)
        bars = min(10, int(self._max_level_recent * 10) + (1 if self._max_level_recent > 0 else 0))
        self._level_label.setText("音量: " + "▁" * (10 - bars) + "▇" * bars)

    def _update_stats(self) -> None:
        blocks = self._capture.blocks_received
        per_sec = blocks - self._last_blocks
        self._last_blocks = blocks
        self._stats_label.setText(
            f"块数: {blocks}（本秒 +{per_sec}，目标每秒约 {1000 // BLOCK_MS}）"
        )
        # 衰减：本秒结束清零峰值并让音量条回落
        if self._max_level_recent > 0:
            self._max_level_recent = 0.0
            self._level_label.setText("音量: ▁▁▁▁▁▁▁▁▁▁")

    def closeEvent(self, event: object) -> None:  # noqa: N802 - Qt 命名
        self._capture.stop()
        super().closeEvent(event)  # type: ignore[arg-type]
