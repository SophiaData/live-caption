"""识别语言参数（Issue：日语短句被猜错，老板实测反馈）。

以前完全没给云端识别传 language 字段，服务只能自己猜；日语这种短句很容易猜错。
这里钉住：指定语言必须出现在请求表单里，不指定时必须不带该字段。
"""

from __future__ import annotations

import sys
from typing import Any

import numpy as np
import pytest

from app.pipeline.recognizer import CloudRecognizer

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" and sys.platform != "linux", reason="跨平台无差异"
)


class _FakeResp:
    status_code = 200
    text = "こんにちは"

    def json(self) -> dict[str, Any]:
        return {"text": "こんにちは"}


class _FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResp:
        self.calls.append(kwargs)
        return _FakeResp()


def _make(language: str | None) -> tuple[CloudRecognizer, _FakeHTTP]:
    from app.config.store import StoredConfig  # noqa: F401  仅为类型参考

    class _Cfg:
        base_url = "https://example.invalid/v1"
        model = "whisper-1"
        api_key = "k"
        timeout_s = 30.0

    rec = CloudRecognizer(_Cfg(), language=language)  # type: ignore[arg-type]
    fake = _FakeHTTP()
    rec._client._client = fake  # type: ignore[attr-defined]
    return rec, fake


def test_language_is_sent_when_specified() -> None:
    rec, fake = _make("ja")
    ok, text = rec.transcribe(np.zeros(1600, dtype=np.float32))
    assert ok and text == "こんにちは"
    assert fake.calls[0]["data"]["language"] == "ja"


def test_language_omitted_when_auto() -> None:
    rec, fake = _make(None)
    rec.transcribe(np.zeros(1600, dtype=np.float32))
    assert "language" not in fake.calls[0]["data"]


def test_set_language_takes_effect_immediately() -> None:
    rec, fake = _make(None)
    rec.set_language("ja")
    rec.transcribe(np.zeros(1600, dtype=np.float32))
    assert fake.calls[0]["data"]["language"] == "ja"
