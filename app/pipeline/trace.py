"""端到端字幕延迟的分阶段时间戳（Issue #16：纯观测注入缝）。

设计（见 architecture.md 决策 9/10）：
- LatencyTracer 只往内存里记时钟读数，不做任何 I/O，不改线程模型；
- 时钟可注入：真实/在线测量用 time.monotonic；确定性基准用 FakeClock，
  由假识别/假翻译按注入的固定耗时「拨表」，全程无真实等待、结果可复现；
- 「说完这句」的锚点 = 断句打点时刻 - utt.trailing_silence_s：确认句尾的
  静音是按实时到达的，所以本句的分段确认延迟不受跨句喂入节奏影响；
- 不传 tracer（生产默认路径）时，管道一行打点都不执行，行为与原来完全一致。

分段定义（五段相加 = 「说完 → 字幕出现」总延迟）：
- 分段 detect：声音说完进入捕获 → 断句器判定这句结束（含静音确认时间）
- 排队 queue：判定结束 → 真正交给识别器（背压/轮询等待）
- 识别 rec：交给识别器 → 识别返回文本
- 翻译 trans：识别返回 → 译文返回（含先上原文的回调耗时）
- 上屏 shown：译文返回 → 字幕回调执行完毕（交给显示层）
"""

from __future__ import annotations

import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .segmenter import Utterance

#: 生命周期打点顺序（split 必须最先打，序号按它分配）
STAGES = ("split", "rec_start", "rec_end", "orig_shown", "tr_start", "tr_end", "shown")


@dataclass
class UtteranceTrace:
    """一句字幕的分阶段时间戳（时钟读数，单位秒）。

    只留折算所需数值（不持有音频样本），长时间挂着观测器跑也不会越攒越多。
    """

    index: int  # 句序号，从 1 开始（按断句先后）
    trailing_silence_s: float  # 句尾静音确认时长（「说完」锚点用）
    times: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class LatencyBreakdown:
    """一句的分段延迟（秒），detect+queue+rec+trans+shown == total。"""

    index: int
    detect_s: float
    queue_s: float
    rec_s: float
    trans_s: float
    shown_s: float
    total_s: float


def breakdown(tr: UtteranceTrace) -> LatencyBreakdown | None:
    """把一条 trace 折算成分段延迟；打点不全（句子被丢弃/未走完）返回 None。

    「说完」锚点取 split 读数回退 trailing_silence_s：断句确认句尾的静音是按
    实时到达的（捕获回调或基准的每块 10ms 拨表），所以这句自己的确认延迟
    不受「处理前一句时喂入暂停」之类的跨句因素影响。
    """
    t = tr.times
    if "split" not in t or "rec_end" not in t or "shown" not in t:
        return None
    rec_start = t.get("rec_start", t["split"])
    detect = tr.trailing_silence_s
    speech_done = t["split"] - detect
    queue = rec_start - t["split"]
    rec = t["rec_end"] - rec_start
    if "tr_start" in t and "tr_end" in t:
        trans = t["tr_end"] - t["rec_end"]
        shown = t["shown"] - t["tr_end"]
    else:
        trans = 0.0
        shown = t["shown"] - t["rec_end"]
    return LatencyBreakdown(
        index=tr.index,
        detect_s=detect,
        queue_s=queue,
        rec_s=rec,
        trans_s=trans,
        shown_s=shown,
        total_s=t["shown"] - speech_done,
    )


class FakeClock:
    """手动推进的虚拟时钟：确定性基准与测试用，真实时间零消耗。"""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, dt: float) -> None:
        if dt < 0:
            raise ValueError("时钟只能向前走")
        with self._lock:
            self._t += dt


class LatencyTracer:
    """分阶段时间戳收集器：纯内存、线程安全、非阻塞。

    用法（生产/在线）：
        tracer = LatencyTracer()
        pipe = RecognitionPipeline(rec, trans, tracer=tracer)
        ...跑一段时间后读 tracer.traces() / tracer.breakdowns()

    结构：records 是只增不改的历史（上限 max_retained，满时丢最旧）；
    pending 只跟踪「已 split 未上屏」的在途句。在途身份用弱引用核对对象本身，
    句子对象被回收后其 id 若被新句复用，旧记录留在 records 里、绝不覆盖——
    这是基准逐句等待「完成句数」能单调增长的前提。trace 不持有音频样本。
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, max_retained: int = 2048
    ) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._records: deque[UtteranceTrace] = deque(maxlen=max_retained)
        self._pending: dict[int, tuple[weakref.ReferenceType[Utterance], UtteranceTrace]] = {}
        self._seq = 0

    def mark(self, stage: str, utt: Utterance) -> None:
        """记录一句生命周期的一个阶段时刻（同一阶段只记首次）。"""
        if stage not in STAGES:
            raise ValueError(f"未知阶段: {stage}")
        with self._lock:
            key = id(utt)
            entry = self._pending.get(key)
            if entry is None or entry[0]() is not utt:
                # 新句开始（或 id 被复用：旧句已回收，其记录只留在 records）
                if entry is not None:
                    del self._pending[key]
                self._seq += 1
                tr = UtteranceTrace(index=self._seq, trailing_silence_s=utt.trailing_silence_s)
                self._records.append(tr)
                self._pending[key] = (weakref.ref(utt), tr)
            else:
                tr = entry[1]
            first = stage not in tr.times
            tr.times[stage] = self._clock() if first else tr.times[stage]
            if stage == "shown" and first:
                del self._pending[key]  # 生命周期走完，不再占在途位
            maxlen = self._records.maxlen or 0
            if maxlen and len(self._pending) > maxlen:  # 防被丢弃句的在途位无限累积
                for k in [k for k, (r, _) in self._pending.items() if r() is None]:
                    del self._pending[k]

    def traces(self) -> list[UtteranceTrace]:
        """按出句先后返回全部 trace。"""
        with self._lock:
            return list(self._records)

    def completed_count(self) -> int:
        """已走完打点全生命周期（上屏）的句数。"""
        with self._lock:
            return sum(1 for tr in self._records if "shown" in tr.times)

    def breakdowns(self) -> list[LatencyBreakdown]:
        """打点完整句子的分段延迟（跨句喂入暂停不计入本句延迟，见 breakdown）。"""
        return [b for tr in self.traces() if (b := breakdown(tr)) is not None]
