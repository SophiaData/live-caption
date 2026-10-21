"""功能 #3 测试：断句器（纯逻辑层）。"""

import numpy as np
import pytest

from app.pipeline.segmenter import Segmenter, SegmenterConfig

SAMPLE_RATE = 16000
BLOCK_MS = 10
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000  # 160


def make_speech_blocks(n_blocks: int, amplitude: float = 0.5) -> list[np.ndarray]:
    """生成 n 个有声音的块（正弦模拟）。"""
    t = np.arange(n_blocks * BLOCK_SAMPLES) / SAMPLE_RATE
    tone = amplitude * np.sin(2 * np.pi * 440 * t)
    return [
        tone[i * BLOCK_SAMPLES : (i + 1) * BLOCK_SAMPLES].astype(np.float32)
        for i in range(n_blocks)
    ]


def make_silence_blocks(n_blocks: int) -> list[np.ndarray]:
    """生成 n 个静音块。"""
    return [np.zeros(BLOCK_SAMPLES, dtype=np.float32) for _ in range(n_blocks)]


class TestSegmenter:
    def test_speech_then_silence_produces_one_utterance(self) -> None:
        """说话 1 秒 → 静音 0.5 秒 → 应切出一句话（断句点在静音满 min_silence_ms=300ms 处）。"""
        seg = Segmenter()
        blocks = make_speech_blocks(100) + make_silence_blocks(50)  # 1s 说话 + 0.5s 静音
        for b in blocks:
            seg.feed(b)
        utts = seg.pop_utterances()
        assert len(utts) == 1
        # 1s 说话 + 0.3s 静音尾巴（300ms 触发断句）
        assert utts[0].duration_s == pytest.approx(1.3, abs=0.02)
        assert len(utts[0].samples) == 130 * BLOCK_SAMPLES

    def test_two_sentences_separated(self) -> None:
        """说话 → 静音 → 说话 → 静音 → 应切出两句。"""
        seg = Segmenter()
        for b in make_speech_blocks(50):  # 0.5s 说话
            seg.feed(b)
        for b in make_silence_blocks(50):  # 0.5s 静音 → 断
            seg.feed(b)
        first = seg.pop_utterances()
        assert len(first) == 1
        for b in make_speech_blocks(30):  # 0.3s 说话
            seg.feed(b)
        for b in make_silence_blocks(50):  # 静音断
            seg.feed(b)
        second = seg.pop_utterances()
        assert len(second) == 1
        # 第二句起点应该在第一句结束之后
        assert second[0].start_s >= first[0].end_s

    def test_pure_silence_produces_nothing(self) -> None:
        """纯静音不该出句子。"""
        seg = Segmenter()
        for b in make_silence_blocks(200):
            seg.feed(b)
        assert seg.pop_utterances() == []

    def test_short_noise_below_min_duration_dropped(self) -> None:
        """咳嗽等太短的声音（< min_utterance_s）丢弃。"""
        cfg = SegmenterConfig(min_utterance_s=0.5, min_silence_ms=100)  # 静音 100ms 就断
        seg = Segmenter(cfg)
        for b in make_speech_blocks(10):  # 0.1s 咳嗽
            seg.feed(b)
        for b in make_silence_blocks(20):  # 0.2s 静音断（含 100ms 尾巴，总长 0.1+0.1=0.2 < 0.5）
            seg.feed(b)
        # 太短，pop 该是空，flush 也该是空
        assert seg.pop_utterances() == []
        assert seg.flush() == []

    def test_too_long_utterance_force_cut(self) -> None:
        """说话超过 max_utterance_s 强制断（防拖太长）。"""
        cfg = SegmenterConfig(max_utterance_s=1.0)
        seg = Segmenter(cfg)
        for b in make_speech_blocks(150):  # 1.5s 连续说话
            seg.feed(b)
        utts = seg.pop_utterances()
        assert len(utts) == 1
        assert utts[0].duration_s <= 1.0 + 0.02

    def test_flush_on_stream_end(self) -> None:
        """流结束时正在说话也能切出来。"""
        seg = Segmenter()
        for b in make_speech_blocks(80):  # 0.8s 说话，没静音
            seg.feed(b)
        utts = seg.flush()
        assert len(utts) == 1
        assert utts[0].duration_s == pytest.approx(0.8, abs=0.02)

    def test_timing_offset_accumulates(self) -> None:
        """句子的 start_s 应该是从流开始算的全局时间。"""
        seg = Segmenter()
        for b in make_silence_blocks(100):  # 1s 静音
            seg.feed(b)
        for b in make_speech_blocks(50):  # 说话 0.5s
            seg.feed(b)
        for b in make_silence_blocks(50):  # 断
            seg.feed(b)
        utts = seg.pop_utterances()
        assert len(utts) == 1
        assert utts[0].start_s == pytest.approx(1.0, abs=0.02)  # 1 秒后开始


class TestConfig:
    def test_custom_threshold(self) -> None:
        """阈值调低后更敏感（轻微声音也算说话）。"""
        cfg = SegmenterConfig(energy_threshold=0.005)
        seg = Segmenter(cfg)
        # 0.01 幅度（低于默认 0.01 阈值，高于自定义 0.005）
        for b in make_speech_blocks(50, amplitude=0.008):
            seg.feed(b)
        for b in make_silence_blocks(50):
            seg.feed(b)
        utts = seg.pop_utterances()
        assert len(utts) == 1  # 自定义阈值下切出来了

    def test_default_threshold_ignores_quiet_noise(self) -> None:
        """默认阈值忽略轻微环境噪声。"""
        seg = Segmenter()  # 默认 0.01
        for b in make_speech_blocks(50, amplitude=0.008):  # 轻微噪声
            seg.feed(b)
        assert seg.pop_utterances() == []
