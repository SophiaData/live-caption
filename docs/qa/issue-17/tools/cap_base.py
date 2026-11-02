"""基线（master 7dc2042）真实捕获：同一场景的两张状态图。"""

import os
import sys
import tempfile


def baseline_tree() -> str:
    """基线代码树（git archive 7dc2042 解出来的目录）：必须用 LC_BASELINE_TREE 指定。"""
    path = os.environ.get("LC_BASELINE_TREE", "")
    if not path:
        sys.exit(
            "请先设 LC_BASELINE_TREE：git archive 7dc2042 | tar -x -C <目录>，再把该目录填进来"
        )
    return path


sys.path.insert(0, baseline_tree())
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.subtitle_window import SubtitleWindow  # noqa: E402

OUT = os.environ.get("LC_OUT", tempfile.gettempdir())


def title_font(a):
    """用应用默认字体（offscreen 下写死字体名会变豆腐块）。"""
    f = a.font()
    f.setPointSize(14)
    f.setBold(True)
    return f


def sub_font(a):
    f = a.font()
    f.setPointSize(11)
    return f


app = QApplication(sys.argv)


def panel(win, title, sub, path):
    shot = win.grab()
    p = QPixmap(1000, 96 + shot.height())
    p.fill(QColor("#101317"))
    g = QPainter(p)
    g.setPen(QColor("#e6e6e6"))
    g.setFont(title_font(app))
    g.drawText(16, 30, title)
    g.setFont(sub_font(app))
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
    "⓪a 改动前基线 · master 7dc2042 · 人还在说话",
    "基线没有草稿路径：句子说完之前，屏幕上不会有任何字幕文字",
    f"{OUT}/p_base_mid.png",
)
w.show_text("The quarter ends at ten", "本季度在十点结束")
panel(
    w,
    "⓪b 改动前基线 · master 7dc2042 · 句子说完",
    "定稿后字幕第一次出现（原文+译文），这就是今天的行为",
    f"{OUT}/p_base_final.png",
)
print("baseline panels ok")
