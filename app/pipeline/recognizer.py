"""识别器：把一段音频转成文字。

两个实现：
- CloudRecognizer：OpenAI 兼容 /audio/transcriptions（硅基流动、OpenAI、Groq 等）
- LocalRecognizer：本地 faster-whisper（零注册、离线、可选）

统一接口 Recognizer.transcribe(samples, sample_rate) -> (ok, text|err)
"""

from __future__ import annotations

import io
import logging
import wave
from abc import ABC, abstractmethod

import numpy as np

from ..providers.openai_compatible import ModelConfig, OpenAICompatibleClient

logger = logging.getLogger(__name__)


def samples_to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """float32 单声道 [-1,1] → 16-bit PCM WAV 字节。纯逻辑，可测。"""
    clipped = np.clip(samples, -1.0, 1.0)
    int16 = (clipped * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(int16.tobytes())
    return buf.getvalue()


class Recognizer(ABC):
    """识别器统一接口。"""

    @abstractmethod
    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> tuple[bool, str]:
        """把音频转文字。返回 (成功否, 文本 或 人话错误)。"""


class CloudRecognizer(Recognizer):
    """云端识别：OpenAI 兼容 /audio/transcriptions。"""

    def __init__(self, config: ModelConfig) -> None:
        self._cfg = config
        self._client = OpenAICompatibleClient(config)

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> tuple[bool, str]:
        wav_bytes = samples_to_wav_bytes(samples, sample_rate)
        try:
            files = {"file": ("speech.wav", wav_bytes, "audio/wav")}
            resp = self._client._client.post(
                self._client._url("audio/transcriptions"),
                headers={"Authorization": f"Bearer {self._cfg.api_key}"},
                files=files,
                data={"model": self._cfg.model},
            )
            if resp.status_code == 200:
                # 兼容两种返回：{"text": "..."} 或纯文本
                try:
                    data = resp.json()
                    text = str(data.get("text", "")).strip()
                except Exception:  # noqa: BLE001 - 非 JSON 就当纯文本
                    text = resp.text.strip()
                return True, text
            if resp.status_code == 401:
                return False, "识别服务密钥错误（401）：请检查密钥是否填对。"
            if resp.status_code == 404:
                return False, f"识别模型不存在（404）：模型名「{self._cfg.model}」可能不对。"
            return False, f"识别失败（{resp.status_code}）"
        except Exception as e:  # noqa: BLE001 - 统一人话化
            logger.exception("云端识别异常")
            return False, f"识别出错：{e}（检查网络或地址）"


class LocalRecognizer(Recognizer):
    """本地识别：faster-whisper（首次调用时下载/加载模型，之后离线）。"""

    def __init__(self, model_size: str = "small", compute_type: str = "int8") -> None:
        self._model_size = model_size
        self._compute_type = compute_type
        self._model: object | None = None  # 延迟加载

    def _ensure_model(self) -> object:
        if self._model is None:
            from faster_whisper import WhisperModel  # 延迟导入，未装不影响其他功能

            logger.info("加载本地 Whisper 模型: %s (%s)", self._model_size, self._compute_type)
            self._model = WhisperModel(
                self._model_size, device="cpu", compute_type=self._compute_type
            )
        return self._model

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> tuple[bool, str]:
        try:
            model = self._ensure_model()
            # faster-whisper 接受 float32 单声道 numpy（16k）
            audio = np.clip(samples.reshape(-1), -1.0, 1.0).astype(np.float32)
            segments, info = model.transcribe(  # type: ignore[attr-defined]
                audio, language=None, beam_size=1, vad_filter=True
            )
            text = "".join(seg.text for seg in segments).strip()
            return True, text
        except Exception as e:  # noqa: BLE001
            logger.exception("本地识别异常")
            return False, f"本地识别出错：{e}"
