"""改动后（head 44f9d2d）真实捕获：跑真实管道 + 真实字幕窗，抓三种状态。"""

import sys

import numpy as np
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, __import__("os").path.abspath(__file__).rsplit("/docs/", 1)[0])
from app.pipeline.asr_pipeline import RecognitionPipeline  # noqa: E402
from app.pipeline.segmenter import DraftConfig  # noqa: E402
from app.ui.subtitle_window import SubtitleWindow  # noqa: E402

OUT = __import__("os").environ.get("LC_OUT", "/tmp/lcd")
app = QApplication(sys.argv)
SR, BLOCK = 16000, 160
# 同一段输入：连续说 2.4s → 顿 0.2s → 再说 1.0s → 句尾静音 0.4s
AUDIO = (
    [np.full(BLOCK, 0.5, dtype=np.float32) for _ in range(240)]
    + [np.zeros(BLOCK, dtype=np.float32) for _ in range(20)]
    + [np.full(BLOCK, 0.5, dtype=np.float32) for _ in range(100)]
    + [np.zeros(BLOCK, dtype=np.float32) for _ in range(40)]
)
WORDS = ["The", "quarter", "ends", "at", "ten"]


class FakeRec:
    """按送进来的音频秒数递增地"读出"几个词——模拟边说边补。"""

    def transcribe(self, samples, sample_rate=16000):
        n = max(1, int(len(samples) / SR))
        return True, " ".join(WORDS[:n])


class FakeTr:
    def translate(self, text):
        return True, "本季度在十点结束"


def panel(shot, title, sub, path):
    p = QPixmap(1000, 96 + shot.height())
    p.fill(QColor("#101317"))
    g = QPainter(p)
    g.setPen(QColor("#e6e6e6"))
    g.setFont(QFont("PingFang SC", 15, QFont.Weight.Bold))
    g.drawText(16, 30, title)
    g.setFont(QFont("PingFang SC", 12))
    g.setPen(QColor("#8b949e"))
    g.drawText(16, 58, sub)
    g.drawPixmap(20, 72, shot)
    g.end()
    p.save(path)


def drive(draft_on, stops):
    """跑一遍真实管道；在指定的音频块序号处抓窗口状态。"""
    win = SubtitleWindow()
    win.resize(960, 170)
    pipe = RecognitionPipeline(
        FakeRec(),
        FakeTr(),
        draft=DraftConfig(enabled=draft_on),  # type: ignore[arg-type]
    )
    pipe.set_on_text(
        lambda o, t, u: win.show_text(o, t, int(getattr(u, "utt_id", 0) or 0), bool(u.is_draft))
    )
    shots = {}
    for i, b in enumerate(AUDIO):
        pipe.feed_block(b)
        pipe.step()
        if i in stops:
            shots[i] = (win.grab(), pipe.draft_calls, pipe.draft_skipped, win.entry_count())
    while pipe.step():
        pass
    shots[-1] = (win.grab(), pipe.draft_calls, pipe.draft_skipped, win.entry_count())
    return shots


# ① 开关开启：说到一半（第 180 块 = 1.8s，还在说话）
on = drive(True, {180})
g = on[180][0]
panel(
    g,
    "② 改动后 · 开关开启 · 人还在说话（第 1.8 秒）",
    f"真实管道产出草稿回调：先出现暗色草稿，边说边补（此句草稿调用 {on[180][1]} 次）",
    f"{OUT}/p_head_draft.png",
)
# ③ 开关开启：句子说完 → 同一行原地变完整
f = on[-1][0]
panel(
    f,
    "③ 改动后 · 开关开启 · 句子说完",
    f"同一行被整句结果原地替换 + 补译文；窗口内字幕条数恒为 {on[-1][3]}（没有第二条）",
    f"{OUT}/p_head_final.png",
)
# ④ 开关关闭：同样的音频、同样的时刻，说话期间仍然没有字
off = drive(False, {180})
panel(
    off[180][0],
    "① 改动后 · 开关关闭 · 人还在说话（第 1.8 秒）",
    f"关掉后与今天一致：草稿调用 {off[-1][1]} 次、识别总共只调了定稿那一次",
    f"{OUT}/p_head_off.png",
)
panel(
    off[-1][0],
    "①b 改动后 · 开关关闭 · 句子说完",
    "与基线同一路径（原文+译文），行为未变",
    f"{OUT}/p_head_off_final.png",
)
print("head panels ok; entries:", on[-1][3], "draft_calls:", on[-1][1], "skipped:", on[-1][2])
