"""Mac 免驱动系统声音捕获（ScreenCaptureKit）：流会话与启停控制。

与 SystemAudioCapture 相同的消费接口（blocks_received / pop_audio / level 回调），
复用 AudioBuffer 有界缓冲。PyObjC 会话细节全部隔离在 _SCKSession 内，
主捕获类不 import PyObjC（保证纯逻辑部分在非 Mac 上可导入可测）。
"""

from __future__ import annotations

import logging
import platform
from collections.abc import Callable

import numpy as np

from .buffer import AudioBuffer
from .sck import (
    MIN_MACOS_MAJOR,
    SCKCaptureError,
    macos_version,
    resample_to_16k,
    unsupported_hint,
)

logger = logging.getLogger(__name__)


class SCKSystemAudioCapture:
    """免驱动系统声音捕获（macOS 13+）。

    接口与 SystemAudioCapture 对齐：set_level_callback / blocks_received /
    pop_audio / running / start / stop。UI 层无感切换。
    """

    def __init__(self) -> None:
        self._session: object | None = None
        self._buffer = AudioBuffer(max_blocks=60 * 100)  # 60 秒 @100 块/秒
        self._on_level: Callable[[float], None] | None = None
        self._block_cb: Callable[[np.ndarray], None] | None = None
        self._blocks_received = 0
        self._is_mac = platform.system() == "Darwin"

    def set_level_callback(self, cb: Callable[[float], None]) -> None:
        self._on_level = cb

    def set_block_callback(self, cb: Callable[[np.ndarray], None]) -> None:
        """注册音频块回调（识别管道用）。"""
        self._block_cb = cb

    @property
    def blocks_received(self) -> int:
        return self._blocks_received

    def pop_audio(self) -> list[np.ndarray]:
        return self._buffer.pop_all()

    @property
    def running(self) -> bool:
        return self._session is not None

    def start(self) -> None:
        if self._session is not None:
            raise SCKCaptureError("捕获已在进行中")
        if not self._is_mac or macos_version()[0] < MIN_MACOS_MAJOR:
            raise SCKCaptureError(unsupported_hint())

        from .sck_session import SCKSession  # Mac-only，延迟导入

        session = SCKSession(on_block=self._on_block)
        session.start()  # 授权、找显示器、开流；失败抛 SCKCaptureError（人话）
        self._session = session
        logger.info("ScreenCaptureKit 系统声音捕获已启动")

    def _on_block(self, block: np.ndarray, level: float) -> None:
        """音频回调（SCK 线程）：塞缓冲 + 转发音量回调。"""
        self._buffer.push(block.reshape(-1, 1))
        self._blocks_received += 1
        if self._on_level is not None:
            self._on_level(level)
        if self._block_cb is not None:
            self._block_cb(block.reshape(-1).astype(np.float32))

    def stop(self) -> None:
        if self._session is not None:
            from .sck_session import SCKSession

            assert isinstance(self._session, SCKSession)
            self._session.stop()
            self._session = None
            logger.info("ScreenCaptureKit 捕获已停止")


def normalize_sck_frame(sample: tuple[np.ndarray, int] | None) -> tuple[np.ndarray, float] | None:
    """把 (mono 采样, 采样率) 规整成 (16k 单声道块, 峰值音量)。纯逻辑，可测。"""
    if sample is None or len(sample[0]) == 0:
        return None
    samples, rate = sample
    mono16k = resample_to_16k(samples, rate)
    level = float(np.abs(mono16k).max()) if len(mono16k) else 0.0
    return mono16k, level
