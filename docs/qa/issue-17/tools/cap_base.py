"""基线（master 7dc2042）真实捕获：同一场景的两张状态图。"""

import sys

sys.path.insert(0, __import__("os").environ.get("LC_BASELINE_TREE", "/tmp/lcd/baseline"))
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.subtitle_window import SubtitleWindow  # noqa: E402

OUT = __import__("os").environ.get("LC_OUT", "/tmp/lcd")
app = QApplication(sys.argv)


def panel(win, title, sub, path):
    shot = win.grab()
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


w = SubtitleWindow()
w.resize(960, 170)
w.show_text("", "")  # 基线签名：只有 (原文, 译文)
panel(
    w,
    "⓪ 改动前基线 · master 7dc2042 · 人还在说话",
    "基线没有草稿路径：句子说完之前，屏幕上不会有任何字幕文字",
    f"{OUT}/p_base_mid.png",
)
w.show_text("The quarter ends at ten", "本季度在十点结束")
panel(
    w,
    "⓪ 改动前基线 · master 7dc2042 · 句子说完",
    "定稿后字幕第一次出现（原文+译文），这就是今天的行为",
    f"{OUT}/p_base_final.png",
)
print("baseline panels ok")
