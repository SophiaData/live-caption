"""OpenAI 兼容协议客户端：支持 chat/completions（翻译）与 audio/transcriptions（识别）。

设计：
- 填 base_url + api_key + model 即可用，覆盖 DeepSeek/Kimi/通义/中转站/Ollama
- 翻译走流式响应（SSE）；识别走 multipart/form-data 上传音频文件
- 所有请求可取消、有超时、禁无界重试（硬约束）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """模型服务调用失败，message 为给用户的人话。"""


@dataclass(frozen=True)
class ModelConfig:
    """一个模型服务的配置（不持久化密钥，密钥走钥匙串）。"""

    base_url: str  # 如 "https://api.deepseek.com/v1"
    api_key: str  # 从钥匙串读取，不存配置文件
    model: str  # 如 "deepseek-chat" / "whisper-1"
    timeout_s: float = 30.0


class OpenAICompatibleClient:
    """OpenAI 兼容 API 客户端（识别 + 翻译双槽位共用）。"""

    def __init__(self, config: ModelConfig) -> None:
        self._cfg = config
        # trust_env=True 会让 httpx 读取全局代理变量；某些环境（如 NO_PROXY 含 [::1]）
        # 会触发 httpx 0.28 的 URL 解析 bug。这里显式开启 trust_env（用户可能确实需要代理），
        # 但把 base_url 去掉、改为每次请求拼完整 URL，避免构造期解析异常。
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=config.timeout_s,
            trust_env=True,
        )

    def _url(self, path: str) -> str:
        return self._cfg.base_url.rstrip("/") + "/" + path.lstrip("/")

    def close(self) -> None:
        self._client.close()

    def test_connection(self, mode: str = "chat") -> tuple[bool, str]:
        """测试连接：返回 (成功否, 人话消息)。mode: "chat" 或 "audio"。"""
        try:
            if mode == "chat":
                # 发一条极短消息验证可达 + 鉴权
                resp = self._client.post(
                    self._url("chat/completions"),
                    json={
                        "model": self._cfg.model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    },
                )
            else:  # audio
                # 发 1 秒静音 wav 验证可达 + 鉴权
                import io
                import struct
                import wave

                buf = io.BytesIO()
                with wave.open(buf, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(struct.pack("<16000h", *([0] * 16000)))
                buf.seek(0)
                files = {"file": ("silence.wav", buf, "audio/wav")}
                resp = self._client.post(
                    self._url("audio/transcriptions"),
                    files=files,
                    data={"model": self._cfg.model},
                )

            if resp.status_code == 200:
                return True, "连接成功，模型响应正常。"
            if resp.status_code == 401:
                return False, "密钥错误（401）：请检查 API Key 是否填对。"
            if resp.status_code == 404:
                return False, f"模型不存在（404）：模型名「{self._cfg.model}」可能不对。"
            if resp.status_code >= 500:
                return False, f"服务端故障（{resp.status_code}）：模型服务商暂时不可用，稍后重试。"
            return False, f"意外响应（{resp.status_code}）：{resp.text[:100]}"
        except httpx.TimeoutException:
            return False, "连接超时：网络不通或地址填错，检查网址和网络。"
        except httpx.ConnectError:
            return False, f"连不上地址「{self._cfg.base_url}」：检查网址和网络。"
        except Exception as e:  # noqa: BLE001 - 统一人话化
            return False, f"意外错误：{e}"

    def translate_stream(self, text: str, source_lang: str, target_lang: str) -> tuple[bool, str]:
        """流式翻译：返回 (成功否, 译文)。同步阻塞，调用方注意放后台线程。"""
        try:
            with self._client.stream(
                "POST",
                self._url("chat/completions"),
                json={
                    "model": self._cfg.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                f"你是字幕翻译器，把用户给的{text}从{source_lang}"
                                f"翻译成{target_lang}，只输出译文不要解释。"
                            ),
                        },
                        {"role": "user", "content": text},
                    ],
                    "stream": True,
                },
            ) as resp:
                if resp.status_code != 200:
                    return False, f"翻译失败（{resp.status_code}）"
                parts: list[str] = []
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        import json

                        try:
                            chunk = json.loads(payload)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                parts.append(delta)
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                return True, "".join(parts)
        except httpx.TimeoutException:
            return False, "翻译超时"
        except Exception as e:  # noqa: BLE001
            return False, f"翻译出错：{e}"

    def transcribe(self, audio_samples: bytes, sample_rate: int = 16000) -> tuple[bool, str]:
        """识别音频：返回 (成功否, 原文文本)。同步阻塞。"""
        try:
            import io
            import struct
            import wave

            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                # 16-bit PCM
                int16 = [
                    int(max(-1.0, min(1.0, s)) * 32767)
                    for s in struct.unpack(f"<{len(audio_samples) // 4}f", audio_samples)
                ]
                wav.writeframes(struct.pack(f"<{len(int16)}h", *int16))
            buf.seek(0)
            files = {"file": ("speech.wav", buf, "audio/wav")}
            resp = self._client.post(
                self._url("audio/transcriptions"),
                files=files,
                data={"model": self._cfg.model, "response_format": "text"},
            )
            if resp.status_code == 200:
                return True, resp.text.strip()
            if resp.status_code == 401:
                return False, "密钥错误"
            return False, f"识别失败（{resp.status_code}）"
        except httpx.TimeoutException:
            return False, "识别超时"
        except Exception as e:  # noqa: BLE001
            return False, f"识别出错：{e}"
