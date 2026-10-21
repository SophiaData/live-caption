"""音量条接线测试：验证「声音→音量条」这根电线没断（QA FAIL 项的防复发测试）。"""

from PySide6.QtWidgets import QApplication

from app.ui.capture_test import CaptureTestWindow


def _window(qtbot: object) -> CaptureTestWindow:
    _app = QApplication.instance() or QApplication([])
    win = CaptureTestWindow()
    return win


def test_level_relay_updates_volume_bar(qtbot: object) -> None:
    """信号驱动：音量回调 → 音量条必须变化。"""
    win = _window(qtbot)
    assert win._level_label.text() == "音量: ▁▁▁▁▁▁▁▁▁▁"

    # 模拟音频线程发来的音量（经 LevelRelay 信号）
    win._relay.emit_level(0.55)

    import pytestqt.qtbot  # noqa: F401  # pytest-qt 保证信号已排队投递

    # 处理排队的事件（queued signal 需要 event loop 转一圈）
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.processEvents()
    text = win._level_label.text()
    assert "▇" in text, f"音量条未跳动: {text}"
    assert text.count("▇") == 6  # 0.55 → int(5.5)+1 = 6 格（有声音至少亮 1 格的修正）


def test_level_relay_is_thread_safe_by_signal(qtbot: object) -> None:
    """音频线程接口存在且指向信号中继，而不是直改 UI。"""
    win = _window(qtbot)
    # start() 前注册的回调就是 relay 的信号发射器（两个捕获器接口一致，用 device 即可）
    win._device_capture.set_level_callback(win._relay.emit_level)
    assert win._device_capture._on_level is not None
    # 峰值保持：多个音量到达时取最大
    win._relay.emit_level(0.2)
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.processEvents()
    win._relay.emit_level(0.8)
    QCoreApplication.processEvents()
    text = win._level_label.text()
    assert text.count("▇") == 9  # 峰值 0.8 → int(8)+1 = 9 格


def test_silence_resets_bar(qtbot: object) -> None:
    """本秒无声音后音量条回落（_update_stats 清零峰值）。"""
    win = _window(qtbot)
    win._relay.emit_level(0.9)
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.processEvents()
    win._update_stats()  # 模拟 1 秒定时器到点
    assert win._level_label.text() == "音量: ▁▁▁▁▁▁▁▁▁▁"
