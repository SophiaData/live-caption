"""翻译器：把原文翻成目标语言（OpenAI 兼容 chat/completions）。

设计：
- 与识别器一样走统一接口 Translator.translate(text, src, tgt) -> (ok, 译文|错误)
- 术语表与上下文窗口留给 V2（M1 先保证准确可用）
- 译文为空/失败时，调用方保留原文显示（老板裁决：翻译失败原文仍显示）
"""

from __future__ import annotations

import logging

from ..providers.openai_compatible import ModelConfig, OpenAICompatibleClient

logger = logging.getLogger(__name__)

# 语言代码 → 中文名（用于提示词）
LANG_NAMES = {
    "en": "英文",
    "zh": "中文",
    "ja": "日文",
    "ko": "韩文",
    "auto": "自动检测",
}


def lang_label(code: str) -> str:
    return LANG_NAMES.get(code, code)


class Translator:
    """字幕翻译器（OpenAI 兼容）。"""

    def __init__(
        self, config: ModelConfig, source_lang: str = "auto", target_lang: str = "zh"
    ) -> None:
        self._client = OpenAICompatibleClient(config)
        self._src = source_lang
        self._tgt = target_lang

    def _build_messages(self, text: str) -> list[dict[str, str]]:
        src = "原语言" if self._src == "auto" else lang_label(self._src)
        tgt = lang_label(self._tgt)
        return [
            {
                "role": "system",
                "content": (
                    f"你是专业的字幕翻译器。把用户给出的{src}字幕翻译成{tgt}。"
                    "只输出译文本身，不要解释、不要加引号、不要重复原文。"
                    "保持口语化和简洁，符合字幕阅读习惯。"
                ),
            },
            {"role": "user", "content": text},
        ]

    def translate(self, text: str) -> tuple[bool, str]:
        """翻译一句。返回 (成功否, 译文 或 人话错误)。"""
        if not text.strip():
            return True, ""  # 空文本直接返回空，不算失败
        try:
            resp = self._client._client.post(
                self._client._url("chat/completions"),
                headers={"Authorization": f"Bearer {self._client._cfg.api_key}"},
                json={
                    "model": self._client._cfg.model,
                    "messages": self._build_messages(text),
                    "temperature": 0.2,
                    "max_tokens": 500,
                },
            )
            if resp.status_code != 200:
                if resp.status_code == 401:
                    return False, "翻译服务密钥错误（401）"
                return False, f"翻译失败（{resp.status_code}）"
            data = resp.json()
            content = data["choices"][0]["message"].get("content") or ""
            return True, content.strip()
        except Exception as e:  # noqa: BLE001 - 统一人话化
            logger.exception("翻译异常")
            return False, f"翻译出错：{e}"
