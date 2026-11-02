"""Issue #17 测试：边说边出（草稿字幕先显示、说完原地修正）。

覆盖验收标准点名的三件事：
- 草稿→定稿落在同一行，且窗口内字幕条目数量没有增加；
- 定稿文本覆盖草稿文本（不是追加）；
- 开关关闭时完全不产生草稿调用（识别调用次数与今天一致）。
外加：草稿不送翻译、定稿永远优先于草稿（排队不被拖坏）、延迟报告能给出
草稿提前量与「原文出现 / 译文出现」分开的数字。
"""

from __future__ import annotations

import re

import numpy as np
import pytest

from app.bench import latency as bench
from app.pipeline.asr_pipeline import RecognitionPipeline
from app.pipeline.recognizer import Recognizer
from app.pipeline.segmenter import DraftConfig, Segmenter, SegmenterConfig, Utterance
from app.pipeline.trace import STAGES, FakeClock, LatencyTracer

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 160


def tone_blocks(count: int, amplitude: float = 0.5) -> list[np.ndarray]:
    t = np.arange(count * BLOCK_SAMPLES) / SAMPLE_RATE
    tone = (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return [tone[i * BLOCK_SAMPLES : (i + 1) * BLOCK_SAMPLES] for i in range(count)]


def silence_blocks(count: int) -> list[np.ndarray]:
    return [np.zeros(BLOCK_SAMPLES, dtype=np.float32) for _ in range(count)]


class CountingRecognizer(Recognizer):
    """按调用次数返回不同文本，并记录每段音频长度（草稿是不断变长的前缀）。"""

    def __init__(self, final_text: str = "整句识别结果") -> None:
        self.final_text = final_text
        self.calls: list[int] = []

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> tuple[bool, str]:
        self.calls.append(len(samples))
        return True, f"第{len(self.calls)}次:{self.final_text}"


class RecordingTranslator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def translate(self, text: str) -> tuple[bool, str]:
        self.calls.append(text)
        return True, "译文"


def run_scenario(
    *, draft: DraftConfig | None, speech: int = 200, tail: int = 35
) -> tuple[list[tuple[str, str, int, bool]], CountingRecognizer, RecordingTranslator, int, int]:
    """同一段语音跑一遍管道：同步驱动（喂一块→处理一项），零真实等待。

    返回 [(原文, 译文, utt_id, is_draft)]、识别器、翻译器、草稿调用数、草稿丢弃数。
    """
    rec = CountingRecognizer()
    trans = RecordingTranslator()
    pipe = RecognitionPipeline(rec, trans, draft=draft)
    shown: list[tuple[str, str, int, bool]] = []

    def on_text(original: str, translation: str, utt: Utterance) -> None:
        shown.append((original, translation, utt.utt_id, utt.is_draft))

    pipe.set_on_text(on_text)
    for block in tone_blocks(speech) + silence_blocks(tail):
        pipe.feed_block(block)
        pipe.step()
    while pipe.step():
        pass
    return shown, rec, trans, pipe.draft_calls, pipe.draft_dropped


class TestSegmenterDraft:
    def test_draft_appears_before_final_and_shares_utt_id(self) -> None:
        """人还在说话就先出草稿；草稿与定稿同号（同一行的依据）。"""
        seg = Segmenter(draft=DraftConfig(enabled=True))
        drafts: list[Utterance] = []
        finals: list[Utterance] = []
        for block in tone_blocks(200) + silence_blocks(35):
            seg.feed(block)
            drafts += seg.pop_drafts()
            finals += seg.pop_utterances()
        assert drafts and finals
        assert all(d.is_draft for d in drafts)
        assert not any(f.is_draft for f in finals)
        assert {d.utt_id for d in drafts} == {finals[0].utt_id}
        # 草稿是「已说到的前缀」：一次比一次长，且短于整句
        lengths = [len(d.samples) for d in drafts]
        assert lengths == sorted(lengths)
        assert lengths[-1] < len(finals[0].samples)

    def test_drafts_are_prefixes_of_the_final(self) -> None:
        """草稿内容必须是定稿的开头若干采样（原地替换的语义前提）。"""
        seg = Segmenter(draft=DraftConfig(enabled=True))
        drafts: list[Utterance] = []
        final: Utterance | None = None
        for block in tone_blocks(220) + silence_blocks(35):
            seg.feed(block)
            drafts += seg.pop_drafts()
            got = seg.pop_utterances()
            final = got[0] if got else final
        assert final is not None and drafts
        for d in drafts:
            n = len(d.samples)
            assert np.array_equal(d.samples, final.samples[:n])

    def test_disabled_emits_nothing_extra(self) -> None:
        """默认关闭：不产出草稿，整句行为与 Issue #17 之前一致。"""
        seg = Segmenter()
        drafts: list[Utterance] = []
        for block in tone_blocks(200) + silence_blocks(35):
            seg.feed(block)
            drafts += seg.pop_drafts()
        assert drafts == []
        assert len(seg.pop_utterances()) == 1

    def test_silence_trigger_is_configurable(self) -> None:
        """静音阈值是参数：调高到接近整句判定值就不该有停顿触发的草稿。"""

        def feed_with(silence_ms: int) -> int:
            seg = Segmenter(draft=DraftConfig(enabled=True, silence_ms=silence_ms, max_s=99.0))
            total = 0
            for block in tone_blocks(120) + silence_blocks(35):
                seg.feed(block)
                total += len(seg.pop_drafts())  # 定稿会把本句草稿清掉，所以要边喂边取
            return total

        assert feed_with(80) == 1  # 句内短停顿 → 出一条草稿
        assert feed_with(300) == 0  # 阈值不低于整句判定值 → 整句先到，不补草稿

    def test_max_duration_trigger_is_configurable(self) -> None:
        """兜底时长是参数：连续说话没停顿时，每隔 max_s 补一条草稿。"""

        def feed_with(max_s: float) -> int:
            seg = Segmenter(
                SegmenterConfig(max_utterance_s=99.0),
                draft=DraftConfig(
                    enabled=True,
                    silence_ms=9999,
                    max_s=max_s,
                    min_s=0.2,
                    max_per_utterance=99,
                    max_window_s=99.0,  # 只看兜底时长，不叠加截断
                ),
            )
            total = 0
            for block in tone_blocks(200):  # 连续说话 2 秒，一次都没停
                seg.feed(block)
                total += len(seg.pop_drafts())
            return total

        assert feed_with(0.5) == 4
        assert feed_with(1.0) == 2

    def test_draft_window_is_capped(self) -> None:
        """草稿音频有长度上限：句子再长，一次草稿调用也不会和整句一样贵。"""
        seg = Segmenter(
            SegmenterConfig(max_utterance_s=99.0),
            draft=DraftConfig(
                enabled=True,
                silence_ms=9999,
                max_s=0.5,
                min_s=0.2,
                max_per_utterance=99,
                max_window_s=1.0,
            ),
        )
        got: list[Utterance] = []
        for block in tone_blocks(300):  # 连续说 3 秒
            seg.feed(block)
            got += seg.pop_drafts()
        assert got
        assert max(len(g.samples) for g in got) <= SAMPLE_RATE + BLOCK_SAMPLES
        # 开头一段不再变长后不该重复送：省额度，也不顶住定稿
        assert len(got) == 2

    def test_max_per_utterance_caps_cost(self) -> None:
        """单句草稿次数有上限（成本上限，可配置）。"""
        seg = Segmenter(
            SegmenterConfig(max_utterance_s=99.0),
            draft=DraftConfig(enabled=True, silence_ms=9999, max_s=0.2, max_per_utterance=2),
        )
        drafts: list[Utterance] = []
        for block in tone_blocks(300):
            seg.feed(block)
            drafts += seg.pop_drafts()
        assert len(drafts) == 2

    def test_very_short_speech_does_not_draft(self) -> None:
        """短于 min_s 的声音不送草稿（咳嗽不该烧配额）。"""
        seg = Segmenter(draft=DraftConfig(enabled=True, min_s=0.8, max_s=0.8, silence_ms=10))
        drafts: list[Utterance] = []
        for block in tone_blocks(30):  # 0.3s，短于 min_s
            seg.feed(block)
            drafts += seg.pop_drafts()
        assert drafts == []


class TestPipelineDraft:
    def test_draft_then_final_same_utterance(self) -> None:
        """同一句：先出草稿、后出定稿；定稿是最后一次回调。"""
        shown, _rec, trans, draft_calls, _dropped = run_scenario(draft=DraftConfig(enabled=True))
        assert draft_calls > 0
        assert {u for _o, _t, u, _d in shown} == {1}
        drafts = [s for s in shown if s[3]]
        finals = [s for s in shown if not s[3]]
        assert drafts and len(finals) == 2
        assert finals[-1][1] == "译文"
        assert finals[-1][0].endswith("整句识别结果")

    def test_draft_never_sent_to_translator(self) -> None:
        """草稿不送翻译槽位：翻译调用次数与开关无关，且只翻那一句定稿。"""
        s_off, _r1, trans_off, _d1, _x1 = run_scenario(draft=None)
        s_on, _r2, trans_on, _d2, _x2 = run_scenario(draft=DraftConfig(enabled=True))
        final_off = [o for o, t, _u, d in s_off if t == "译文"][0]
        final_on = [o for o, t, _u, d in s_on if t == "译文"][0]
        assert len(trans_off.calls) == len(trans_on.calls) == 1
        assert trans_off.calls == [final_off]
        assert trans_on.calls == [final_on]
        draft_texts = [o for o, _t, _u, d in s_on if d]
        assert draft_texts  # 确实出过草稿
        assert all(dt not in trans_on.calls for dt in draft_texts)

    def test_off_state_identical_to_before(self) -> None:
        """开关关闭：草稿调用 0 次，识别只调一次（与今天一致）。"""
        shown, rec, trans, draft_calls, dropped = run_scenario(draft=None)
        assert (draft_calls, dropped) == (0, 0)
        assert len(rec.calls) == 1
        assert [d for _o, _t, _u, d in shown] == [False, False]
        assert trans.calls == [shown[0][0]]

    def test_runtime_switch_off_stops_drafts(self) -> None:
        """运行中关掉开关：立刻不再产出草稿（设置面板即时生效）。"""
        rec = CountingRecognizer()
        pipe = RecognitionPipeline(rec, RecordingTranslator(), draft=DraftConfig(enabled=True))
        pipe.set_draft_enabled(False)
        shown: list[tuple[str, str, int, bool]] = []
        pipe.set_on_text(lambda o, t, u: shown.append((o, t, u.utt_id, u.is_draft)))
        for block in tone_blocks(200) + silence_blocks(35):
            pipe.feed_block(block)
            pipe.step()
        while pipe.step():
            pass
        assert pipe.draft_calls == 0
        assert all(not d for _o, _t, _u, d in shown)
        assert len(rec.calls) == 1

    def test_final_never_waits_behind_draft(self) -> None:
        """整句队列有积压时草稿一律不入队——定稿的排队不会被草稿拖坏。"""
        rec = CountingRecognizer()
        pipe = RecognitionPipeline(rec, None, draft=DraftConfig(enabled=True))
        # 第一句喂完但不 step：定稿堆在队列里，制造「有积压」的真实状态
        for block in tone_blocks(200) + silence_blocks(35):
            pipe.feed_block(block)
        assert len(rec.calls) == 0  # 没 step：什么都还没送识别
        # 第二句边说边出的草稿必须全被挡下（只记公开计数，不碰内部队列）
        for block in tone_blocks(200):
            pipe.feed_block(block)
        assert pipe.draft_calls == 0
        assert pipe.draft_dropped > 0

    def test_runtime_switch_on_starts_drafts(self) -> None:
        """先关着启动、之后在设置里打开：必须真的开始出草稿，不能是哑开关。"""
        rec = CountingRecognizer()
        pipe = RecognitionPipeline(rec, None, draft=DraftConfig(enabled=False))
        shown: list[tuple[str, str, int, bool]] = []
        pipe.set_on_text(lambda o, t, u: shown.append((o, t, u.utt_id, u.is_draft)))
        assert pipe.draft_enabled is False
        pipe.set_draft_enabled(True)
        assert pipe.draft_enabled is True
        for block in tone_blocks(200) + silence_blocks(35):
            pipe.feed_block(block)
            pipe.step()
        while pipe.step():
            pass
        assert pipe.draft_calls > 0
        assert any(d for _o, _t, _u, d in shown)
        assert not any(d for _o, t, _u, d in shown if t)  # 草稿仍然一条都没送翻译

    def test_draft_cannot_render_after_switch_off_midflight(self) -> None:
        """开关关掉时正好有一条草稿在识别：那条草稿不许再上屏（认完也不许）。"""

        class SwitchOffInside(CountingRecognizer):
            """第一次送识别的必然是草稿（草稿先入队）：识别中途把开关关掉。"""

            def transcribe(self, samples: np.ndarray, sample_rate: int = 16000):
                if not self.calls:
                    self._pipe.set_draft_enabled(False)  # type: ignore[attr-defined]
                return super().transcribe(samples, sample_rate)

        rec = SwitchOffInside()
        pipe = RecognitionPipeline(rec, None, draft=DraftConfig(enabled=True))
        rec._pipe = pipe  # type: ignore[attr-defined]
        shown: list[tuple[str, str, int, bool]] = []
        pipe.set_on_text(lambda o, t, u: shown.append((o, t, u.utt_id, u.is_draft)))
        for block in tone_blocks(200) + silence_blocks(35):
            pipe.feed_block(block)
            pipe.step()
        while pipe.step():
            pass
        assert pipe.draft_calls == 0  # 一次都没上屏
        assert pipe.draft_dropped >= 1
        assert [d for _o, _t, _u, d in shown] == [False]  # 只有定稿那次（没配翻译器）

    def test_stale_draft_cannot_outrank_final(self) -> None:
        """定稿已上屏后，迟到的同句草稿必须作废（不能盖掉定稿）。"""
        pipe = RecognitionPipeline(CountingRecognizer(), None, draft=DraftConfig(enabled=True))
        shown: list[tuple[str, str, int, bool]] = []
        pipe.set_on_text(lambda o, t, u: shown.append((o, t, u.utt_id, u.is_draft)))
        pipe._enqueue(
            Utterance(samples=np.zeros(3200, dtype=np.float32), start_s=0.0, end_s=0.2, utt_id=7)
        )
        assert pipe.step()
        pipe._enqueue_draft(
            Utterance(
                samples=np.zeros(1600, dtype=np.float32),
                start_s=0.0,
                end_s=0.1,
                utt_id=7,
                is_draft=True,
            )
        )
        assert pipe.step() is False
        assert [d for _o, _t, _u, d in shown] == [False]
        assert pipe.draft_dropped >= 1


class TestWindowSameLine:
    """字幕窗：草稿→定稿原地替换，绝不冒出第二条字幕（验收核心断言）。"""

    def test_draft_then_final_does_not_add_entries(self, qtbot: pytest.FixtureRequest) -> None:
        from app.ui.subtitle_window import SubtitleWindow

        win = SubtitleWindow()
        qtbot.addWidget(win)  # type: ignore[attr-defined]
        before = win.entry_count()
        win.show_text("草稿一二三", "", 1, True)
        mid = win.entry_count()
        win.show_text("一二三整句识别结果", "译文在此", 1)
        assert mid == before
        assert win.entry_count() == before  # 条目数量没有增加
        assert win.displayed() == ("译文在此", "一二三整句识别结果")

    def test_final_overwrites_draft_text(self, qtbot: pytest.FixtureRequest) -> None:
        from app.ui.subtitle_window import SubtitleWindow

        win = SubtitleWindow()
        qtbot.addWidget(win)  # type: ignore[attr-defined]
        win.show_text("草稿开头", "", 1, True)
        assert win.displayed()[0] == "草稿开头"
        win.show_text("草稿开头完整句", "整句译文", 1)
        main, sub = win.displayed()
        assert (main, sub) == ("整句译文", "草稿开头完整句")
        assert main != "草稿开头完整句"  # 定稿把草稿那行换掉了，不是拼接残留

    def test_late_draft_cannot_overwrite_final(self, qtbot: pytest.FixtureRequest) -> None:
        from app.ui.subtitle_window import SubtitleWindow

        win = SubtitleWindow()
        qtbot.addWidget(win)  # type: ignore[attr-defined]
        win.show_text("完整句", "整句译文", 5)
        win.show_text("迟到的旧草稿", "", 5, True)
        assert win.displayed() == ("整句译文", "完整句")
        win.show_text("更早句的草稿", "", 4, True)
        assert win.displayed() == ("整句译文", "完整句")

    def test_draft_is_visually_marked(self, qtbot: pytest.FixtureRequest) -> None:
        """草稿态主行用较暗字色，定稿后恢复：用户能看出这句还没定稿。"""
        from app.ui.subtitle_window import DRAFT_COLOR, FINAL_COLOR, SubtitleWindow

        win = SubtitleWindow()
        qtbot.addWidget(win)  # type: ignore[attr-defined]
        win.show_text("草稿", "", 1, True)
        assert DRAFT_COLOR in win._trans_label.styleSheet()
        win.show_text("完整句", "译文", 1)
        assert DRAFT_COLOR not in win._trans_label.styleSheet()
        assert FINAL_COLOR in win._trans_label.styleSheet()


def make_utt(
    end_s: float, trailing: float = 0.3, utt_id: int = 1, is_draft: bool = False
) -> Utterance:
    return Utterance(
        samples=np.zeros(1600, dtype=np.float32),
        start_s=0.0,
        end_s=end_s,
        trailing_silence_s=trailing,
        utt_id=utt_id,
        is_draft=is_draft,
    )


class TestTraceDraft:
    def test_existing_stage_semantics_unchanged(self) -> None:
        """#16 的 STAGES 与五段语义不许被改动（只加不改）。"""
        assert STAGES == (
            "split",
            "rec_start",
            "rec_end",
            "orig_shown",
            "tr_start",
            "tr_end",
            "shown",
        )

    def test_draft_before_split_is_attached_to_final(self) -> None:
        """草稿先打点、定稿后 split：草稿时刻要并进定稿那条记录，不新建句子记录。"""
        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now)
        clock.advance(1.0)
        tracer.note_draft(make_utt(1.0, utt_id=1, is_draft=True))
        clock.advance(0.5)
        final = make_utt(1.5, utt_id=1)
        for stage in STAGES:
            clock.advance(0.1)
            tracer.mark(stage, final)
        rows = tracer.breakdowns()
        assert len(tracer.traces()) == 1  # 草稿不额外占句记录
        assert len(rows) == 1
        tr = tracer.traces()[0]
        assert tr.draft_shown_at == pytest.approx(1.0)
        assert rows[0].draft_lead_s is not None
        assert rows[0].draft_lead_s > 0  # 草稿确实比定稿早出现

    def test_draft_total_is_negative_when_shown_while_speaking(self) -> None:
        """话没说完就出字 → draft_total_s 为负（这是「边说边出」的定义）。"""
        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now)
        final = make_utt(3.0, trailing=0.3, utt_id=2)
        clock.advance(1.2)  # split 前的 1.2s：还在说话
        tracer.note_draft(make_utt(1.2, utt_id=2, is_draft=True))
        clock.advance(1.8)  # 到 split（说完 0.3s 确认 → 说完锚点 = 3.0-0.3 = 2.7）
        for stage in STAGES:
            clock.advance(0.05)
            tracer.mark(stage, final)
        b = tracer.breakdowns()[0]
        assert b.draft_total_s is not None and b.draft_total_s < 0

    def test_repeated_drafts_counted_once_per_time(self) -> None:
        """每条草稿都要计数（成本可见），但只保留最早那条的时刻。"""
        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now)
        clock.advance(0.4)
        tracer.note_draft(make_utt(0.4, utt_id=3, is_draft=True))
        clock.advance(0.6)
        tracer.note_draft(make_utt(1.0, utt_id=3, is_draft=True))
        final = make_utt(1.5, utt_id=3)
        for stage in STAGES:
            clock.advance(0.05)
            tracer.mark(stage, final)
        tr = tracer.traces()[0]
        assert tr.draft_count == 2
        assert tr.draft_shown_at == pytest.approx(0.4)

    def test_unidentified_utterance_draft_ignored(self) -> None:
        """手工构造、没有句标识（utt_id=0）的草稿不参与对齐，不污染统计。"""
        tracer = LatencyTracer(FakeClock(0.0).now)
        tracer.note_draft(make_utt(0.5, utt_id=0, is_draft=True))
        assert tracer.traces() == []

    def test_no_draft_means_no_extra_fields(self) -> None:
        """没开边说边出：草稿字段保持 None/0，报告数字与 #16 完全一致。"""
        clock = FakeClock(0.0)
        tracer = LatencyTracer(clock.now)
        final = make_utt(1.0, utt_id=1)
        for stage in STAGES:
            clock.advance(0.1)
            tracer.mark(stage, final)
        b = tracer.breakdowns()[0]
        assert b.draft_lead_s is None and b.draft_total_s is None and b.draft_count == 0
        assert b.orig_total_s is not None
        assert b.total_s == pytest.approx(b.detect_s + b.queue_s + b.rec_s + b.trans_s + b.shown_s)


class TestLatencyReportEvidence:
    def test_report_splits_orig_and_final(self) -> None:
        """报告必须把「原文出现时刻」与「译文出现时刻」分开输出（追加要求 1）。"""
        report = bench.run_deterministic(bench.DeterministicParams(speech_s=(1.5,)))
        assert "原文出现在说完后" in report
        assert "译文出现在说完后" in report
        assert "原文出现：中位" in report and "译文出现：中位" in report

    def test_report_keeps_legacy_line_format(self) -> None:
        """#16 的行格式不许被改动：#16 的老解析器（LINE_RE）仍能解析出七段数字。"""
        from tests.test_latency import parse_rows

        report = bench.run_deterministic(bench.DeterministicParams(speech_s=(1.5, 2.0)))
        assert len(parse_rows(report)) == 2

    def test_draft_wording_matches_sign(self) -> None:
        """「话还没说完就有字」只能在草稿真的早于说完时说，不许吹。"""
        from app.pipeline.trace import LatencyBreakdown

        def line(draft_total: float) -> str:
            b = LatencyBreakdown(
                index=1,
                detect_s=0.3,
                queue_s=0.0,
                rec_s=0.4,
                trans_s=0.6,
                shown_s=0.0,
                total_s=1.3,
                orig_total_s=0.7,
                draft_total_s=draft_total,
                draft_lead_s=2.0,
                draft_count=1,
            )
            return bench._fmt_detail(b)

        assert "说完前" in line(-0.5) and "话还没说完就有字" in line(-0.5)
        after = line(0.5)
        assert "说完后" in after and "话还没说完就有字" not in after

    def test_compare_shows_draft_earlier_than_final(self) -> None:
        """同场景跑两遍：草稿比定稿早出现（>0），且排队与翻译调用没有被拖坏。"""
        p = bench.DeterministicParams(speech_s=(1.5, 2.0, 2.5))
        text = bench.run_draft_compare(p)
        assert "草稿提前出现：中位" in text
        assert "排队对比" in text
        assert "没有恶化" in text
        # 翻译调用次数两遍相同 → 草稿没送翻译
        tail = [ln for ln in text.splitlines() if ln.startswith("识别调用：")][0]
        assert "翻译调用：关闭 3 次 → 开启 3 次" in tail

    def test_compare_leads_positive_for_each_sentence(self) -> None:
        """每句都要有正的提前量（不是只有一句侥幸）。"""
        off_rows, *_rest = bench._run_once(bench.DeterministicParams(speech_s=(2.0,)), None)
        on_rows, _r1, _r2, draft_calls, _skip = bench._run_once(
            bench.DeterministicParams(speech_s=(2.0,)), DraftConfig(enabled=True)
        )
        assert off_rows[0].draft_lead_s is None
        assert on_rows[0].draft_lead_s is not None and on_rows[0].draft_lead_s > 0
        assert on_rows[0].draft_total_s is not None and on_rows[0].draft_total_s < 0
        assert draft_calls > 0

    def test_cli_draft_flag_plumbs_through(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert bench.main(["--draft", "--sentences", "2.0"]) == 0
        out = capsys.readouterr().out
        assert "边说边出对比" in out and "草稿提前出现" in out

    def test_live_without_flag_keeps_issue16_behavior(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LIVE 不带 --draft 必须不开草稿：Issue #16 的行为不许被本票偷偷改掉。"""
        seen: list[dict[str, object]] = []

        def fake_run_live(*args: object, **kwargs: object) -> tuple[int, str]:
            seen.append(dict(kwargs))
            return 0, "ok"

        monkeypatch.setattr(bench, "run_live", fake_run_live)
        assert bench.main(["--live"]) == 0
        assert seen == [{}]  # 根本没传 draft → 走 Issue #16 的关闭路径

    def test_live_with_flag_runs_both_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """LIVE 带 --draft 必须走「关闭 vs 开启」两遍对比（追加要求 2 的交付口径）。"""
        called: list[str] = []
        monkeypatch.setattr(bench, "run_live", lambda **kw: (called.append("single"), (0, ""))[1])
        monkeypatch.setattr(
            bench, "run_live_compare", lambda **kw: (called.append("compare"), (0, ""))[1]
        )
        assert bench.main(["--live", "--draft"]) == 0
        assert called == ["compare"]
        assert bench.main(["--live"]) == 0
        assert called == ["compare", "single"]

    def test_cli_draft_params_plumb_through(self, capsys: pytest.CaptureFixture[str]) -> None:
        """草稿窗口参数走命令行，不写死：兜底时长越短，同一句话补的草稿越多。"""
        assert bench.main(["--draft", "--sentences", "2.5", "--draft-max-s", "0.5"]) == 0
        many = capsys.readouterr().out
        assert bench.main(["--draft", "--sentences", "2.5", "--draft-max-s", "3.0"]) == 0
        few = capsys.readouterr().out

        def draft_total(text: str) -> int:
            return sum(int(x) for x in re.findall(r"本句补了 (\d+) 次草稿", text))

        assert draft_total(many) > draft_total(few) > 0


class TestSettingsToggle:
    def test_dialog_exposes_draft_toggle_default_on(self, qtbot: pytest.FixtureRequest) -> None:
        from app.ui import subtitle_settings

        class FakeWin:
            font_size = 30
            opacity = 0.9

        dlg = subtitle_settings.SubtitleSettingsDialog(FakeWin())  # type: ignore[arg-type]
        qtbot.addWidget(dlg)  # type: ignore[attr-defined]
        assert dlg._draft_check.isChecked() is True
        assert "2~4" in dlg._draft_note.text()  # 成本说明不许隐瞒

    def test_dialog_toggle_notifies_and_can_start_off(self, qtbot: pytest.FixtureRequest) -> None:
        from app.ui import subtitle_settings

        seen: list[bool] = []

        class FakeWin:
            font_size = 30
            opacity = 0.9

        dlg = subtitle_settings.SubtitleSettingsDialog(
            FakeWin(),
            draft_enabled=False,
            on_draft_change=seen.append,  # type: ignore[arg-type]
        )
        qtbot.addWidget(dlg)  # type: ignore[attr-defined]
        assert dlg._draft_check.isChecked() is False
        dlg._draft_check.setChecked(True)
        assert seen == [True]


class TestDraftPrefs:
    def test_bool_and_int_prefs_roundtrip(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import store

        monkeypatch.setattr(store, "DB_PATH", tmp_path / "prefs.db")
        assert store.load_bool_pref("draft_enabled", True) is True  # 默认开
        store.save_bool_pref("draft_enabled", False)
        assert store.load_bool_pref("draft_enabled", True) is False
        assert store.load_int_pref("draft_silence_ms", 150) == 150
        store.save_pref("draft_silence_ms", "80")
        assert store.load_int_pref("draft_silence_ms", 150) == 80

    def test_broken_pref_falls_back(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """坏偏好不该让软件起不来：读不懂就按默认值。"""
        from app.config import store

        monkeypatch.setattr(store, "DB_PATH", tmp_path / "prefs.db")
        store.save_pref("draft_enabled", "也许")
        store.save_pref("draft_max_ms", "abc")
        assert store.load_bool_pref("draft_enabled", True) is True
        assert store.load_int_pref("draft_max_ms", 1200) == 1200

    def test_main_window_builds_draft_from_prefs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.ui import capture_test as ct

        values = {"draft_enabled": "0", "draft_silence_ms": "90", "draft_max_ms": "2000"}
        monkeypatch.setattr(ct, "load_bool_pref", lambda k, d: values[k] != "0")
        monkeypatch.setattr(ct, "load_int_pref", lambda k, d: int(values[k]))
        cfg = ct.draft_from_prefs()
        assert cfg.enabled is False
        assert (cfg.silence_ms, cfg.max_s) == (90, 2.0)

    def test_out_of_range_prefs_get_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """手改坏的偏好不能让功能变哑：阈值不低于整句判定值时必须夹回来。"""
        from app.pipeline.segmenter import SegmenterConfig

        cfg_def = SegmenterConfig()
        from app.ui import capture_test as ct

        values = {"draft_enabled": "1", "draft_silence_ms": "99999", "draft_max_ms": "999999"}
        monkeypatch.setattr(ct, "load_bool_pref", lambda k, d: values[k] != "0")
        monkeypatch.setattr(ct, "load_int_pref", lambda k, d: int(values[k]))
        cfg = ct.draft_from_prefs()
        assert 0 < cfg.silence_ms < cfg_def.min_silence_ms  # 否则停顿触发永远不发生
        assert cfg.min_s < cfg.max_s <= cfg_def.max_utterance_s

        values2 = {"draft_enabled": "1", "draft_silence_ms": "1", "draft_max_ms": "1"}
        monkeypatch.setattr(ct, "load_int_pref", lambda k, d: int(values2[k]))
        cfg2 = ct.draft_from_prefs()
        assert cfg2.silence_ms >= ct.DRAFT_MIN_SILENCE_MS  # 不许退化成每块都吐
        assert cfg2.max_s >= cfg2.min_s


class TestDraftQueueProtection:
    """Issue #17 追加要求 2：草稿不得让排队恶化。

    时钟注入（和 LatencyTracer 一样），所以「这条草稿值不值得送」的判断完全可复现，
    不靠墙钟 sleep —— CI 上 sleep 精度不够，用墙钟写的用例会飘。
    """

    def _make(self, cost_s: float) -> tuple[RecognitionPipeline, FakeClock, list[int]]:
        clock = FakeClock(0.0)
        calls: list[int] = []

        class _VirtualRec(Recognizer):
            def transcribe(self, samples: np.ndarray, sample_rate: int = 16000):
                calls.append(len(samples))
                clock.advance(cost_s)  # 每次识别固定花这么多时间
                return True, f"txt{len(calls)}"

        pipe = RecognitionPipeline(
            _VirtualRec(), None, draft=DraftConfig(enabled=True), clock=clock.now
        )
        pipe.set_on_text(lambda o, t, u: None)
        return pipe, clock, calls

    def _feed_one_backlog_cycle(self, pipe: RecognitionPipeline) -> None:
        """说一段 → 句中短停顿（停顿触发草稿）→ 再说 → 句尾静音（定稿）。"""
        for block in tone_blocks(120) + silence_blocks(8) + tone_blocks(60) + silence_blocks(35):
            pipe.feed_block(block)
            pipe.step()
        while pipe.step():
            pass

    def test_slow_service_skips_drafts_that_cannot_be_earlier(self) -> None:
        """识别比草稿的提前量还慢：停顿草稿换不来更早的字，必须省掉。"""
        pipe, _clock, calls = self._make(0.4)  # 0.4s/次，远大于 0.15s 提前量
        self._feed_one_backlog_cycle(pipe)
        assert pipe.draft_skipped >= 1
        # 定稿那次之外，最多只有「学会耗时」的探针草稿
        assert len(calls) <= 3

    def test_fast_service_still_shows_silence_drafts(self) -> None:
        """识别快过提前量时不该省：那时草稿确实能更早出字。"""
        pipe, _clock, _calls = self._make(0.02)  # 0.02s/次 << 0.15s 提前量
        self._feed_one_backlog_cycle(pipe)
        assert pipe.draft_skipped == 0
        assert pipe.draft_calls >= 2

    def test_draft_window_is_capped_in_pipeline(self) -> None:
        """送出去的草稿音频不会超过 max_window_s：一次调用的占用有上界。"""
        pipe, _clock, calls = self._make(0.05)
        for block in tone_blocks(400):  # 连续说 4 秒（超过 max_utterance 会强制切分）
            pipe.feed_block(block)
            pipe.step()
        while pipe.step():
            pass
        draft_calls = [
            c for c in calls if c <= int(DraftConfig().max_window_s * 16000) + BLOCK_SAMPLES
        ]
        assert draft_calls  # 草稿确实被限在窗口内

    def test_probe_learns_rate_then_gates(self) -> None:
        """没测过耗时前先放一条探针；学会比值之后才判断，不一上来就猜阈值。"""
        pipe, _clock, _calls = self._make(0.4)
        assert pipe.last_rec_rate is None
        pipe._enqueue_draft(
            Utterance(
                samples=np.zeros(3200, dtype=np.float32),
                start_s=0.0,
                end_s=0.2,
                is_draft=True,
                utt_id=1,
                draft_lead_s=0.15,
                draft_trigger="silence",
            )
        )
        assert pipe.step()  # 探针放行
        assert pipe.last_rec_rate is not None and pipe.last_rec_rate > 0

    def test_timeout_drafts_are_never_gated(self) -> None:
        """连续说话触发的草稿提前量大（到强制切分才定稿），慢服务也照送。"""
        pipe, _clock, _calls = self._make(0.4)
        pipe._enqueue_draft(
            Utterance(
                samples=np.zeros(3200, dtype=np.float32),
                start_s=0.0,
                end_s=0.2,
                is_draft=True,
                utt_id=2,
                draft_lead_s=0.15,
                draft_trigger="timeout",
            )
        )
        assert pipe.step()
        assert pipe.draft_calls == 1 and pipe.draft_skipped == 0

    def test_drafts_pause_while_finals_queue_up_and_resume(self) -> None:
        """处理中途又来了整句 = 这一波追不上：立刻停补草稿，队列清空后自动恢复。"""
        clock = FakeClock(0.0)
        calls: list[int] = []

        class ArriveWhileBusy(Recognizer):
            def transcribe(self, samples: np.ndarray, sample_rate: int = 16000):
                calls.append(len(samples))
                if len(calls) == 1:
                    # 模拟音频线程：worker 正忙时又切完了一句
                    self.pipe._enqueue(  # type: ignore[attr-defined]
                        Utterance(
                            samples=np.zeros(3200, dtype=np.float32),
                            start_s=0.0,
                            end_s=0.2,
                            utt_id=50,
                        )
                    )
                clock.advance(0.3)
                return True, "txt"

        rec = ArriveWhileBusy()
        pipe = RecognitionPipeline(rec, None, draft=DraftConfig(enabled=True), clock=clock.now)
        pipe.set_on_text(lambda o, t, u: None)
        rec.pipe = pipe  # type: ignore[attr-defined]
        # 处理这一条定稿：期间来了第二条 → 队列没清空 → 暂停草稿
        pipe._enqueue(
            Utterance(samples=np.zeros(3200, dtype=np.float32), start_s=0.0, end_s=0.2, utt_id=1)
        )
        assert pipe.step()
        assert pipe.draft_paused is True
        dropped_before = pipe.draft_dropped
        pipe._enqueue_draft(
            Utterance(
                samples=np.zeros(3200, dtype=np.float32),
                start_s=0.0,
                end_s=0.2,
                is_draft=True,
                utt_id=60,
                draft_lead_s=1.0,
                draft_trigger="timeout",
            )
        )
        assert pipe.draft_dropped > dropped_before  # 一条都没进队列
        calls_before = len(calls)
        # 把排队的整句清掉：暂停期间草稿一次都没占用识别通道
        while pipe.step():
            pass
        assert pipe.draft_calls == 0  # 暂停期间没有一次草稿识别
        assert calls[:calls_before] == [3200]  # 通道只用于排队的整句
        assert pipe.draft_paused is False  # 队列空了，恢复
        pipe._enqueue_draft(
            Utterance(
                samples=np.zeros(3200, dtype=np.float32),
                start_s=0.0,
                end_s=0.2,
                is_draft=True,
                utt_id=61,
                draft_lead_s=1.0,
                draft_trigger="timeout",
            )
        )
        assert pipe.step()
        assert pipe.draft_calls >= 1

    def test_gate_uses_length_aware_rate_not_absolute_cost(self) -> None:
        """比值判据：整句识别慢不等于草稿也慢——短草稿不该被误杀。"""
        pipe, _clock, calls = self._make(0.6)  # 一次调用固定 0.6s
        pipe._enqueue(
            Utterance(samples=np.zeros(96000, dtype=np.float32), start_s=0.0, end_s=6.0, utt_id=1)
        )
        assert pipe.step()  # 6 秒音频 → 比值 0.1
        assert pipe.last_rec_rate is not None
        assert pipe.last_rec_rate == pytest.approx(0.1, rel=0.05)
        # 0.3s 音频的停顿草稿：预计 0.03s < 0.15s 提前量 → 该送
        pipe._enqueue_draft(
            Utterance(
                samples=np.zeros(4800, dtype=np.float32),
                start_s=0.0,
                end_s=0.3,
                is_draft=True,
                utt_id=2,
                draft_lead_s=0.15,
                draft_trigger="silence",
            )
        )
        assert pipe.step()
        assert pipe.draft_calls == 1 and pipe.draft_skipped == 0
        # 6 秒音频的停顿草稿：预计 0.6s > 0.15s → 入队前就拒，一次识别都不调
        before = len(calls)
        pipe._enqueue_draft(
            Utterance(
                samples=np.zeros(96000, dtype=np.float32),
                start_s=0.0,
                end_s=6.0,
                is_draft=True,
                utt_id=3,
                draft_lead_s=0.15,
                draft_trigger="silence",
            )
        )
        assert pipe.step() is False
        assert pipe.draft_skipped == 1
        assert len(calls) == before


class TestDraftLeadStamp:
    """草稿要自带「比预计定稿早多少、由什么触发」，管道才能判断值不值得送。"""

    def test_silence_draft_carries_small_lead(self) -> None:
        seg = Segmenter(draft=DraftConfig(enabled=True, silence_ms=150, max_s=99.0))
        got: list[Utterance] = []
        for block in tone_blocks(120) + silence_blocks(20):
            seg.feed(block)
            got += seg.pop_drafts()
        assert len(got) == 1
        assert got[0].draft_trigger == "silence"
        assert got[0].draft_lead_s == pytest.approx(0.300 - 0.150)

    def test_timeout_draft_carries_remaining_speech_lead(self) -> None:
        cfg = SegmenterConfig(max_utterance_s=3.0)
        seg = Segmenter(cfg, draft=DraftConfig(enabled=True, silence_ms=9999, max_s=1.0))
        got: list[Utterance] = []
        for block in tone_blocks(150):  # 连续说 1.5s
            seg.feed(block)
            got += seg.pop_drafts()
        assert got and all(d.draft_trigger == "timeout" for d in got)
        assert got[0].draft_lead_s == pytest.approx(3.0 - 1.0, abs=0.02)
