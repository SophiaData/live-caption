"""功能 #7/#8 测试：字幕窗样式/拖动状态、设置常量、托盘图标生成。"""

import pytest

from app.ui.subtitle_window import (
    MAX_FONT,
    MAX_OPACITY,
    MIN_FONT,
    MIN_OPACITY,
    SubtitleWindow,
)


@pytest.fixture
def win(qtbot: object) -> SubtitleWindow:
    w = SubtitleWindow()
    return w


class TestSubtitleStyle:
    def test_default_font_and_opacity(self, win: SubtitleWindow) -> None:
        assert MIN_FONT <= win.font_size <= MAX_FONT
        assert MIN_OPACITY <= win.opacity <= MAX_OPACITY

    def test_apply_font_clamped(self, win: SubtitleWindow) -> None:
        """字号超范围被夹紧。"""
        win.apply_style(font_size=999)
        assert win.font_size == MAX_FONT
        win.apply_style(font_size=1)
        assert win.font_size == MIN_FONT

    def test_apply_opacity_clamped(self, win: SubtitleWindow) -> None:
        win.apply_style(opacity=5.0)
        assert win.opacity == MAX_OPACITY
        win.apply_style(opacity=-1.0)
        assert win.opacity == MIN_OPACITY

    def test_apply_style_updates_stylesheet(self, win: SubtitleWindow) -> None:
        win.apply_style(font_size=40)
        assert "40px" in win._trans_label.styleSheet()


class TestTextDisplay:
    def test_bilingual_both_labels(self, win: SubtitleWindow) -> None:
        win.show_text("hello", "你好")
        # Signal 同步投递（同线程直连）
        assert win._trans_label.text() == "你好"
        assert win._orig_label.text() == "hello"
        assert win._orig_label.isVisibleTo(win)

    def test_translation_empty_shows_only_original(self, win: SubtitleWindow) -> None:
        win.show_text("hello", "")
        assert win._trans_label.text() == "hello"
        # 原文 label 隐藏
        assert not win._orig_label.isVisibleTo(win)


class TestPosition:
    def test_restore_and_read_position(self, win: SubtitleWindow) -> None:
        win.restore_position(100, 200)
        assert win.current_pos() == (100, 200)


class TestTrayIcon:
    def test_make_icon_returns_icon(self) -> None:
        from app.ui.tray import _make_icon

        icon = _make_icon("#E74C3C")
        assert not icon.isNull()
