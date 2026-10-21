"""断句器：把连续 16kHz 单声道音频切成「一句一句」的片段。

第一版用能量阈值 + 静音时长断句（零依赖）：
- 音频块能量高于阈值 → 正在说话
- 连续静音超过 min_silence_ms → 一句结束
- 句长上限 max_utterance_s：超了强制断（防止流式说话把一句拖太长）
- 句长下限 min_utterance_s：太短的（咳嗽等）丢弃

音乐背景重的场景会切不准——这是已知权衡（见 tech-debt TD-1），
后续观察效果再决定是否升级专业 VAD。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


@dataclass(frozen=True)
class SegmenterConfig:
    """断句参数（可调的独立阈值）。"""

    energy_threshold: float = 0.015  # 能量门限（调高：避免背景音乐的持续低音量被当说话）
    min_silence_ms: int = 300  # 连续静音多久判为一句结束（调低：轻微停顿也能断）
    max_utterance_s: float = 3.0  # 一句话最长多久（字幕不宜超过 5 秒）
    min_utterance_s: float = 0.3  # 一句话最短多久（防咳嗽误触）


@dataclass(frozen=True)
class Utterance:
    """切出的一句话（含原始采样与起止时间，单位秒）。"""

    samples: np.ndarray
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class Segmenter:
    """增量式断句器：喂入 10ms 块，随时取出切完的整句。

    用法：
        seg = Segmenter()
        seg.feed(block)  # 10ms 的 160 样本块
        ...
        for utt in seg.pop_utterances():
            print(utt.start_s, utt.end_s, utt.samples)
    """

    def __init__(self, config: SegmenterConfig | None = None) -> None:
        self._cfg = config or SegmenterConfig()
        self._blocks: list[np.ndarray] = []  # 当前句的块
        self._cur_samples: int = 0  # 当前句累计样本数
        self._total_samples_seen = 0  # 全局累计样本数（算时间用）
        self._silence_samples: int = 0  # 句内连续静音样本数
        self._in_speech = False  # 当前是否在说话
        self._completed: list[Utterance] = []  # 已断完等待取走的句

    def feed(self, block: np.ndarray) -> None:
        """喂入一块 float32 单声道音频（大小任意，按样本数算时长）。"""
        n = len(block)
        energy = float(np.abs(block).max())
        is_speech = energy > self._cfg.energy_threshold

        if not self._in_speech:
            if is_speech:
                self._in_speech = True
                self._cur_samples = n
                self._silence_samples = 0
                self._blocks = [block]
            # 非说话时静默丢弃
        else:
            self._blocks.append(block)
            self._cur_samples += n
            if is_speech:
                self._silence_samples = 0
            else:
                self._silence_samples += n
            # 断句：静音够了 或 句太长了（都按样本数换算）
            silence_s = self._silence_samples / SAMPLE_RATE
            cur_s = self._cur_samples / SAMPLE_RATE
            if silence_s >= self._cfg.min_silence_ms / 1000 or cur_s >= self._cfg.max_utterance_s:
                self._finalize_utterance()

        self._total_samples_seen += n

    def pop_utterances(self) -> list[Utterance]:
        """取出当前已断的整句（返回后清空完成队列）。"""
        result = self._completed
        self._completed = []
        return result

    def _finalize_utterance(self) -> None:
        """把当前缓冲的块拼成一句话并存入完成队列；太短则丢弃。"""
        samples = np.concatenate(self._blocks).reshape(-1)
        duration = len(samples) / SAMPLE_RATE
        # 结束时间：本块已加入 _cur_samples，但 _total_samples_seen 尚未累加本块，
        # 所以 end = (total_seen + 本块样本) / SAMPLE_RATE
        end_s = (self._total_samples_seen + len(self._blocks[-1])) / SAMPLE_RATE
        start_s = end_s - duration
        self._blocks = []
        self._cur_samples = 0
        self._in_speech = False
        self._silence_samples = 0
        if duration >= self._cfg.min_utterance_s:
            self._completed.append(Utterance(samples=samples, start_s=start_s, end_s=end_s))
        # 太短（咳嗽等）静默丢弃

    def flush(self) -> list[Utterance]:
        """流结束时强制切出当前句（若正在说话且够长），并取出全部完成句。"""
        if self._in_speech and self._blocks:
            self._finalize_utterance()
        return self.pop_utterances()
