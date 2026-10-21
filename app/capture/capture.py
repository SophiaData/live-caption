"""系统音频流捕获器：打开回环设备，持续把块送入 AudioBuffer。"""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np
import sounddevice as sd

from .buffer import AudioBuffer
from .devices import BLOCKSIZE, CHANNELS, SAMPLE_RATE, AudioDevice, pick_device

logger = logging.getLogger(__name__)

# 缓冲上界：60 秒音频（100 块/秒），足够断句消费端取数据
MAX_SECONDS = 60


class CaptureError(Exception):
    """捕获失败，message 为给用户的人话。"""


class SystemAudioCapture:
    """从回环设备持续捕获系统声音。

    用法：
        cap = SystemAudioCapture()
        cap.start()          # 开始捕获
        ...
        cap.stop()
    消费端通过 blocks_received / AudioBuffer 获取数据。
    """

    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._buffer = AudioBuffer(max_blocks=MAX_SECONDS * (SAMPLE_RATE // BLOCKSIZE))
        self._on_level: Callable[[float], None] | None = None
        self._on_block: Callable[[np.ndarray], None] | None = None

    def set_level_callback(self, cb: Callable[[float], None]) -> None:
        """注册音量回调（0~1），UI 音量条用。"""
        self._on_level = cb

    def set_block_callback(self, cb: Callable[[np.ndarray], None]) -> None:
        """注册音频块回调（每 10ms 一块），识别管道用。"""
        self._on_block = cb

    @property
    def blocks_received(self) -> int:
        return self._buffer.blocks_received

    def pop_audio(self) -> list[np.ndarray]:
        """取走当前缓冲的全部块。"""
        return self._buffer.pop_all()

    def start(self, device_index: int | None = None) -> AudioDevice:
        """打开捕获流。device_index 为 None 时自动选择回环设备。"""
        if self._stream is not None:
            raise CaptureError("捕获已在进行中")

        names_and_channels = [
            (i, d["name"], d["max_input_channels"]) for i, d in enumerate(sd.query_devices())
        ]
        device: AudioDevice | None = None
        if device_index is not None:
            for i, name, ch in names_and_channels:
                if i == device_index:
                    device = AudioDevice(index=i, name=name, max_input_channels=ch)
        else:
            device = pick_device(names_and_channels)
        if device is None:
            from .devices import missing_device_hint

            raise CaptureError(missing_device_hint())

        def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
            if status:
                logger.warning("捕获回调状态: %s", status)
            block = indata[:, :CHANNELS].copy()
            self._buffer.push(block)
            if self._on_level is not None:
                self._on_level(
                    float(
                        np.abs(block).max() / 32768.0
                        if block.dtype == np.int16
                        else np.abs(block).max()
                    )
                )
            if self._on_block is not None:
                self._on_block(block.reshape(-1).astype(np.float32))

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                channels=CHANNELS,
                dtype="float32",
                device=device.index,
                callback=callback,
            )
            self._stream.start()
        except Exception as e:  # noqa: BLE001 - 统一转成给用户的人话错误
            self._stream = None
            raise CaptureError(
                f"打开音频设备「{device.name}」失败：{e}。\n"
                "请检查系统声音设置，确认该设备未被其他独占式应用占用。"
            ) from e
        logger.info("开始捕获：%s (设备 %d)", device.name, device.index)
        return device

    def stop(self) -> None:
        """停止并关闭捕获流（幂等）。"""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            logger.info("捕获已停止")

    @property
    def running(self) -> bool:
        return self._stream is not None
