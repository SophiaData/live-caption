"""功能 #5 测试：识别管道（识别器抽象、WAV 转换、管道编排、背压）。"""

import io
import wave

import numpy as np
import pytest

from app.pipeline.asr_pipeline import RecognitionPipeline
from app.pipeline.recognizer import (
    CloudRecognizer,
    Recognizer,
    samples_to_wav_bytes,
)
from app.pipeline.segmenter import Utterance

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 160


class FakeRecognizer(Recognizer):
    """测试用假识别器：记录收到的音频并返回固定文本。"""

    def __init__(self, result: str = "hello world", fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.calls: list[int] = []

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> tuple[bool, str]:
        self.calls.append(len(samples))
        if self.fail:
            return False, "识别失败"
        return True, self.result


def make_speech_blocks(n_blocks: int, amplitude: float = 0.5) -> list[np.ndarray]:
    t = np.arange(n_blocks * BLOCK_SAMPLES) / SAMPLE_RATE
    tone = amplitude * np.sin(2 * np.pi * 440 * t)
    return [
        tone[i * BLOCK_SAMPLES : (i + 1) * BLOCK_SAMPLES].astype(np.float32)
        for i in range(n_blocks)
    ]


def make_silence(n_blocks: int) -> list[np.ndarray]:
    return [np.zeros(BLOCK_SAMPLES, dtype=np.float32) for _ in range(n_blocks)]


class TestWavConversion:
    def test_samples_to_wav_roundtrip(self) -> None:
        samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
        wav = samples_to_wav_bytes(samples, SAMPLE_RATE)
        with wave.open(io.BytesIO(wav), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getframerate() == SAMPLE_RATE
            assert w.getsampwidth() == 2
            assert w.getnframes() == 5

    def test_clipping_protection(self) -> None:
        """超出 [-1,1] 的值被裁剪，不崩。"""
        samples = np.array([2.0, -2.0, 0.5], dtype=np.float32)
        wav = samples_to_wav_bytes(samples, SAMPLE_RATE)  # 不抛异常即通过
        assert len(wav) > 44  # 有 WAV 头 + 数据


class TestPipeline:
    def test_feeds_speech_and_produces_text(self) -> None:
        """说话→静音→管道应调用识别器并回调文字。"""
        rec = FakeRecognizer("hello world")
        pipe = RecognitionPipeline(rec)
        texts: list[str] = []
        pipe.set_on_text(lambda t, tr, u: texts.append(t))
        pipe.start()

        for b in make_speech_blocks(50) + make_silence(50):
            pipe.feed_block(b)
        import time

        time.sleep(0.3)  # 等后台识别线程处理
        pipe.stop()

        assert len(rec.calls) == 1  # 切出 1 句送去识别
        assert texts == ["hello world"]

    def test_silence_produces_no_text(self) -> None:
        """纯静音不触发识别。"""
        rec = FakeRecognizer()
        pipe = RecognitionPipeline(rec)
        texts: list[str] = []
        pipe.set_on_text(lambda t, tr, u: texts.append(t))
        pipe.start()
        for b in make_silence(200):
            pipe.feed_block(b)
        pipe.stop()
        assert rec.calls == []
        assert texts == []

    def test_recognition_failure_skipped(self) -> None:
        """识别失败不回调、不崩。"""
        rec = FakeRecognizer(fail=True)
        pipe = RecognitionPipeline(rec)
        texts: list[str] = []
        pipe.set_on_text(lambda t, tr, u: texts.append(t))
        pipe.start()
        for b in make_speech_blocks(50) + make_silence(50):
            pipe.feed_block(b)
        pipe.stop()
        assert texts == []

    def test_flush_sends_remaining(self) -> None:
        """流结束 flush 把没断的音频也送识别。"""
        rec = FakeRecognizer("tail")
        pipe = RecognitionPipeline(rec)
        texts: list[str] = []
        pipe.set_on_text(lambda t, tr, u: texts.append(t))
        pipe.start()
        for b in make_speech_blocks(40):  # 说话但没静音
            pipe.feed_block(b)
        pipe.flush()
        import time

        time.sleep(0.3)
        pipe.stop()
        assert len(rec.calls) == 1

    def test_backpressure_drops_when_full(self) -> None:
        """队列满时丢弃并计数（背压）。"""
        # 用一个慢的假识别器难以构造，直接验证 _max_queue 上限逻辑：
        pipe = RecognitionPipeline(FakeRecognizer())
        # 手动塞满队列
        for _ in range(25):
            p = Utterance(samples=np.zeros(1600, dtype=np.float32), start_s=0.0, end_s=0.1)
            pipe._enqueue(p)
        assert pipe.dropped_utterances == 5  # 25 - 20 上限


class TestCloudRecognizerFormat:
    @pytest.fixture(autouse=True)
    def _no_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in (
            "http_proxy",
            "https_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "all_proxy",
            "ALL_PROXY",
            "no_proxy",
            "NO_PROXY",
        ):
            monkeypatch.delenv(v, raising=False)

    def test_parses_text_field(self) -> None:
        """识别接口返回 {"text": ...} 时正确取 text。"""
        from unittest.mock import MagicMock, patch

        from app.providers.openai_compatible import ModelConfig

        cfg = ModelConfig(base_url="https://api.test.com/v1", api_key="sk-x", model="whisper-1")
        rec = CloudRecognizer(cfg)
        with patch.object(rec._client._client, "post") as mock_post:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"text": "识别出的句子", "language": "English"}
            mock_post.return_value = resp
            ok, text = rec.transcribe(np.zeros(1600, dtype=np.float32))
        assert ok is True
        assert text == "识别出的句子"
