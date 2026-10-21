"""功能 #6 测试：翻译器 + 双语字幕管道。"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.pipeline.asr_pipeline import RecognitionPipeline
from app.pipeline.recognizer import Recognizer
from app.pipeline.translator import Translator, lang_label
from app.providers.openai_compatible import ModelConfig

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 160


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
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


class FakeRecognizer(Recognizer):
    def __init__(self, text: str = "hello world") -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> tuple[bool, str]:
        self.calls += 1
        return True, self.text


class FakeTranslator:
    def __init__(self, translation: str = "你好世界", fail: bool = False) -> None:
        self.translation = translation
        self.fail = fail
        self.calls: list[str] = []

    def translate(self, text: str) -> tuple[bool, str]:
        self.calls.append(text)
        if self.fail:
            return False, "翻译失败"
        return True, self.translation


def make_speech(n: int, amp: float = 0.5) -> list[np.ndarray]:
    rng = np.random.default_rng(0)
    return [(amp * rng.standard_normal(BLOCK_SAMPLES)).astype(np.float32) for _ in range(n)]


def make_silence(n: int) -> list[np.ndarray]:
    return [np.zeros(BLOCK_SAMPLES, dtype=np.float32) for _ in range(n)]


class TestTranslatorConfig:
    def test_lang_label(self) -> None:
        assert lang_label("zh") == "中文"
        assert lang_label("en") == "英文"
        assert lang_label("xx") == "xx"  # 未知原样返回

    def test_build_messages_contains_languages(self) -> None:
        cfg = ModelConfig(base_url="https://api.test/v1", api_key="sk-x", model="m")
        t = Translator(cfg, source_lang="en", target_lang="zh")
        msgs = t._build_messages("hello")
        assert any("中文" in m["content"] for m in msgs)
        assert any("英文" in m["content"] for m in msgs)

    def test_empty_text_returns_empty_ok(self) -> None:
        cfg = ModelConfig(base_url="https://api.test/v1", api_key="sk-x", model="m")
        t = Translator(cfg)
        ok, out = t.translate("   ")
        assert ok is True
        assert out == ""

    def test_translate_success(self) -> None:
        cfg = ModelConfig(base_url="https://api.test/v1", api_key="sk-x", model="m")
        t = Translator(cfg)
        with patch.object(t._client._client, "post") as mock_post:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"choices": [{"message": {"content": "你好世界"}}]}
            mock_post.return_value = resp
            ok, out = t.translate("hello world")
        assert ok is True
        assert out == "你好世界"

    def test_translate_401(self) -> None:
        cfg = ModelConfig(base_url="https://api.test/v1", api_key="bad", model="m")
        t = Translator(cfg)
        with patch.object(t._client._client, "post") as mock_post:
            resp = MagicMock()
            resp.status_code = 401
            mock_post.return_value = resp
            ok, out = t.translate("hello")
        assert ok is False
        assert "密钥" in out or "401" in out


class TestBilingualPipeline:
    def test_original_and_translation_both_delivered(self) -> None:
        """识别完先出原文(译文空)，翻译完再更新双语 —— 共两次回调，最后一次带译文。"""
        pipe = RecognitionPipeline(FakeRecognizer("hello"), FakeTranslator("你好"))
        got: list[tuple[str, str]] = []
        pipe.set_on_text(lambda orig, trans, u: got.append((orig, trans)))
        pipe.start()
        for b in make_speech(50) + make_silence(50):
            pipe.feed_block(b)
        import time

        time.sleep(0.4)
        pipe.stop()
        # 先 (原文,"") 后 (原文,译文)
        assert got[0] == ("hello", "")
        assert got[-1] == ("hello", "你好")
        assert ("hello", "你好") in got

    def test_translation_failure_still_shows_original(self) -> None:
        """翻译失败时原文照常显示（译文为空字符串）。"""
        pipe = RecognitionPipeline(FakeRecognizer("hello"), FakeTranslator(fail=True))
        got: list[tuple[str, str]] = []
        pipe.set_on_text(lambda orig, trans, u: got.append((orig, trans)))
        pipe.start()
        for b in make_speech(50) + make_silence(50):
            pipe.feed_block(b)
        import time

        time.sleep(0.3)
        pipe.stop()
        # 先出原文 (hello,"")，翻译失败后不再更新 → 最后一条仍是 (hello,"")
        assert got[0][0] == "hello"
        assert got[-1][1] == ""  # 译文为空（显示层只显原文）

    def test_no_translator_works(self) -> None:
        """没配翻译器（None）时只出原文，不崩。"""
        pipe = RecognitionPipeline(FakeRecognizer("hi"), None)
        got: list[tuple[str, str]] = []
        pipe.set_on_text(lambda orig, trans, u: got.append((orig, trans)))
        pipe.start()
        for b in make_speech(50) + make_silence(50):
            pipe.feed_block(b)
        import time

        time.sleep(0.3)
        pipe.stop()
        assert got == [("hi", "")]
