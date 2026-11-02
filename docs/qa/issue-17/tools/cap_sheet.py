import os
import sys

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

OUT = os.environ.get("LC_OUT", "/tmp/lcd")
REPO = os.path.abspath(__file__).rsplit("/docs/", 1)[0]
app = QApplication(sys.argv)


def stack(paths, out, width):
    imgs = [QImage(p) for p in paths]
    h = sum(i.height() for i in imgs) + 8 * (len(imgs) - 1)
    canvas = QImage(width, h, QImage.Format.Format_RGB32)
    canvas.fill(QColor("#0a0c0f"))
    g = QPainter(canvas)
    y = 0
    for i in imgs:
        g.drawImage(0, y, i)
        y += i.height() + 8
    g.end()
    canvas.save(out)
    print(out, canvas.width(), "x", canvas.height())


sub = [
    f"{OUT}/p_base_mid.png",
    f"{OUT}/p_base_final.png",
    f"{OUT}/p_head_off.png",
    f"{OUT}/p_head_off_final.png",
    f"{OUT}/p_head_draft.png",
    f"{OUT}/p_head_final.png",
]
w = max(QImage(p).width() for p in sub)
stack(sub, f"{REPO}/docs/qa/issue-17/compare_subtitles_v2.png", w)

sets = [f"{OUT}/p_set_base.png", f"{OUT}/p_set_on.png", f"{OUT}/p_set_off.png"]
w2 = max(QImage(p).width() for p in sets)
stack(sets, f"{REPO}/docs/qa/issue-17/compare_settings_v2.png", w2)
