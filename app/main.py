"""live-caption 应用主入口。

启动流程：首次启动若没配置模型 → 弹「模型配置向导」；通过后进入捕获测试页。
"""

from __future__ import annotations

import sys
from typing import Any


def _apply_icon(app: Any) -> None:
    """给应用和所有窗口挂图标（菜单栏/Dock/任务切换）。

    打包后图标来自 .app 的 Info.plist（assets/live-caption.icns，见 spec）；
    从源码跑时没有 bundle，这里显式加载 assets 里的 icns，避免显示成解释器图标。
    """
    from pathlib import Path

    from PySide6.QtGui import QIcon

    candidates = [
        Path(__file__).resolve().parent.parent / "assets" / "live-caption.icns",
        Path(getattr(sys, "_MEIPASS", "")) / "assets" / "live-caption.icns",
    ]
    for p in candidates:
        try:
            if p.is_file():
                app.setWindowIcon(QIcon(str(p)))
                return
        except OSError:
            continue


def main() -> None:
    from PySide6.QtWidgets import QApplication

    from .config.store import load_config
    from .ui.capture_test import CaptureTestWindow
    from .ui.config_wizard import ConfigWizard

    app = QApplication(sys.argv)
    _apply_icon(app)

    # 首次启动（任一模态缺失）→ 弹配置向导
    if load_config("asr") is None or load_config("translate") is None:
        wizard = ConfigWizard()
        if wizard.exec() != ConfigWizard.DialogCode.Accepted:
            # 用户关掉向导：仍进捕获测试页（但无法翻译）；后续再做「未配置」提示
            pass

    window = CaptureTestWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
