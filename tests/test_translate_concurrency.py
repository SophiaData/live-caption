"""翻译并发（2026-11-05：老板反馈速度慢）。

识别必须串行，但翻译彼此独立。连续说话时并发翻能显著缩短总等待：
实测 3 句串行 4.62s → 并发 2.36s。这里钉住两条关键行为：
1. 有翻译池时，翻译不阻塞识别线程（原文先出、译文后补）；
2. 没有池时（基准/测试的同步路径），行为与改动前一致，不能被悄悄改掉。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

from app.pipeline.asr_pipeline import RecognitionPipeline
from app.pipeline.segmenter import SAMPLE_RATE


class _SlowTranslator:
    """每句固定睡 0.25s，模拟真实模型响应。"""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def translate(self, text: str) -> tuple[bool, str]:
        with self._lock:
            self.calls += 1
        time.sleep(0.25)
        return True, f"[zh]{text}"


class _InstantRecognizer:
    def transcribe(self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> tuple[bool, str]:
        return True, "hello"


def _pipeline(translator: Any) -> tuple[RecognitionPipeline, list[tuple[str, str]]]:
    shown: list[tuple[str, str]] = []
    shown_lock = threading.Lock()

    def on_text(orig: str, trans: str, utt: Any) -> None:
        with shown_lock:
            shown.append((orig, trans))

    pipe = RecognitionPipeline(recognizer=_InstantRecognizer(), translator=translator)
    pipe.set_on_text(on_text)
    return pipe, shown


def test_translation_runs_on_pool_and_original_shows_first() -> None:
    """有池时：原文立刻可见，译文稍后补上，且不阻塞后续识别。"""
    tr = _SlowTranslator()
    pipe, shown = _pipeline(tr)
    pipe._tr_pool = __import__("concurrent.futures", fromlist=["x"]).ThreadPoolExecutor(
        max_workers=3
    )
    try:
        from app.pipeline.segmenter import Utterance

        utt = Utterance(
            samples=np.zeros(1600, dtype=np.float32),
            start_s=0.0,
            end_s=0.1,
            utt_id=1,
        )
        pipe._process_utterance(utt)
        # 原文必须已经上屏，且此刻译文还没回来（翻译在跑）
        assert shown and shown[0][1] == ""
        assert tr.calls == 1
        time.sleep(0.6)
        assert any(t for _, t in shown), "译文最终必须补上"
    finally:
        pipe._tr_pool.shutdown(wait=True)


def test_no_pool_keeps_synchronous_behavior() -> None:
    """没有池时：保持原同步路径（基准与测试依赖它可复现）。"""
    tr = _SlowTranslator()
    pipe, shown = _pipeline(tr)
    pipe._tr_pool = None
    from app.pipeline.segmenter import Utterance

    utt = Utterance(
        samples=np.zeros(1600, dtype=np.float32),
        start_s=0.0,
        end_s=0.1,
        utt_id=1,
    )
    pipe._process_utterance(utt)
    # 同步路径：函数返回时译文已经在了
    assert shown[-1][1] == "[zh]hello"
