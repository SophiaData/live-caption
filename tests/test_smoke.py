"""冒烟测试：验证测试链路与 GUI 基础设施真的在工作。"""


def test_smoke() -> None:
    assert 1 + 1 == 2


def test_main_window_smoke(qtbot: object) -> None:
    from PySide6.QtWidgets import QMainWindow

    window = QMainWindow()
    window.setWindowTitle("live-caption")
    window.show()
    assert window.windowTitle() == "live-caption"
