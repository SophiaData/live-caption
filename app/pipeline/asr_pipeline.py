"""识别管道：把「捕获 → 断句 → 识别 → 字幕」串起来。

设计：
- 采集线程（Qt 定时器或后台线程）不断从捕获器取音频块，喂断句器
- 断句器切出整句后，送后台识别线程（QThread），避免卡 UI
- 识别结果通过回调发到 UI 显示字幕
- 队列有界，慢识别不无限堆积（背压）

边说边出（Issue #17）：断句器可以在整句判定结束之前先吐出「草稿片段」。草稿走
单独一条队列，并遵守三条硬规则，保证它不把真实延迟（尤其排队）拖坏：
1. 定稿优先：**已入队**的整句永远比草稿先被取走——一句话的定稿不会排在任何草稿
   后面。消不掉的唯一代价是「正在识别中的那一条草稿」：Recognizer 没有取消接口，
   所以定稿最多多等一次识别调用（草稿的音频比整句短，这次调用通常也更短）；
   整句队列一旦有积压，草稿立刻不入队，积压不会被草稿加深。
2. 同一句只保留最新草稿：新草稿挤掉旧草稿，整句定稿到达则清掉本句待处理草稿；
3. 整句队列有积压（背压）时直接丢弃草稿，不加戏、不烧配额。
草稿只送识别，绝不送翻译（否则额度直接翻倍）；定稿一定覆盖草稿。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from .recognizer import Recognizer
from .segmenter import DraftConfig, Segmenter, SegmenterConfig, Utterance
from .trace import LatencyTracer
from .translator import Translator

logger = logging.getLogger(__name__)

_SETTLED_KEEP = 64  # 已定稿句号保留上限（防无限累积；超出后最旧的句号不再抑制草稿）


class RecognitionPipeline:
    """识别管道（线程安全，UI 无关）。

    用法：
        pipe = RecognitionPipeline(recognizer)
        pipe.set_on_text(callback)  # callback(原文, 译文, utt) 在后台线程被调
        pipe.start()
        pipe.feed_block(audio_block)  # 从捕获回调调用
        pipe.stop()

    观测（Issue #16）：可选传入 tracer=LatencyTracer()，管道只在生命周期
    节点记时钟读数（纯内存、非阻塞），不传则一行都不执行，行为不变。

    边说边出（Issue #17）：传入 draft=DraftConfig(enabled=True) 或
    set_draft_enabled(True) 才会产出草稿；默认关闭——关闭时队列、调用次数、
    打点全部与 Issue #17 之前一致。
    """

    def __init__(
        self,
        recognizer: Recognizer,
        translator: Translator | None = None,
        tracer: LatencyTracer | None = None,
        config: SegmenterConfig | None = None,
        draft: DraftConfig | None = None,
        max_draft_queue: int = 1,
    ) -> None:
        self._recognizer = recognizer
        self._translator = translator
        self._tracer = tracer
        self._segmenter = Segmenter(config, draft)
        self._on_text: Callable[[str, str, Utterance], None] | None = None
        self._queue: list[Utterance] = []
        self._draft_queue: deque[Utterance] = deque(maxlen=max(1, max_draft_queue))
        self._lock = threading.RLock()  # 可重入：入队判断里会再读已定稿集合
        self._worker: threading.Thread | None = None
        self._running = False
        self._max_queue = 20  # 背压：最多积压 20 句
        self._dropped = 0
        self._settled: deque[int] = deque(maxlen=_SETTLED_KEEP)  # 已定稿句号
        self._draft_calls = 0  # 草稿实际调用识别器的次数（成本可见）
        # 「这条草稿值不值得送」的判据：实测的一次识别耗时（滑动均值）。
        # 停顿触发的草稿只有 0.15s 左右的提前量，若一次识别比这更慢，它既换不来
        # 更早的字，又会让紧随其后的定稿多等一次调用（真服务实测会把排队拖坏）。
        self._rec_cost_ema: float | None = None  # 一次调用的滑动耗时（秒）
        self._rec_rate_ema: float | None = None  # 每秒音频要多少识别时间（比值）
        self._draft_skipped = 0  # 因为「来不及更早」而省掉的草稿数
        self._draft_dropped = 0  # 因背压/已定稿/被取代而丢弃的草稿数

    def set_on_text(self, cb: Callable[[str, str, Utterance], None]) -> None:
        """注册字幕回调 (原文, 译文, utt)；在后台线程调用，UI 侧用信号转发。"""
        self._on_text = cb

    # ---------- 边说边出开关（Issue #17） ----------

    @property
    def draft_enabled(self) -> bool:
        return self._segmenter.draft_enabled

    def set_draft_enabled(self, on: bool) -> None:
        """开/关「边说边出」；关掉后立刻不再产出草稿，行为退回「说完再出字幕」。"""
        self._segmenter.set_draft_enabled(on)
        if not on:
            with self._lock:
                self._draft_queue.clear()

    @property
    def draft_calls(self) -> int:
        """草稿真的送去识别的次数（开关关闭时恒为 0）。"""
        return self._draft_calls

    @property
    def draft_dropped(self) -> int:
        """被丢弃的草稿数（背压保护 + 定稿覆盖 + 同句只留最新）。"""
        return self._draft_dropped

    @property
    def dropped_utterances(self) -> int:
        """因积压满而丢弃的句数（背压指标）。"""
        return self._dropped

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._work_loop, daemon=True, name="asr-worker")
        self._worker.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._queue.clear()
            self._draft_queue.clear()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None

    def feed_block(self, block: np.ndarray) -> None:
        """喂音频块（从捕获回调线程调用，必须非阻塞）。"""
        self._segmenter.feed(block)
        for utt in self._segmenter.pop_utterances():
            self._enqueue(utt)
        for draft in self._segmenter.pop_drafts():
            self._enqueue_draft(draft)

    def flush(self) -> None:
        """流结束时把剩余未断的音频也送去识别。"""
        for utt in self._segmenter.flush():
            self._enqueue(utt)

    # ---------- 入队 ----------

    def _enqueue(self, utt: Utterance) -> None:
        with self._lock:
            if len(self._queue) >= self._max_queue:
                self._dropped += 1
                logger.warning("识别队列已满，丢弃一句（背压）")
                return
            self._queue.append(utt)
            # 定稿一定覆盖草稿：本句还没处理的草稿直接作废（省一次调用）
            if utt.utt_id:
                kept = [d for d in self._draft_queue if d.utt_id != utt.utt_id]
                self._draft_dropped += len(self._draft_queue) - len(kept)
                self._draft_queue = deque(kept, maxlen=self._draft_queue.maxlen)
        if self._tracer is not None:
            self._tracer.mark("split", utt)

    def _enqueue_draft(self, draft: Utterance) -> None:
        """草稿入队：整句队列有积压就不入（保护排队），同句只留最新一条。"""
        if not self.draft_enabled:  # 已关掉：一条都不再往里塞
            return
        with self._lock:
            if self._queue or self._is_settled(draft.utt_id):
                self._draft_dropped += 1
                return
            if not self._draft_worth_it(draft):
                self._draft_skipped += 1
                return
            if self._draft_queue and self._draft_queue[-1].utt_id == draft.utt_id:
                self._draft_queue.pop()  # 同一句：新草稿取代旧草稿
                self._draft_dropped += 1
            self._draft_queue.append(draft)

    def _timed_transcribe(self, samples: np.ndarray) -> tuple[bool, str]:
        """调识别器，并记下「每秒音频要花多少识别时间」的滑动比值。

        比值（而不是绝对耗时）才能拿来预判一条新草稿：草稿音频比整句短得多，
        绝对均值会把「整句要 1.3s」错当成「草稿也要 1.3s」。
        """
        t0 = time.monotonic()
        ok, result = self._recognizer.transcribe(samples, 16000)
        cost = time.monotonic() - t0
        audio_s = max(len(samples) / 16000.0, 1e-3)
        with self._lock:
            self._rec_cost_ema = (
                cost if self._rec_cost_ema is None else 0.6 * self._rec_cost_ema + 0.4 * cost
            )
            rate = cost / audio_s
            self._rec_rate_ema = (
                rate if self._rec_rate_ema is None else 0.6 * self._rec_rate_ema + 0.4 * rate
            )
        return ok, result

    def _draft_worth_it(self, utt: Utterance) -> bool:
        """草稿只有在「预计能比定稿更早出现」时才送识别。

        预计耗时 = 实测的每秒音频识别比值 × 这条草稿自己的音频长度（草稿已被
        `max_window_s` 限住长度，所以占用有上界）；预计比它的提前量还慢，就注定换不来
        更早的字，只会让紧随的定稿多排队 → 直接省掉。没测过时先放一条探针学会比值。
        """
        if utt.draft_trigger != "silence":
            return True  # 连续说话的提前量大（到强制切分才定稿），照旧送
        with self._lock:
            rate = self._rec_rate_ema
        if rate is None:
            return True  # 第一条探针：让它跑一次，之后就学会了
        est_s = rate * max(len(utt.samples) / 16000.0, 1e-3)
        return utt.draft_lead_s >= est_s

    @property
    def draft_skipped(self) -> int:
        """因「来不及比定稿更早」而省掉的草稿数（既省额度，也保护排队）。"""
        return self._draft_skipped

    @property
    def last_rec_cost_s(self) -> float | None:
        """一次识别的滑动耗时（秒）；没调用过就是 None。"""
        with self._lock:
            return self._rec_cost_ema

    @property
    def last_rec_rate(self) -> float | None:
        """「每秒音频要花多少识别时间」的滑动比值；没调用过就是 None。"""
        with self._lock:
            return self._rec_rate_ema

    def _is_settled(self, utt_id: int) -> bool:
        """这句是否已经定稿上屏（读锁内：worker 与音频线程会并发增删这个队列）。"""
        if not utt_id:
            return False
        with self._lock:
            return utt_id in self._settled

    # ---------- 后台 worker ----------

    def _take(self) -> Utterance | None:
        """取下一条要处理的句子：整句优先，草稿只在整句队列空时才拿（定稿不等草稿）。"""
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
            if self._draft_queue:
                return self._draft_queue.popleft()
        return None

    def _work_loop(self) -> None:
        while self._running:
            if not self.step():
                time.sleep(0.05)  # 空转小睡，避免忙等

    def step(self) -> bool:
        """同步处理一条队列项（基准/测试用：不依赖真实时间，结果可复现）。

        与后台 worker 走同一套取项与处理逻辑：整句永远优先于草稿。
        返回是否真的处理了一项。
        """
        utt = self._take()
        if utt is None:
            return False
        if utt.is_draft:
            self._process_draft(utt)
        else:
            self._process_utterance(utt)
        return True

    def _process_draft(self, utt: Utterance) -> None:
        """草稿：只送识别、不送翻译；定稿已上屏则作废（不能让它盖掉定稿）。"""
        if self._is_settled(utt.utt_id) or not self.draft_enabled:
            with self._lock:
                self._draft_dropped += 1
            return
        ok, result = self._timed_transcribe(utt.samples)
        if not ok or not result:
            return  # 草稿失败/为空：静默，不打扰用户
        # 识别这一下的工夫里用户可能已经关掉开关，或这句已定稿：都不能再上屏
        if self._is_settled(utt.utt_id) or not self.draft_enabled:
            with self._lock:
                self._draft_dropped += 1
            return
        with self._lock:
            self._draft_calls += 1
        if self._on_text is not None:
            try:
                self._on_text(result, "", utt)
            except Exception:  # noqa: BLE001
                logger.exception("字幕回调异常")
        if self._tracer is not None:
            self._tracer.note_draft(utt)

    def _process_utterance(self, utt: Utterance) -> None:
        """定稿：整句识别 → 原文先上屏 → 翻译后补双语（Issue #5/#6 原路径）。"""
        if self._tracer is not None:
            self._tracer.mark("rec_start", utt)
        ok, result = self._timed_transcribe(utt.samples)
        if self._tracer is not None:
            self._tracer.mark("rec_end", utt)
        if not ok:
            logger.warning("识别失败: %s", result)
            return
        if not result:
            return  # 没识别出内容（静音/噪声），跳过
        if utt.utt_id:
            with self._lock:
                self._settled.append(utt.utt_id)
        # 优化：识别完先立刻出「原文字幕」，翻译完再更新为双语——
        # 让用户先看到英文（感知延迟=识别延迟），中文晚 1~2 秒补上。
        if self._on_text is not None:
            try:
                self._on_text(result, "", utt)
            except Exception:  # noqa: BLE001
                logger.exception("字幕回调异常")
        if self._tracer is not None:
            self._tracer.mark("orig_shown", utt)
        if self._translator is not None:
            if self._tracer is not None:
                self._tracer.mark("tr_start", utt)
            tok, translated = self._translator.translate(result)
            if self._tracer is not None:
                self._tracer.mark("tr_end", utt)
            if tok and translated:
                if self._on_text is not None:
                    try:
                        self._on_text(result, translated, utt)
                    except Exception:  # noqa: BLE001
                        logger.exception("字幕回调异常")
            else:
                logger.warning("翻译失败（保留原文显示）: %s", translated)
        if self._tracer is not None:
            # 「上屏」终点 = 这句最后一次字幕回调执行完毕（交给显示层）
            self._tracer.mark("shown", utt)
