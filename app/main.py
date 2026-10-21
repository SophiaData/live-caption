"""live-caption 应用主入口。

当前阶段（功能 #2）：启动后直接进「捕获测试」页，验证系统声音捕获链路。
"""

import sys


def main() -> None:
    from PySide6.QtWidgets import QApplication

    from .ui.capture_test import CaptureTestWindow

    app = QApplication(sys.argv)
    window = CaptureTestWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
