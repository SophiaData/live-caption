"""音频缓冲逻辑（纯逻辑，可测）。

捕获回调线程里只做一件事：把块塞进本缓冲；消费端按需取整段。
线程安全由锁保证，缓冲有上界（超过 MAX_SECONDS 即丢弃最旧的），
保证慢消费不会撑爆内存。
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np


class AudioBuffer:
    """有界音频块缓冲：线程安全，先进先出，超界丢最旧。"""

    def __init__(self, max_blocks: int) -> None:
        if max_blocks <= 0:
            raise ValueError("max_blocks 必须为正")
        self._blocks: deque[np.ndarray] = deque(maxlen=max_blocks)
        self._lock = threading.Lock()
        self._dropped = 0
        self._blocks_received = 0

    def push(self, block: np.ndarray) -> None:
        """生产端（音频回调线程）调用。"""
        with self._lock:
            if len(self._blocks) == self._blocks.maxlen:
                self._dropped += 1
            self._blocks.append(block)
            self._blocks_received += 1

    def pop_all(self) -> list[np.ndarray]:
        """消费端调用：取出并清空全部块。"""
        with self._lock:
            blocks = list(self._blocks)
            self._blocks.clear()
            return blocks

    @property
    def blocks_received(self) -> int:
        """累计收到的块数（用于验证每秒约 100 块的验收标准）。"""
        with self._lock:
            return self._blocks_received

    @property
    def dropped(self) -> int:
        """因缓冲满而丢弃的块数（背压指标）。"""
        with self._lock:
            return self._dropped
