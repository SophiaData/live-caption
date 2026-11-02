"""基线（master 7dc2042）字幕设置面板真实捕获——与改动后同一套字体/尺寸。"""

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


from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, baseline_tree())
from app.ui.subtitle_settings import SubtitleSettingsDialog  # noqa: E402
from app.ui.subtitle_window import SubtitleWindow  # noqa: E402

OUT = os.environ.get("LC_OUT", tempfile.gettempdir())
app = QApplication(sys.argv)


def title_font(a):
    f = a.font()
    f.setPointSize(14)
    f.setBold(True)
    return f


def sub_font(a):
    f = a.font()
    f.setPointSize(11)
    return f


win = SubtitleWindow()
win.resize(960, 170)
dlg = SubtitleSettingsDialog(win)
dlg.resize(460, dlg.sizeHint().height() + 10)
dlg.show()
QApplication.processEvents()
shot = dlg.grab()
p = QPixmap(max(500, shot.width() + 24), shot.height() + 92)
p.fill(QColor("#101317"))
g = QPainter(p)
g.setPen(QColor("#e6e6e6"))
g.setFont(title_font(app))
g.drawText(12, 30, "④ 字幕设置面板 · 改动前基线 master 7dc2042")
g.setFont(sub_font(app))
g.setPen(QColor("#8b949e"))
g.drawText(12, 58, "基线只有字号 / 透明度 / 位置重置，没有「边说边出」开关")
g.drawPixmap(12, 72, shot)
g.end()
p.save(f"{OUT}/p_set_base.png")
print("baseline settings panel ok")
