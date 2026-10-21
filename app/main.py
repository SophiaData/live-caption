"""live-caption 应用主入口。

启动流程：首次启动若没配置模型 → 弹「模型配置向导」；通过后进入捕获测试页。
"""

import sys


def main() -> None:
    from PySide6.QtWidgets import QApplication

    from .config.store import load_config
    from .ui.capture_test import CaptureTestWindow
    from .ui.config_wizard import ConfigWizard

    app = QApplication(sys.argv)

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
