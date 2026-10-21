"""打包专用入口：PyInstaller 顶层脚本。

放在项目根目录、使用绝对导入（from app.xxx），因为 PyInstaller 把入口脚本
当作顶层模块执行，没有包上下文，相对导入（from .xxx）会失败。
开发时仍用 `python -m app.main`（走 app/main.py）。
"""

import sys


def main() -> None:
    from PySide6.QtWidgets import QApplication

    from app.config.store import load_config
    from app.ui.capture_test import CaptureTestWindow
    from app.ui.config_wizard import ConfigWizard

    app = QApplication(sys.argv)

    if load_config("asr") is None or load_config("translate") is None:
        wizard = ConfigWizard()
        if wizard.exec() != ConfigWizard.DialogCode.Accepted:
            pass

    window = CaptureTestWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
