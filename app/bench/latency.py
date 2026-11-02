"""端到端字幕延迟基准：`make latency` 一条命令出分阶段报告（Issue #16）。

两种模式：
- 确定性模式（默认）：不联网、不需要任何密钥。识别/翻译用假件，各自耗时
  由参数注入（假时钟拨表，不真等）；断句走真实 Segmenter 逻辑；分阶段
  时间戳走真实管道打点。输出报告，不设失败阈值门禁（真实基线未测出前
  定阈值 = 猜）。
- 真服务模式（LIVE=1）：走配置向导里配好的真实识别/翻译，输入为本机
  `say` 合成的英文语音，实时喂入。没配好时给人话提示并以退出码 2 结束，
  绝不静默跳过。

报告里明确标注哪些环节是真实测量、哪些是被假件模拟的——不把模拟数字当产品延迟。
"""

from __future__ import annotations

import argparse
import math
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from ..config.store import StoredConfig, load_config
from ..pipeline.asr_pipeline import RecognitionPipeline
from ..pipeline.recognizer import CloudRecognizer, Recognizer
from ..pipeline.segmenter import SAMPLE_RATE, DraftConfig, SegmenterConfig
from ..pipeline.trace import FakeClock, LatencyBreakdown, LatencyTracer
from ..pipeline.translator import Translator
from ..providers.openai_compatible import ModelConfig

BLOCK_SAMPLES = 160  # 10ms @16kHz，与捕获层块长一致
BLOCK_S = BLOCK_SAMPLES / SAMPLE_RATE
#: 断句确认「说完」所需的静音块数（跟着断句器配置走，参数漂移也不静默失配）
SILENCE_BLOCKS = math.ceil(SegmenterConfig().min_silence_ms / 1000 / BLOCK_S)

LIVE_SENTENCES = (
    "The meeting will start in ten minutes.",
    "This demo measures how fast subtitles appear on screen.",
    "Real latency numbers help us decide the next step.",
)


#: 排队对比的容差：小于这个变化视为「没有恶化」（异步线程调度的正常抖动量级）
_QUEUE_TOLERANCE_S = 0.05


@dataclass(frozen=True)
class DeterministicParams:
    """确定性基准的注入参数（耗时都是参数，不写死在断言里）。"""

    recognizer_s: float = 0.400
    translator_s: float = 0.600
    ui_s: float = 0.020  # 假件「上屏回调」耗时
    speech_s: tuple[float, ...] = (1.5, 2.0, 2.5)  # 每句语音时长（句界已知）
    # 传入 DraftConfig(enabled=True) 则同场景再跑一遍「边说边出」，并给出草稿提前量
    draft: DraftConfig | None = None


class ClockFakeRecognizer(Recognizer):
    """假识别器（FakeRecognizer 同款思路）：不真等，按注入耗时拨动共享假时钟。"""

    def __init__(self, clock: FakeClock, cost_s: float) -> None:
        self._clock = clock
        self._cost = cost_s
        self._calls = 0

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> tuple[bool, str]:
        self._calls += 1
        self._clock.advance(self._cost)
        return True, f"recognized sentence {self._calls}"

    @property
    def calls(self) -> int:
        """总共被调了几次（边说边出的成本可见性用这个数）。"""
        return self._calls


class ClockFakeTranslator:
    """假翻译器（FakeTranslator 同款思路）：按注入耗时拨动共享假时钟。"""

    def __init__(self, clock: FakeClock, cost_s: float) -> None:
        self._clock = clock
        self._cost = cost_s
        self._calls = 0

    def translate(self, text: str) -> tuple[bool, str]:
        self._calls += 1
        self._clock.advance(self._cost)
        return True, f"译文第{self._calls}句"

    @property
    def calls(self) -> int:
        """被调了几次（用来证明草稿没有偷偷送翻译）。"""
        return self._calls


def wait_until(
    predicate: Callable[[], bool], *, timeout_s: float = 10.0, poll_s: float = 0.01
) -> bool:
    """限时等条件成立（真实时间轮询——跨线程同步机制，不用于数值断言）。

    返回是否等到。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def _expect_completed(tracer: LatencyTracer, n: int) -> Callable[[], bool]:
    """谓词工厂：完成句数达到 n（避免循环变量闭包陷阱）。"""
    return lambda: tracer.completed_count() >= n


def tone_blocks(seconds: float, amplitude: float = 0.5) -> list[np.ndarray]:
    """合成「说话」块：440Hz 正弦，能量远高于断句阈值，边界精确已知。"""
    n = max(1, round(seconds / BLOCK_S))
    t = np.arange(n * BLOCK_SAMPLES) / SAMPLE_RATE
    tone = (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return [tone[i * BLOCK_SAMPLES : (i + 1) * BLOCK_SAMPLES] for i in range(n)]


def silence_blocks(count: int) -> list[np.ndarray]:
    return [np.zeros(BLOCK_SAMPLES, dtype=np.float32) for _ in range(count)]


def _fmt_num(value: float | None) -> str:
    """延迟数字：没打点就写「无」，绝不用 0 冒充测量过。"""
    return "无" if value is None else f"{value:.2f}s"


def _fmt_detail(b: LatencyBreakdown) -> str:
    """一句的「原文出现时刻」与「译文出现时刻」分开写（Issue #17 追加要求 1）。

    原文 ≈2s 而译文 ≈5s 是两回事：产品达不达标要分开看，所以这里分开输出，
    不再用一个 total 把两件事混在一起。开了边说边出时再补一行草稿证据。
    """
    lines = [
        f"    原文出现在说完后 {_fmt_num(b.orig_total_s)}，译文出现在说完后 {_fmt_num(b.total_s)}"
    ]
    if b.draft_lead_s is not None:
        if b.draft_total_s is not None and b.draft_total_s < 0:
            when = f"说完前 {abs(b.draft_total_s):.2f}s（话还没说完就有字）"
        else:
            when = f"说完后 {b.draft_total_s:.2f}s（话已说完，仍比定稿早）"
        lines.append(
            f"    首条草稿出现在{when}，比定稿早 {b.draft_lead_s:.2f}s，"
            f"本句补了 {b.draft_count} 次草稿"
        )
    return "\n".join(lines)


def format_report(breakdowns: Sequence[LatencyBreakdown], header: Sequence[str]) -> str:
    """把分段延迟拼成人话报告；无有效句时明确说明而不是返回空。"""
    lines = list(header)
    if not breakdowns:
        lines.append("（没有一句走完全程，无法给出延迟数字——看上方提示排查）")
        return "\n".join(lines)
    for b in breakdowns:
        lines.append(
            f"第{b.index}句 说完→字幕出现 共 {b.total_s:.2f}s"
            f"（分段 {b.detect_s:.2f} / 排队 {b.queue_s:.2f} / 识别 {b.rec_s:.2f}"
            f" / 翻译 {b.trans_s:.2f} / 上屏 {b.shown_s:.2f}）"
        )
        lines.append(_fmt_detail(b))
    totals = [b.total_s for b in breakdowns]
    lines.append(f"中位数 {statistics.median(totals):.2f}s，最大 {max(totals):.2f}s")
    lines.extend(_fmt_summary_lines(breakdowns))
    return "\n".join(lines)


def _fmt_summary_lines(breakdowns: Sequence[LatencyBreakdown]) -> list[str]:
    """汇总：原文/译文分开、排队单独一行（对比边说边出前后就看这一行）。"""
    origs = [b.orig_total_s for b in breakdowns if b.orig_total_s is not None]
    finals = [b.total_s for b in breakdowns]
    queues = [b.queue_s for b in breakdowns]
    out = []
    if origs:
        out.append(
            f"原文出现：中位 {statistics.median(origs):.2f}s，最大 {max(origs):.2f}s"
            f"｜译文出现：中位 {statistics.median(finals):.2f}s，最大 {max(finals):.2f}s"
        )
    out.append(f"排队：中位 {statistics.median(queues):.2f}s，最大 {max(queues):.2f}s")
    return out


def compare_queues(a: Sequence[LatencyBreakdown], b: Sequence[LatencyBreakdown]) -> str:
    """两组结果的排队对比（Issue #17 追加要求 2：边说边出不得让排队恶化）。"""
    if not a or not b:
        return "排队对比：数据不足（有一组没跑完），不下结论"
    am, bm = statistics.median([x.queue_s for x in a]), statistics.median([x.queue_s for x in b])
    ax, bx = max(x.queue_s for x in a), max(x.queue_s for x in b)
    delta = bx - ax
    verdict = "没有恶化" if delta <= _QUEUE_TOLERANCE_S else "恶化了——需要处理"
    return (
        f"排队对比：关闭边说边出 中位 {am:.2f}s / 最大 {ax:.2f}s → "
        f"开启边说边出 中位 {bm:.2f}s / 最大 {bx:.2f}s（最大变化 {delta:+.2f}s，{verdict}）"
    )


def _run_once(
    p: DeterministicParams, draft: DraftConfig | None
) -> tuple[list[LatencyBreakdown], int, int, int]:
    """跑一遍场景，返回（分段结果, 整句识别调用, 翻译调用, 草稿识别调用）。

    同步驱动（喂一块 → 处理一项）：草稿与定稿的先后完全由喂入顺序决定，
    结果可复现、零真实等待，也避免线程抖动让基准数字跳。
    """
    clock = FakeClock(0.0)
    tracer = LatencyTracer(clock.now)
    rec = ClockFakeRecognizer(clock, p.recognizer_s)
    trans = ClockFakeTranslator(clock, p.translator_s)
    pipe = RecognitionPipeline(rec, trans, tracer=tracer, draft=draft)  # type: ignore[arg-type]

    def display_hook(original: str, translation: str, utt: object) -> None:
        # 上屏是有代价的：译文上屏与草稿上屏都按注入的 ui_s 计时
        if translation or getattr(utt, "is_draft", False):
            clock.advance(p.ui_s)

    pipe.set_on_text(display_hook)
    expected = 0
    for speech_s in p.speech_s:
        expected += 1
        for block in tone_blocks(speech_s) + silence_blocks(SILENCE_BLOCKS):
            clock.advance(BLOCK_S)  # 假时钟模拟实时喂音频
            pipe.feed_block(block)
            pipe.step()
        while pipe.step():  # 这句处理完再喂下一句，保证可复现
            pass
        if tracer.completed_count() < expected:
            raise TimeoutError(f"第 {expected} 句未走完生命周期")
    return tracer.breakdowns(), rec.calls, trans.calls, pipe.draft_calls


def _base_header(p: DeterministicParams) -> list[str]:
    return [
        "=== live-caption 端到端延迟基准（确定性模式：不联网、不需要密钥）===",
        f"注入假耗时：识别 {p.recognizer_s:.2f}s / 翻译 {p.translator_s:.2f}s / 上屏 "
        f"{p.ui_s:.2f}s（都是参数，不是真实服务耗时）",
        "被假件模拟的环节：识别、翻译、上屏（假时钟按注入耗时拨表）",
        "真实测量的环节：断句判定与管道打点顺序（真实 Segmenter + 真实管道，虚拟时钟）",
        "排队在确定性模式下恒为 0（逐句串行，假时钟不随 worker 轮询走）——真实排队看 LIVE",
        f"输入：合成正弦语音 {len(p.speech_s)} 句（"
        f"{'/'.join(f'{s:.1f}s' for s in p.speech_s)}），"
        f"句界 = {SILENCE_BLOCKS * BLOCK_S:.2f}s 静音",
    ]


def run_deterministic(params: DeterministicParams | None = None) -> str:
    """确定性基准：真实管道 + 假时钟假件，逐句串行跑完并出报告（零真实等待）。"""
    p = params or DeterministicParams()
    rows, _rec_calls, _trans_calls, _draft_calls = _run_once(p, p.draft)
    header = list(_base_header(p))
    if p.draft is not None and p.draft.enabled:
        header.append(f"本行是「边说边出」结果：{p.draft}")
    report = format_report(rows, header)
    return (
        report + "\n\n注：本报告只证明「延迟可拆分测量」，不代表 PRD「2 秒内」达标与否；"
        "达标判定要跑 make latency LIVE=1 接真实服务。"
    )


def run_draft_compare(params: DeterministicParams | None = None) -> str:
    """同一场景跑两遍（关/开边说边出）：证明草稿真的更早出现，且没把排队拖坏。

    这是 Issue #17 验收 3 的数字证据；排队的真实前后对比仍以 LIVE 为准
    （确定性模式逐句串行，排队本来就接近 0）。
    """
    p = params or DeterministicParams()
    off_rows, off_rec, off_trans, off_draft = _run_once(p, None)
    d = p.draft if p.draft is not None else DraftConfig(enabled=True)
    seg = SegmenterConfig()  # 提前量要按断句器真实阈值算，不另写一份
    on_rows, on_rec, on_trans, on_draft = _run_once(p, d)
    lines = ["=== 边说边出对比（同一场景跑两遍：关闭 vs 开启）==="]
    lines.append(f"草稿参数：{d}")
    lines.append("关闭边说边出：")
    lines.append(format_report(off_rows, ["（基线）"]))
    lines.append("开启边说边出：")
    lines.append(format_report(on_rows, ["（边说边出）"]))
    leads = [r.draft_lead_s for r in on_rows if r.draft_lead_s is not None]
    if leads:
        lines.append(
            f"草稿提前出现：中位 {statistics.median(leads):.2f}s，"
            f"最小 {min(leads):.2f}s，最大 {max(leads):.2f}s（>0 才是真的更快）"
        )
    else:
        lines.append("草稿提前出现：没有草稿上屏（边说边出没生效，需排查）")
    lines.append(compare_queues(off_rows, on_rows))
    lines.append(
        f"识别调用：关闭 {off_rec} 次 → 开启 {on_rec} 次（其中草稿 {on_draft} 次）；"
        f"翻译调用：关闭 {off_trans} 次 → 开启 {on_trans} 次（草稿不送翻译，应相等）"
    )
    lines.append("注：以上全部来自假件与虚拟时钟，不是产品真实延迟；真实排队看 LIVE。")
    lead_ms = max(0, seg.min_silence_ms - d.silence_ms)
    lines.append(
        f"两种草稿触发的差别：句内停顿触发的草稿只比整句判定早 {lead_ms / 1000:.2f}s"
        "（= 整句静音阈值 − 草稿静音阈值），真服务识别耗时若大于这个提前量，草稿就会落在"
        "「说完后」——仍早于定稿，但不是话没说完就有字；连续说话时的「说完前」靠兜底时长"
        f"（当前 {d.max_s:.2f}s）触发，参数见 --draft-silence-ms / --draft-max-s。"
    )
    return "\n".join(lines)


def _model_label(cfg: StoredConfig) -> str:
    return f"{cfg.model} @ {cfg.base_url}"


def resolve_live_configs() -> tuple[StoredConfig | None, StoredConfig | None, str]:
    """读真实服务配置；缺哪个就给人话说明（绝不静默跳过）。"""
    asr = load_config("asr")
    tr = load_config("translate")
    missing = []
    if asr is None:
        missing.append("识别服务")
    if tr is None:
        missing.append("翻译服务")
    if missing:
        return (
            asr,
            tr,
            f"还没配好{'和'.join(missing)}。\n"
            "请先打开软件（make run）走一遍「模型配置向导」，把识别与翻译服务都配置好，"
            "再跑：make latency LIVE=1",
        )
    assert asr is not None and tr is not None
    return asr, tr, ""


def generate_speech(text: str, dest_dir: Path) -> np.ndarray:
    """用本机 say 合成一句英文语音 → float32 单声道 16k 采样。"""
    aiff = dest_dir / "say_out.aiff"
    wav = dest_dir / "say_out.wav"
    subprocess.run(["say", "-o", str(aiff), text], check=True, capture_output=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
        check=True,
        capture_output=True,
    )
    with wave.open(str(wav), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != SAMPLE_RATE:
            raise RuntimeError(f"合成音频格式不对：{w.getnchannels()}声道 {w.getframerate()}Hz")
        frames = w.readframes(w.getnframes())
    return (np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0).reshape(-1)


def run_live(
    sentences: Sequence[str] = LIVE_SENTENCES,
    *,
    tmp_dir: Path | None = None,
    timeout_s: float = 120.0,
    settle_s: float = 3.0,
    draft: DraftConfig | None = None,
) -> tuple[int, str]:
    """真服务模式：真识别 + 真翻译 + 真实时钟。返回 (退出码, 报告文本)。"""
    code, text, _rows = _run_live_once(
        sentences, tmp_dir=tmp_dir, timeout_s=timeout_s, settle_s=settle_s, draft=draft
    )
    return code, text


def run_live_compare(
    sentences: Sequence[str] = LIVE_SENTENCES,
    *,
    tmp_dir: Path | None = None,
    timeout_s: float = 120.0,
    settle_s: float = 3.0,
    draft: DraftConfig | None = None,
) -> tuple[int, str]:
    """真服务下跑两遍（关闭 vs 开启边说边出），给排队列前后对比（Issue #17 追加要求 2）。"""
    d = draft or DraftConfig(enabled=True)
    code_a, text_a, rows_a = _run_live_once(
        sentences, tmp_dir=tmp_dir, timeout_s=timeout_s, settle_s=settle_s, draft=None
    )
    code_b, text_b, rows_b = _run_live_once(
        sentences, tmp_dir=tmp_dir, timeout_s=timeout_s, settle_s=settle_s, draft=d
    )
    parts = [
        "###### 真服务 · 关闭边说边出 ######",
        text_a,
        "",
        "###### 真服务 · 开启边说边出 ######",
        text_b,
    ]
    if rows_a and rows_b:
        parts.extend(["", compare_queues(rows_a, rows_b)])
    else:
        parts.extend(["", "排队对比：有一组没跑完，不下结论（看上方提示排查后重跑）"])
    code = code_a if code_a != 0 else code_b
    return code, "\n".join(parts)


def _run_live_once(
    sentences: Sequence[str] = LIVE_SENTENCES,
    *,
    tmp_dir: Path | None = None,
    timeout_s: float = 120.0,
    settle_s: float = 3.0,
    draft: DraftConfig | None = None,
) -> tuple[int, str, list[LatencyBreakdown]]:
    """真服务跑一遍：返回 (退出码, 报告文本, 分段结果)。"""
    asr_cfg, tr_cfg, hint = resolve_live_configs()
    if asr_cfg is None or tr_cfg is None:
        return 2, f"无法跑真服务延迟基准：{hint}", []
    if shutil.which("say") is None or shutil.which("afconvert") is None:
        return (
            2,
            "无法跑真服务延迟基准：本机没有 say/afconvert（合成语音输入需要 macOS 自带命令）。"
            f"当前平台：{sys.platform}",
            [],
        )

    use_tmp = tmp_dir or Path(tempfile.mkdtemp(prefix="lc-latency-"))
    use_tmp.mkdir(parents=True, exist_ok=True)
    blocks: list[np.ndarray] = []
    for text in sentences:
        try:
            audio = generate_speech(text, use_tmp)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            return (
                2,
                f"合成语音失败：{e}\n（需要 macOS 自带 say/afconvert，且系统装有英文语音）",
                [],
            )
        blocks.extend(audio[i : i + BLOCK_SAMPLES] for i in range(0, len(audio), BLOCK_SAMPLES))
        gap = silence_blocks(round(0.6 / BLOCK_S))  # 句间停顿，保证断句边界
        blocks.extend(gap)

    tracer = LatencyTracer()
    recognizer = CloudRecognizer(
        ModelConfig(base_url=asr_cfg.base_url, api_key=asr_cfg.api_key, model=asr_cfg.model)
    )
    translator = Translator(
        ModelConfig(base_url=tr_cfg.base_url, api_key=tr_cfg.api_key, model=tr_cfg.model)
    )
    pipe = RecognitionPipeline(recognizer, translator, tracer=tracer, draft=draft)
    pipe.set_on_text(lambda original, translation, utt: None)
    pipe.start()
    try:
        for block in blocks:
            pipe.feed_block(block)
            time.sleep(BLOCK_S)  # 实时喂入（模拟「电脑正在播的声音」）
        prev, last_change = -1, time.monotonic()
        timed_out = False
        while True:
            n_tr = len(tracer.traces())
            now = time.monotonic()
            if n_tr != prev:
                prev, last_change = n_tr, now
            if n_tr > 0 and tracer.completed_count() == n_tr and now - last_change >= settle_s:
                break  # 全部出句都已上屏且已稳定（say 断句数可能多/少于句数，不假设）
            if now - last_change > timeout_s:
                timed_out = True
                break
            time.sleep(0.1)
    finally:
        pipe.stop()

    header = [
        "=== live-caption 端到端延迟基准（真服务模式 LIVE=1）===",
        f"识别：{_model_label(asr_cfg)} / 翻译：{_model_label(tr_cfg)}",
        "真实测量的环节：断句判定、识别调用、翻译调用、上屏回调（真实时钟全程计时）",
        "未包含：Qt 字幕窗实际绘制（本基准不进界面，终点=字幕回调返回）",
        "「说完」锚点=断句判定回退句尾静音确认，按音频实时到达计——捕获侧积压不计入",
        f"输入：say 合成英文语音 {len(sentences)} 句，按真实时间喂入",
        f"边说边出：{'关闭' if draft is None or not draft.enabled else draft}",
    ]
    if timed_out:
        header.append(
            f"注意：等了 {timeout_s:.0f}s 仍有句子未走完（识别慢或堵在队列），"
            "以下只是已完成句的数字"
        )
    breakdowns = tracer.breakdowns()
    if not breakdowns:
        return (
            1,
            "\n".join(header) + "\n（一句字幕都没走完：识别可能失败或音频断句不成句，"
            "看软件日志排查后重跑）",
            [],
        )
    cost = (
        f"\n边说边出用量：草稿上屏 {pipe.draft_calls} 次，"
        f"被丢弃草稿 {pipe.draft_dropped} 次（定稿优先/背压保护）"
    )
    if draft is None or not draft.enabled:
        cost = "\n边说边出用量：关闭（无草稿调用）"
    return 0, format_report(breakdowns, header) + cost, breakdowns


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make latency",
        description="端到端字幕延迟分阶段报告（默认确定性假件基准，不联网、不需要密钥）",
    )
    parser.add_argument(
        "--live", action="store_true", help="走真实识别/翻译服务（需先在配置向导配好）"
    )
    parser.add_argument("--recognizer-ms", type=float, default=400.0, help="假识别耗时（毫秒）")
    parser.add_argument("--translator-ms", type=float, default=600.0, help="假翻译耗时（毫秒）")
    parser.add_argument("--ui-ms", type=float, default=20.0, help="假上屏回调耗时（毫秒）")
    parser.add_argument(
        "--sentences",
        type=str,
        default="1.5,2.0,2.5",
        help="确定性模式每句语音时长（秒，逗号分隔）",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="加跑一遍「边说边出」并给出草稿提前量与排队对比",
    )
    parser.add_argument("--draft-silence-ms", type=int, default=None, help="草稿停顿阈值（毫秒）")
    parser.add_argument("--draft-max-s", type=float, default=None, help="草稿兜底时长（秒）")
    args = parser.parse_args(argv)

    d = DraftConfig(enabled=True)
    if args.draft_silence_ms is not None:
        d = replace(d, silence_ms=args.draft_silence_ms)
    if args.draft_max_s is not None:
        d = replace(d, max_s=args.draft_max_s)

    if args.live:
        # 不带 --draft 时保持 Issue #16 的行为（不开草稿）；带上则同场景跑两遍给排队对比
        if args.draft:
            code, text = run_live_compare(draft=d)
        else:
            code, text = run_live()
    else:
        try:
            speech = tuple(float(s) for s in args.sentences.split(","))
            if not speech or any(s < SegmenterConfig().min_utterance_s for s in speech):
                raise ValueError
        except ValueError:
            print(
                f"参数不合法：--sentences 需要逗号分隔的秒数，且每句不低于断句下限"
                f"（{SegmenterConfig().min_utterance_s:g}s），收到「{args.sentences}」"
            )
            return 2
        base = DeterministicParams(
            recognizer_s=args.recognizer_ms / 1000.0,
            translator_s=args.translator_ms / 1000.0,
            ui_s=args.ui_ms / 1000.0,
            speech_s=speech,
        )
        try:
            report = run_deterministic(base)
            if args.draft:
                # 同场景再跑一遍关闭 vs 开启：草稿提前量、排队对比、调用次数都在这段
                report = report + "\n\n" + run_draft_compare(replace(base, draft=d))
        except TimeoutError as e:
            print(f"基准没跑完：{e}\n（管道或断句配置被改动？先跑 make check 看质量门）")
            return 1
        code, text = 0, report
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
