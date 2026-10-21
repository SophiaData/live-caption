"""live-caption 应用主入口（骨架版）。

启动后显示一个最小主窗口，验证 GUI 链路与测试基础设施可跑通。
"""


def main() -> None:
    from PySide6.QtWidgets import QLabel, QMainWindow

    window = QMainWindow()
    window.setWindowTitle("live-caption")
    window.setCentralWidget(QLabel("live-caption 骨架就绪"))
    window.show()  # pragma: no cover - GUI 冒烟由 pytest-qt 承担


if __name__ == "__main__":
    main()
