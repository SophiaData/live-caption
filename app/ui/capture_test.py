"""UI：主窗口（捕获 + 实时字幕）。

功能：选设备 → 打开「实时字幕」开关 → 捕获 → 断句 → 识别 → 字幕窗显示当前一句。
音频线程的音量/块都通过信号或回调转发；识别在后台线程，UI 永不卡死。
Mac 首选项「系统声音（免驱动）」走 ScreenCaptureKit；其余走 sounddevice。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
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
from ..capture.sck import SCKCaptureError
from ..capture.sck_capture import SCKSystemAudioCapture
from ..config.store import load_config
from ..pipeline.asr_pipeline import RecognitionPipeline
from ..pipeline.recognizer import CloudRecognizer, Recognizer
from ..providers.openai_compatible import ModelConfig
from .subtitle_window import SubtitleWindow

SCK_ENTRY = "系统声音（免驱动 · macOS 13+）"
LOCAL_ASR_ENTRY = "本地识别（faster-whisper，免注册）"


class _CaptureLike(Protocol):
    @property
    def blocks_received(self) -> int: ...

    def set_level_callback(self, cb: Callable[[float], None]) -> None: ...

    def set_block_callback(self, cb: Callable[[object], None]) -> None: ...

    def stop(self) -> None: ...


class LevelRelay(QObject):
    """音频线程音量 → Qt 信号（跨线程安全）。"""

    levelChanged = Signal(float)

    def emit_level(self, level: float) -> None:
        self.levelChanged.emit(level)


class CaptureTestWindow(QMainWindow):
    """主窗口：捕获 + 实时字幕。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("live-caption")
        self.resize(520, 320)
        self._device_capture = SystemAudioCapture()
        self._sck_capture = SCKSystemAudioCapture()
        self._active_tag: str | None = None
        self._update_stats_source: _CaptureLike | None = None
        self._pipeline: RecognitionPipeline | None = None
        self._subtitle: SubtitleWindow | None = None
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

        self._subtitle_check = QCheckBox("实时字幕（识别 + 显示）")
        self._subtitle_check.setChecked(True)
        layout.addWidget(self._subtitle_check)

        self._toggle_btn = QPushButton("开始捕获")
        layout.addWidget(self._toggle_btn)

        self._level_label = QLabel("音量: ▁▁▁▁▁▁▁▁▁▁")
        self._level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._level_label)

        self._stats_label = QLabel("块数: 0（目标每秒约 100）")
        self._stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._stats_label)

        self._asr_label = QLabel("识别状态: 未启动")
        self._asr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._asr_label)

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
        self._device_combo.addItem(SCK_ENTRY, None)
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                self._device_combo.addItem(f"{d['name']}", i)

    # ---------- 识别器构造 ----------

    def _build_recognizer(self) -> tuple[Recognizer | None, str]:
        """按配置构造识别器。返回 (识别器, 错误提示)。"""
        cfg = load_config("asr")
        if cfg is None:
            # 未配置：友好提示（老板裁决「友好提示」）
            return None, (
                "还没有配置识别服务。\n请先运行软件配置向导填写识别服务（或用本地识别方案）。"
            )
        return CloudRecognizer(
            ModelConfig(base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model)
        ), ""

    # ---------- 启停 ----------

    def _toggle(self) -> None:
        if self._active_tag is not None:
            self._stop_all()
            return
        self._error_label.setText("")
        selection = self._device_combo.currentData()

        # 若开了实时字幕，先准备识别管道
        pipeline: RecognitionPipeline | None = None
        if self._subtitle_check.isChecked():
            recognizer, err = self._build_recognizer()
            if recognizer is None:
                self._error_label.setText(err)  # 友好提示，不启动
                return
            pipeline = RecognitionPipeline(recognizer)
            self._pipeline = pipeline
            self._asr_label.setText("识别状态: 准备中（首次启动需加载）")
            if self._subtitle is None:
                self._subtitle = SubtitleWindow()
            self._subtitle.show()
            # 识别结果 → 字幕窗（字幕窗内部用 Signal 保证跨线程安全）
            pipeline.set_on_text(self._on_asr_text)
            pipeline.start()

        try:
            if selection is None:
                self._sck_capture.set_level_callback(self._relay.emit_level)
                if pipeline is not None:
                    self._sck_capture.set_block_callback(pipeline.feed_block)
                self._sck_capture.start()
                self._active_tag = "sck"
                self._update_stats_source = self._sck_capture
                self.setWindowTitle("live-caption · 捕获中: 系统声音（免驱动）")
            else:
                self._device_capture.set_level_callback(self._relay.emit_level)
                if pipeline is not None:
                    self._device_capture.set_block_callback(pipeline.feed_block)
                device = self._device_capture.start(device_index=selection)
                self._active_tag = "device"
                self._update_stats_source = self._device_capture
                self.setWindowTitle(f"live-caption · 捕获中: {device.name}")
            self._last_blocks = 0
            self._timer.start()
            self._toggle_btn.setText("停止")
            if pipeline is not None:
                self._asr_label.setText("识别状态: 运行中")
        except (CaptureError, SCKCaptureError) as e:
            self._active_tag = None
            self._update_stats_source = None
            self._error_label.setText(str(e))
            if self._pipeline is not None:
                self._pipeline.stop()
                self._pipeline = None

    def _stop_all(self) -> None:
        if self._pipeline is not None:
            self._pipeline.flush()
            self._pipeline.stop()
            self._pipeline = None
        if self._active_tag == "sck":
            self._sck_capture.stop()
        elif self._active_tag == "device":
            self._device_capture.stop()
        self._active_tag = None
        self._update_stats_source = None
        self._timer.stop()
        self._toggle_btn.setText("开始捕获")
        self._error_label.setText("")
        self._asr_label.setText("识别状态: 未启动")
        self.setWindowTitle("live-caption")

    def _on_asr_text(self, text: str, _utt: object) -> None:
        """识别线程回调 → 转给字幕窗（字幕窗用 Signal 跨线程安全）。"""
        if self._subtitle is not None:
            self._subtitle.show_text(text)

    # ---------- 音量与统计 ----------

    def _on_level(self, level: float) -> None:
        self._max_level_recent = max(self._max_level_recent, level)
        bars = min(10, int(self._max_level_recent * 10) + (1 if self._max_level_recent > 0 else 0))
        self._level_label.setText("音量: " + "▁" * (10 - bars) + "▇" * bars)

    def _update_stats(self) -> None:
        if self._max_level_recent > 0:
            self._max_level_recent = 0.0
            self._level_label.setText("音量: ▁▁▁▁▁▁▁▁▁▁")
        if self._update_stats_source is None:
            return
        blocks = self._update_stats_source.blocks_received
        per_sec = blocks - self._last_blocks
        self._last_blocks = blocks
        self._stats_label.setText(
            f"块数: {blocks}（本秒 +{per_sec}，目标每秒约 {1000 // BLOCK_MS}）"
        )

    def closeEvent(self, event: object) -> None:  # noqa: N802 - Qt 命名
        if self._active_tag is not None or self._pipeline is not None:
            self._stop_all()
        if self._subtitle is not None:
            self._subtitle.close()
        super().closeEvent(event)  # type: ignore[arg-type]
