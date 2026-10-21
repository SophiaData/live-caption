"""识别管道：把「捕获 → 断句 → 识别 → 字幕」串起来。

设计：
- 采集线程（Qt 定时器或后台线程）不断从捕获器取音频块，喂断句器
- 断句器切出整句后，送后台识别线程（QThread），避免卡 UI
- 识别结果通过回调发到 UI 显示字幕
- 队列有界，慢识别不无限堆积（背压）
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import numpy as np

from .recognizer import Recognizer
from .segmenter import Segmenter, Utterance
from .translator import Translator

logger = logging.getLogger(__name__)


class RecognitionPipeline:
    """识别管道（线程安全，UI 无关）。

    用法：
        pipe = RecognitionPipeline(recognizer)
        pipe.set_on_text(callback)  # callback(text: str) 在后台线程被调
        pipe.start()
        pipe.feed_block(audio_block)  # 从捕获回调调用
        pipe.stop()
    """

    def __init__(self, recognizer: Recognizer, translator: Translator | None = None) -> None:
        self._recognizer = recognizer
        self._translator = translator
        self._segmenter = Segmenter()
        self._on_text: Callable[[str, str, Utterance], None] | None = None
        self._queue: list[Utterance] = []
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._running = False
        self._max_queue = 20  # 背压：最多积压 20 句
        self._dropped = 0

    def set_on_text(self, cb: Callable[[str, str, Utterance], None]) -> None:
        """注册字幕回调 (原文, 译文, utt)；在后台线程调用，UI 侧用信号转发。"""
        self._on_text = cb

    @property
    def dropped_utterances(self) -> int:
        """因积压满而丢弃的句数（背压指标）。"""
        return self._dropped

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._work_loop, daemon=True, name="asr-worker")
        self._worker.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._queue.clear()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None

    def feed_block(self, block: np.ndarray) -> None:
        """喂音频块（从捕获回调线程调用，必须非阻塞）。"""
        self._segmenter.feed(block)
        for utt in self._segmenter.pop_utterances():
            self._enqueue(utt)

    def flush(self) -> None:
        """流结束时把剩余未断的音频也送去识别。"""
        for utt in self._segmenter.flush():
            self._enqueue(utt)

    def _enqueue(self, utt: Utterance) -> None:
        with self._lock:
            if len(self._queue) >= self._max_queue:
                self._dropped += 1
                logger.warning("识别队列已满，丢弃一句（背压）")
                return
            self._queue.append(utt)

    def _work_loop(self) -> None:
        while self._running:
            utt: Utterance | None = None
            with self._lock:
                if self._queue:
                    utt = self._queue.pop(0)
            if utt is None:
                import time

                time.sleep(0.05)  # 空转小睡，避免忙等
                continue
            ok, result = self._recognizer.transcribe(utt.samples, 16000)
            if not ok:
                logger.warning("识别失败: %s", result)
                continue
            if not result:
                continue  # 没识别出内容（静音/噪声），跳过
            # 优化：识别完先立刻出「原文字幕」，翻译完再更新为双语——
            # 让用户先看到英文（感知延迟=识别延迟），中文晚 1~2 秒补上。
            if self._on_text is not None:
                try:
                    self._on_text(result, "", utt)
                except Exception:  # noqa: BLE001
                    logger.exception("字幕回调异常")
            if self._translator is not None:
                tok, translated = self._translator.translate(result)
                if tok and translated:
                    if self._on_text is not None:
                        try:
                            self._on_text(result, translated, utt)
                        except Exception:  # noqa: BLE001
                            logger.exception("字幕回调异常")
                else:
                    logger.warning("翻译失败（保留原文显示）: %s", translated)
