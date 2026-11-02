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
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config.store import StoredConfig, load_config
from ..pipeline.asr_pipeline import RecognitionPipeline
from ..pipeline.recognizer import CloudRecognizer, Recognizer
from ..pipeline.segmenter import SAMPLE_RATE, SegmenterConfig
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


@dataclass(frozen=True)
class DeterministicParams:
    """确定性基准的注入参数（耗时都是参数，不写死在断言里）。"""

    recognizer_s: float = 0.400
    translator_s: float = 0.600
    ui_s: float = 0.020  # 假件「上屏回调」耗时
    speech_s: tuple[float, ...] = (1.5, 2.0, 2.5)  # 每句语音时长（句界已知）


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
    totals = [b.total_s for b in breakdowns]
    lines.append(f"中位数 {statistics.median(totals):.2f}s，最大 {max(totals):.2f}s")
    return "\n".join(lines)


def run_deterministic(params: DeterministicParams | None = None) -> str:
    """确定性基准：真实管道 + 假时钟假件，逐句串行跑完并出报告（零真实等待）。"""
    p = params or DeterministicParams()
    clock = FakeClock(0.0)
    tracer = LatencyTracer(clock.now)
    rec = ClockFakeRecognizer(clock, p.recognizer_s)
    trans = ClockFakeTranslator(clock, p.translator_s)
    pipe = RecognitionPipeline(rec, trans, tracer=tracer)  # type: ignore[arg-type]

    def display_hook(original: str, translation: str, _utt: object) -> None:
        if translation:
            clock.advance(p.ui_s)  # 模拟上屏回调耗时

    pipe.set_on_text(display_hook)
    pipe.start()
    try:
        expected = 0
        for speech_s in p.speech_s:
            expected += 1
            for block in tone_blocks(speech_s) + silence_blocks(SILENCE_BLOCKS):
                clock.advance(BLOCK_S)  # 假时钟模拟实时喂音频
                pipe.feed_block(block)
            # 逐句串行：等这句打点走完再喂下一句，保证结果可复现
            if not wait_until(_expect_completed(tracer, expected), timeout_s=20.0):
                raise TimeoutError(f"第 {expected} 句未在限时内走完生命周期")
    finally:
        pipe.stop()

    header = [
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
    report = format_report(tracer.breakdowns(), header)
    return (
        report + "\n\n注：本报告只证明「延迟可拆分测量」，不代表 PRD「2 秒内」达标与否；"
        "达标判定要跑 make latency LIVE=1 接真实服务。"
    )


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
) -> tuple[int, str]:
    """真服务模式：真识别 + 真翻译 + 真实时钟。返回 (退出码, 报告文本)。"""
    asr_cfg, tr_cfg, hint = resolve_live_configs()
    if asr_cfg is None or tr_cfg is None:
        return 2, f"无法跑真服务延迟基准：{hint}"
    if shutil.which("say") is None or shutil.which("afconvert") is None:
        return (
            2,
            "无法跑真服务延迟基准：本机没有 say/afconvert（合成语音输入需要 macOS 自带命令）。"
            f"当前平台：{sys.platform}",
        )

    use_tmp = tmp_dir or Path(tempfile.mkdtemp(prefix="lc-latency-"))
    use_tmp.mkdir(parents=True, exist_ok=True)
    blocks: list[np.ndarray] = []
    for text in sentences:
        try:
            audio = generate_speech(text, use_tmp)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            return 2, f"合成语音失败：{e}\n（需要 macOS 自带 say/afconvert，且系统装有英文语音）"
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
    pipe = RecognitionPipeline(recognizer, translator, tracer=tracer)
    pipe.set_on_text(lambda original, translation, _utt: None)
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
        )
    return 0, format_report(breakdowns, header)


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
    args = parser.parse_args(argv)

    if args.live:
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
        try:
            report = run_deterministic(
                DeterministicParams(
                    recognizer_s=args.recognizer_ms / 1000.0,
                    translator_s=args.translator_ms / 1000.0,
                    ui_s=args.ui_ms / 1000.0,
                    speech_s=speech,
                )
            )
        except TimeoutError as e:
            print(f"基准没跑完：{e}\n（管道或断句配置被改动？先跑 make check 看质量门）")
            return 1
        code, text = 0, report
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
