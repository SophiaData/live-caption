"""断句器：把连续 16kHz 单声道音频切成「一句一句」的片段。

第一版用能量阈值 + 静音时长断句（零依赖）：
- 音频块能量高于阈值 → 正在说话
- 连续静音超过 min_silence_ms → 一句结束
- 句长上限 max_utterance_s：超了强制断（防止流式说话把一句拖太长）
- 句长下限 min_utterance_s：太短的（咳嗽等）丢弃

音乐背景重的场景会切不准——这是已知权衡（见 tech-debt TD-1），
后续观察效果再决定是否升级专业 VADs。

边说边出（Issue #17）：整句判定结束之前，可按更短的静音间隔（或最长时长兜底）
先吐出一个「草稿片段」（prefix of 当前句），让人还在说话时字幕就有内容。
参数集中在 DraftConfig，默认关闭——引擎层保持与 Issue #17 之前完全一致的行为，
产品层（设置面板 + 偏好）默认打开，详见 architecture.md 决策 11。
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
class DraftConfig:
    """边说边出（草稿字幕）的子窗口参数。

    enabled=False 时断句器行为与 Issue #17 之前逐字节一致（不产出任何草稿）。
    参数都是字段而不是代码里的魔法数，可由偏好/设置覆盖（Issue #17 设计约束）。
    """

    enabled: bool = False
    silence_ms: int = 150  # 句内更短的静音间隔 → 先出草稿（必须 < min_silence_ms）
    max_s: float = 1.2  # 连续说话没有停顿时的兜底：每说这么久强制补一次草稿
    min_s: float = 0.4  # 短于这个时长不值得调用识别（防咳嗽烧配额）
    max_per_utterance: int = 3  # 单句最多补几次草稿（成本上限，约多 2~4 倍调用）


@dataclass(frozen=True)
class Utterance:
    """切出的一句话（含原始采样与起止时间，单位秒）。

    utt_id / is_draft 为 Issue #17 新增：同一句的草稿与定稿共享 utt_id，
    显示层据此把草稿与定稿钉在同一行，并丢弃迟到的旧草稿。
    """

    samples: np.ndarray
    start_s: float
    end_s: float
    trailing_silence_s: float = 0.0  # 句尾用于确认「说完」的静音时长（延迟观测用，Issue #16）
    utt_id: int = 0  # 句标识（>0 由 Segmenter 递增分配；同一句的草稿与定稿同号）
    is_draft: bool = False  # True=句中子窗口草稿，False=整句定稿

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def speech_end_s(self) -> float:
        """真正「说完」的时刻（end_s 去掉尾部静音确认）。"""
        return max(self.end_s - self.trailing_silence_s, self.start_s)


class Segmenter:
    """增量式断句器：喂入 10ms 块，随时取出切完的整句与可选的草稿片段。

    用法：
        seg = Segmenter()
        seg.feed(block)  # 10ms 的 160 样本块
        ...
        for utt in seg.pop_utterances():
            print(utt.start_s, utt.end_s, utt.samples)
        for d in seg.pop_drafts():   # 仅 DraftConfig(enabled=True) 时会有
            print(d.duration_s, d.utt_id)
    """

    def __init__(
        self,
        config: SegmenterConfig | None = None,
        draft: DraftConfig | None = None,
    ) -> None:
        self._cfg = config or SegmenterConfig()
        self._draft: DraftConfig = draft or DraftConfig()
        self._draft_on: bool = self._draft.enabled  # 运行时开关（设置面板可即时切换）
        self._blocks: list[np.ndarray] = []  # 当前句的块
        self._cur_samples: int = 0  # 当前句累计样本数
        self._total_samples_seen = 0  # 全局累计样本数（算时间用）
        self._silence_samples: int = 0  # 句内连续静音样本数
        self._in_speech = False  # 当前是否在说话
        self._completed: list[Utterance] = []  # 已断完等待取走的句
        self._drafts: list[Utterance] = []  # 待取走的草稿片段（同句只留最新）
        self._utt_seq: int = 0  # 当前句的 utt_id（说话开始时 +1）
        self._draft_count: int = 0  # 本句已吐出的草稿次数（成本上限）
        self._last_draft_s: float = 0.0  # 本句内上次吐草稿时的已说时长
        self._draft_silence_fired = False  # 本轮静音是否已触发过草稿

    # ---------- 配置访问（供管道与观测层读取） ----------

    @property
    def config(self) -> SegmenterConfig:
        return self._cfg

    @property
    def draft(self) -> DraftConfig:
        return self._draft

    @property
    def draft_enabled(self) -> bool:
        """当前是否产出草稿片段（配置与运行时开关的与）。"""
        return self._draft.enabled and self._draft_on

    def set_draft_enabled(self, on: bool) -> None:
        """设置面板即时切换「边说边出」；关闭时清掉未取走的草稿，避免旧片段上屏。"""
        self._draft_on = on
        if not on:
            self._drafts = []

    def feed(self, block: np.ndarray) -> None:
        """喂入一块 float32 单声道音频（大小任意，按样本数算时长）。"""
        n = len(block)
        energy = float(np.abs(block).max())
        is_speech = energy > self._cfg.energy_threshold

        if not self._in_speech:
            if is_speech:
                self._in_speech = True
                self._utt_seq += 1  # 新句开始：草稿与定稿共用这个号
                self._cur_samples = n
                self._silence_samples = 0
                self._blocks = [block]
                self._reset_draft_state()
            # 非说话时静默丢弃
        else:
            self._blocks.append(block)
            self._cur_samples += n
            if is_speech:
                self._silence_samples = 0
                self._draft_silence_fired = False  # 停顿被打断，可以重新触发草稿
            else:
                self._silence_samples += n
            # 断句：静音够了 或 句太长了（都按样本数换算）
            silence_s = self._silence_samples / SAMPLE_RATE
            cur_s = self._cur_samples / SAMPLE_RATE
            if silence_s >= self._cfg.min_silence_ms / 1000 or cur_s >= self._cfg.max_utterance_s:
                self._finalize_utterance()
            else:
                self._maybe_emit_draft(silence_s, cur_s)

        self._total_samples_seen += n

    def pop_utterances(self) -> list[Utterance]:
        """取出当前已断的整句（返回后清空完成队列）。"""
        result = self._completed
        self._completed = []
        return result

    def pop_drafts(self) -> list[Utterance]:
        """取出待显示的草稿片段（返回后清空；关闭草稿时恒为空）。"""
        result = self._drafts
        self._drafts = []
        return result

    def flush(self) -> list[Utterance]:
        """流结束时强制切出当前句（若正在说话且够长），并取出全部完成句。"""
        if self._in_speech and self._blocks:
            self._finalize_utterance()
        self._drafts = []  # 流末未定稿的草稿不再有意义
        return self.pop_utterances()

    # ---------- 内部 ----------

    def _reset_draft_state(self) -> None:
        self._draft_count = 0
        self._last_draft_s = 0.0
        self._draft_silence_fired = False

    def _maybe_emit_draft(self, silence_s: float, cur_s: float) -> None:
        """句内子窗口：够条件就先用「已说到的这一段」去识别（Issue #17）。

        触发条件（任一）：
        1) 出现 silence_ms 这么长的停顿（比整句判定更短）——说话人顿了一下；
        2) 距上次草稿已连续说了 max_s 还没停顿——长句也要边说边补。
        约束：本句已说时长 >= min_s（太短不值得调用）、单句最多 max_per_utterance 次。
        """
        d = self._draft
        if not self.draft_enabled:
            return
        if cur_s < d.min_s or self._draft_count >= d.max_per_utterance:
            return
        by_silence = silence_s >= d.silence_ms / 1000 and not self._draft_silence_fired
        by_timeout = cur_s - self._last_draft_s >= d.max_s
        if not (by_silence or by_timeout):
            return
        if by_silence:
            self._draft_silence_fired = True
        self._draft_count += 1
        self._last_draft_s = cur_s
        self._drafts.append(self._snapshot_draft(cur_s))

    def _snapshot_draft(self, cur_s: float) -> Utterance:
        """把当前句已缓冲的采样复制成一条草稿片段。"""
        samples = np.concatenate(self._blocks).reshape(-1)
        end_s = (self._total_samples_seen + len(self._blocks[-1])) / SAMPLE_RATE
        return Utterance(
            samples=samples,
            start_s=end_s - cur_s,
            end_s=end_s,
            trailing_silence_s=0.0,  # 草稿没有「说完」确认，不参与整句延迟锚点
            utt_id=self._utt_seq,
            is_draft=True,
        )

    def _finalize_utterance(self) -> None:
        """把当前缓冲的块拼成一句话并存入完成队列；太短则丢弃。"""
        samples = np.concatenate(self._blocks).reshape(-1)
        duration = len(samples) / SAMPLE_RATE
        # 结束时间：本块已加入 _cur_samples，但 _total_samples_seen 尚未累加本块，
        # 所以 end = (total_seen + 本块样本) / SAMPLE_RATE
        end_s = (self._total_samples_seen + len(self._blocks[-1])) / SAMPLE_RATE
        start_s = end_s - duration
        trailing_silence_s = self._silence_samples / SAMPLE_RATE  # 打点须在复位前取
        utt_id = self._utt_seq
        self._blocks = []
        self._cur_samples = 0
        self._in_speech = False
        self._silence_samples = 0
        self._reset_draft_state()
        if duration >= self._cfg.min_utterance_s:
            self._completed.append(
                Utterance(
                    samples=samples,
                    start_s=start_s,
                    end_s=end_s,
                    trailing_silence_s=trailing_silence_s,
                    utt_id=utt_id,
                )
            )
            self._drafts = [d for d in self._drafts if d.utt_id != utt_id]  # 定稿已就位
        # 太短（咳嗽等）静默丢弃
