"""Issue #16 测试：分阶段时间戳（观测注入缝）+ `make latency` 报告生成。

覆盖：断句尾部静音标注、tracer 采集与分段折算、真实管道端到端打点、
报告生成与「改注入耗时→报告随之变化」的变异证明、真服务模式缺配置提示。
"""

import re
from collections.abc import Sequence
from unittest.mock import patch

import numpy as np
import pytest

from app.bench import latency as bench
from app.config.store import StoredConfig
from app.pipeline.asr_pipeline import RecognitionPipeline
from app.pipeline.segmenter import SAMPLE_RATE, Segmenter, Utterance
from app.pipeline.trace import STAGES, FakeClock, LatencyTracer, breakdown
from tests.test_asr_pipeline import FakeRecognizer
from tests.test_translator import FakeTranslator

BLOCK_SAMPLES = 160


def tone_blocks(n: int, amplitude: float = 0.5) -> list[np.ndarray]:
    t = np.arange(n * BLOCK_SAMPLES) / SAMPLE_RATE
    tone = (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return [tone[i * BLOCK_SAMPLES : (i + 1) * BLOCK_SAMPLES] for i in range(n)]


def silence_blocks(n: int) -> list[np.ndarray]:
    return [np.zeros(BLOCK_SAMPLES, dtype=np.float32) for _ in range(n)]


def make_utt(end_s: float, trailing: float) -> Utterance:
    return Utterance(
        samples=np.zeros(1600, dtype=np.float32),
        start_s=0.0,
        end_s=end_s,
        trailing_silence_s=trailing,
    )


def feed_paced(pipe: RecognitionPipeline, clock: FakeClock, blocks: Sequence[np.ndarray]) -> None:
    """假时钟按每块 10ms 推进后喂入（模拟实时捕获，零真实等待）。"""
    for b in blocks:
        clock.advance(0.01)
        pipe.feed_block(b)


LINE_RE = re.compile(
    r"第(\d+)句 说完→字幕出现 共 ([0-9.]+)s"
    r"（分段 ([0-9.]+) / 排队 ([0-9.]+) / 识别 ([0-9.]+) / 翻译 ([0-9.]+) / 上屏 ([0-9.]+)）"
)


def parse_rows(report: str) -> list[list[float]]:
    """报告行 → [序号, 总, 分段, 排队, 识别, 翻译, 上屏]（浮点）。"""
    return [[float(g) for g in m.groups()] for m in LINE_RE.finditer(report)]


class TestSegmenterTrailingSilence:
    def test_trailing_silence_recorded(self) -> None:
        """静音确认满 300ms 断句：句对象带上尾部静音时长（延迟观测的锚点）。"""
        seg = Segmenter()
        for b in tone_blocks(100) + silence_blocks(30):
            seg.feed(b)
        utts = seg.pop_utterances()
        assert len(utts) == 1
        assert utts[0].trailing_silence_s == pytest.approx(0.30, abs=0.001)
        assert utts[0].speech_end_s == pytest.approx(utts[0].end_s - 0.30, abs=0.001)

    def test_forced_split_has_zero_trailing(self) -> None:
        """句长上限强制断句时没有静音确认（说话仍在继续），尾部静音=0。"""
        seg = Segmenter()
        for b in tone_blocks(305):
            seg.feed(b)
        utts = seg.pop_utterances()
        assert len(utts) == 1
        assert utts[0].trailing_silence_s == pytest.approx(0.0, abs=1e-9)

    def test_default_keeps_old_construction_valid(self) -> None:
        """旧构造方式（不传新字段）仍合法：纯数据扩展，不破坏调用方。"""
        u = Utterance(samples=np.zeros(1600, dtype=np.float32), start_s=0.0, end_s=0.1)
        assert u.trailing_silence_s == 0.0


class TestTracerCollection:
    def test_breakdown_math(self) -> None:
        """分段五段相加 = 总延迟；各段差值来自打点读数（纯采集逻辑）。"""
        clock = FakeClock(1.0)
        tracer = LatencyTracer(clock.now)
        utt = make_utt(end_s=0.6, trailing=0.3)
        for dt, stage in [
            (0.00, "split"),
            (0.02, "rec_start"),
            (0.38, "rec_end"),
            (0.00, "orig_shown"),
            (0.00, "tr_start"),
            (0.60, "tr_end"),
            (0.02, "shown"),
        ]:
            clock.advance(dt)
            tracer.mark(stage, utt)
        tr = tracer.traces()[0]
        assert tr.index == 1
        b = breakdown(tr)
        assert b is not None
        # 说完锚点 = split 回退 0.30s 静音确认 → detect 恰为确认窗口
        assert b.detect_s == pytest.approx(0.30)
        assert b.queue_s == pytest.approx(0.02)
        assert b.rec_s == pytest.approx(0.38)
        assert b.trans_s == pytest.approx(0.60)
        assert b.shown_s == pytest.approx(0.02)
        assert b.total_s == pytest.approx(1.32)
        assert b.total_s == pytest.approx(b.detect_s + b.queue_s + b.rec_s + b.trans_s + b.shown_s)

    def test_incomplete_trace_gives_none(self) -> None:
        """没走完生命周期（如被背压丢弃）的句：折算返回 None，报告不猜数。"""
        tracer = LatencyTracer(FakeClock(5.0).now)
        tracer.mark("split", make_utt(0.6, 0.3))
        assert breakdown(tracer.traces()[0]) is None

    def test_no_translator_path_counts_shown_from_rec_end(self) -> None:
        """只有原文上屏（没配翻译器）：翻译段记 0，上屏段从识别返回起算。"""
        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now)
        utt = make_utt(0.6, 0.3)
        for stage in ("split", "rec_start", "rec_end"):
            tracer.mark(stage, utt)
        clock.advance(0.1)
        tracer.mark("shown", utt)
        b = breakdown(tracer.traces()[0])
        assert b is not None
        assert b.trans_s == 0.0
        assert b.shown_s == pytest.approx(0.1)

    def test_unknown_stage_rejected(self) -> None:
        tracer = LatencyTracer(FakeClock(0.0).now)
        with pytest.raises(ValueError):
            tracer.mark("teleport", make_utt(0.6, 0.3))

    def test_completed_count_tracks_shown(self) -> None:
        tracer = LatencyTracer(FakeClock(0.0).now)
        u1, u2 = make_utt(0.6, 0.3), make_utt(1.2, 0.3)
        for stage in STAGES:
            tracer.mark(stage, u1)
        tracer.mark("split", u2)
        assert tracer.completed_count() == 1
        assert len(tracer.traces()) == 2


class TestPipelineTraceEndToEnd:
    """真实管道（真线程真队列）+ 假时钟假件：验证打点确实在生命周期节点采集。"""

    def test_full_lifecycle_stages_and_detect_lag(self) -> None:
        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now)
        pipe = RecognitionPipeline(FakeRecognizer("hello"), FakeTranslator("你好"), tracer=tracer)
        pipe.start()
        try:
            feed_paced(pipe, clock, tone_blocks(100) + silence_blocks(30))
            assert bench.wait_until(lambda: tracer.completed_count() >= 1, timeout_s=10.0), (
                "管道未在限时内走完一句"
            )
        finally:
            pipe.stop()
        tr = tracer.traces()[0]
        assert set(tr.times) == set(STAGES)
        b = breakdown(tr)
        assert b is not None
        # 断句确认「说完」要等 300ms 静音 —— 真实 Segmenter 行为，不是注入的假数
        assert b.detect_s == pytest.approx(0.30, abs=0.011)
        assert b.rec_s == pytest.approx(0.0)  # 假件瞬时返回
        assert b.trans_s == pytest.approx(0.0)
        assert b.total_s == pytest.approx(b.detect_s + b.queue_s + b.rec_s + b.trans_s + b.shown_s)

    def test_no_tracer_pipeline_unchanged(self) -> None:
        """不传 tracer：管道照常出字幕（观测缝对默认路径零影响）。"""
        rec = FakeRecognizer("hello")
        pipe = RecognitionPipeline(rec, FakeTranslator("你好"))
        texts: list[tuple[str, str]] = []
        pipe.set_on_text(lambda o, t, u: texts.append((o, t)))
        pipe.start()
        try:
            feed_paced(pipe, FakeClock(0.0), tone_blocks(60) + silence_blocks(35))
            assert bench.wait_until(lambda: len(texts) >= 2, timeout_s=10.0)
        finally:
            pipe.stop()
        assert texts[0] == ("hello", "")
        assert texts[-1] == ("hello", "你好")
        assert rec.calls  # 识别器确实被调过

    def test_serial_sentences_keep_monotonic_history(self) -> None:
        """连跑 10 句串行等待：旧句记录不被 id 复用吞掉（曾致 completed 永不达标而挂死）。

        钉住真实回归：句子对象回收后新句复用其 id 时，已上屏旧句的记录必须
        还在历史里，串行等待「完成句数」才能单调增长。
        """
        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now)
        pipe = RecognitionPipeline(FakeRecognizer(), FakeTranslator(), tracer=tracer)
        pipe.start()
        try:
            for expected in range(1, 11):
                feed_paced(pipe, clock, tone_blocks(40) + silence_blocks(30))
                assert bench.wait_until(
                    lambda n=expected: tracer.completed_count() >= n, timeout_s=10.0
                ), f"第 {expected} 句后完成数没到 {expected}（历史记录丢了）"
        finally:
            pipe.stop()
        rows = tracer.breakdowns()
        assert [r.index for r in rows] == list(range(1, 11))

    def test_dropped_utterance_not_marked(self) -> None:
        """背压丢弃的句没有 split 打点（_enqueue 先查队满再打点），不进延迟统计。"""
        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now)
        pipe = RecognitionPipeline(FakeRecognizer(), tracer=tracer)
        for _ in range(25):
            pipe._enqueue(make_utt(0.6, 0.3))
        assert pipe.dropped_utterances == 5
        assert len(tracer.traces()) == 20
        assert tracer.completed_count() == 0
        assert tracer.breakdowns() == []


class TestDeterministicReport:
    def test_default_report_matches_injected_costs(self) -> None:
        report = bench.run_deterministic()
        rows = parse_rows(report)
        assert len(rows) == 3
        for _, total, detect, queue, rec, trans, shown in rows:
            assert rec == pytest.approx(0.40)  # 注入的假识别耗时
            assert trans == pytest.approx(0.60)  # 注入的假翻译耗时
            assert shown == pytest.approx(0.02)  # 注入的假上屏耗时
            assert detect == pytest.approx(0.30, abs=0.011)  # 断句器真实行为
            assert queue == pytest.approx(0.0, abs=0.011)
            assert total == pytest.approx(detect + queue + rec + trans + shown, abs=0.011)
        assert "中位数" in report and "最大" in report
        assert "被假件模拟的环节" in report  # 诚实标注（验收 5）
        assert "真实测量的环节" in report

    def test_mutation_translator_cost_moves_report(self) -> None:
        """变异证明（验收 3）：翻译耗时 600ms→1200ms，总耗时随之 +0.6s，且被用例抓住。"""
        p = bench.DeterministicParams(speech_s=(1.5, 2.0))
        base = bench.run_deterministic(p)
        slow = bench.run_deterministic(
            bench.DeterministicParams(recognizer_s=0.4, translator_s=1.2, speech_s=(1.5, 2.0))
        )
        bt = [r[1] for r in parse_rows(base)]
        st = [r[1] for r in parse_rows(slow)]
        assert all(parse_rows(base)[i][5] == pytest.approx(0.60) for i in range(2))
        assert all(parse_rows(slow)[i][5] == pytest.approx(1.20) for i in range(2))
        assert all(s == pytest.approx(b + 0.60, abs=0.011) for s, b in zip(st, bt, strict=True))

    def test_injected_values_not_hardcoded(self) -> None:
        """报告数字跟随任意注入参数：换一组非默认值，分段数字即变。"""
        report = bench.run_deterministic(
            bench.DeterministicParams(
                recognizer_s=0.25, translator_s=0.75, ui_s=0.01, speech_s=(1.0,)
            )
        )
        rows = parse_rows(report)
        assert len(rows) == 1
        _, _, _, _, rec, trans, shown = rows[0]
        assert (rec, trans, shown) == (
            pytest.approx(0.25),
            pytest.approx(0.75),
            pytest.approx(0.01),
        )

    def test_sentences_param_respected(self) -> None:
        report = bench.run_deterministic(bench.DeterministicParams(speech_s=(1.2,)))
        assert len(parse_rows(report)) == 1
        assert "合成正弦语音 1 句" in report

    def test_cli_ms_params_plumb_through(self) -> None:
        """make latency 的 REC_MS/TR_MS（毫秒）经 CLI 进到报告数字里。"""
        with patch("builtins.print") as fake_print:
            code = bench.main(
                ["--recognizer-ms", "250", "--translator-ms", "1150", "--sentences", "1.5"]
            )
        assert code == 0
        out = " ".join(str(a) for a in fake_print.call_args[0])
        assert "识别 0.25" in out and "翻译 1.15" in out

    def test_cli_bad_sentences_human_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        """坏参数给人话提示 + 非零退出，不甩 ValueError traceback。"""
        assert bench.main(["--sentences", "abc"]) == 2
        assert "参数不合法" in capsys.readouterr().out
        assert bench.main(["--sentences", "0.05"]) == 2  # 低于断句下限


class TestLiveModeGuidance:
    """真服务模式：没配好服务时必须给人话提示并以非零码退出（不得静默跳过）。"""

    def test_missing_configs_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bench, "load_config", lambda slot: None)
        code, text = bench.run_live()
        assert code == 2
        assert "模型配置向导" in text and "make run" in text
        assert "识别服务" in text and "翻译服务" in text

    def test_missing_translate_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = StoredConfig(base_url="https://api.test/v1", model="asr-m", api_key="sk-x")
        monkeypatch.setattr(bench, "load_config", lambda slot: cfg if slot == "asr" else None)
        code, text = bench.run_live()
        assert code == 2
        assert "翻译服务" in text

    def test_say_tools_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = StoredConfig(base_url="https://api.test/v1", model="m", api_key="sk-x")
        monkeypatch.setattr(bench, "load_config", lambda slot: cfg)
        monkeypatch.setattr(bench.shutil, "which", lambda name: None)
        code, text = bench.run_live()
        assert code == 2
        assert "say" in text and "afconvert" in text

    def test_resolve_live_configs_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = StoredConfig(base_url="https://api.test/v1", model="m", api_key="sk-x")
        monkeypatch.setattr(bench, "load_config", lambda slot: cfg)
        asr, tr, hint = bench.resolve_live_configs()
        assert hint == "" and asr is cfg and tr is cfg

    def test_model_label_never_contains_key(self) -> None:
        """报告/日志用的模型标签只含模型名和地址，密钥绝不进输出。"""
        cfg = StoredConfig(base_url="https://api.test/v1", model="m", api_key="sk-topsecret")
        label = bench._model_label(cfg)
        assert "sk-topsecret" not in label and "m" in label


class TestTracerMemory:
    def test_traces_survive_utterance_release(self) -> None:
        """trace 不持有音频对象：句子对象释放后历史延迟数字照常可算。"""
        import gc

        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now)
        utt = make_utt(0.6, 0.3)
        for stage in STAGES:
            clock.advance(0.01)
            tracer.mark(stage, utt)
        del utt
        gc.collect()
        rows = tracer.breakdowns()
        assert len(rows) == 1
        assert rows[0].detect_s == pytest.approx(0.30)

    def test_old_records_evicted_in_flight_kept(self) -> None:
        """超过保留上限先清最旧且已走完的，在途句记录不被误删。"""
        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now, max_retained=4)
        old = [make_utt(0.6, 0.3) for _ in range(6)]
        for u in old:  # 6 句全部走完 → 只留最近几句
            for stage in STAGES:
                tracer.mark(stage, u)
        inflight = make_utt(0.9, 0.3)
        for stage in ("split", "rec_start", "rec_end"):
            tracer.mark(stage, inflight)
        rows = tracer.breakdowns()
        assert [r.index for r in rows] == [4, 5, 6]
        assert tracer.completed_count() == 3
        assert len(tracer.traces()) == 4  # 在途那句仍在（打点不全，不进 breakdown）
