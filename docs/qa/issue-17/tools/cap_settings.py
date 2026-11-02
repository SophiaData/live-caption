"""字幕设置面板真实捕获：勾选（默认开启）与取消勾选（关闭）两态。"""

import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[4]  # tools → issue-17 → qa → docs → 仓库根
sys.path.insert(0, str(REPO_ROOT))
from app.ui.subtitle_settings import SubtitleSettingsDialog  # noqa: E402
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


REPO_ROOT = Path(__file__).resolve().parents[4]  # tools → issue-17 → qa → docs → 仓库根
app = QApplication(sys.argv)
changes: list[bool] = []
win = SubtitleWindow()
win.resize(960, 170)

for checked, title, path in (
    (True, "⑤ 字幕设置面板 · 改动后 head 44f9d2d（默认勾选 = 开启）", f"{OUT}/p_set_on.png"),
    (False, "⑥ 字幕设置面板 · 改动后 head 44f9d2d（取消勾选 = 关闭）", f"{OUT}/p_set_off.png"),
):
    dlg = SubtitleSettingsDialog(
        win, draft_enabled=checked, on_draft_change=lambda v: changes.append(v)
    )
    dlg.resize(460, dlg.sizeHint().height() + 10)
    dlg.show()
    QApplication.processEvents()
    shot = dlg.grab()
    p = QPixmap(max(500, shot.width() + 24), shot.height() + 92)
    p.fill(QColor("#101317"))
    g = QPainter(p)
    g.setPen(QColor("#e6e6e6"))
    g.setFont(title_font(app))
    g.drawText(12, 30, title)
    g.setFont(sub_font(app))
    g.setPen(QColor("#8b949e"))
    g.drawText(12, 58, f"真实控件截图 · 勾选状态 = {dlg._draft_check.isChecked()}")
    g.drawPixmap(12, 72, shot)
    g.end()
    p.save(path)
    # 顺手验一下取消勾选真的会通知管道（不是画个样子）
    dlg._draft_check.setChecked(not dlg._draft_check.isChecked())
    dlg.close()

print("settings panels ok; 面板通知到的开关变化:", changes)
