"""确定性测试：四个设备/界面装配层模块。"""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.capture import capture, sck_session
from app.capture.sck import SCKCaptureError
from app.ui import config_wizard, subtitle_settings


class FakeInputStream:
    instances: list["FakeInputStream"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True
        callback = self.kwargs["callback"]
        callback(np.array([[0.25, -0.1], [0.0, 0.1]], dtype=np.float32), 2, None, None)

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def test_system_capture_start_delivers_block_and_level(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeInputStream.instances.clear()
    monkeypatch.setattr(
        capture.sd, "query_devices", lambda: [{"name": "BlackHole", "max_input_channels": 2}]
    )
    monkeypatch.setattr(capture.sd, "InputStream", FakeInputStream)
    levels: list[float] = []
    blocks: list[np.ndarray] = []
    cap = capture.SystemAudioCapture()
    cap.set_level_callback(levels.append)
    cap.set_block_callback(blocks.append)

    device = cap.start(device_index=0)

    assert device.name == "BlackHole"
    assert cap.running
    assert cap.blocks_received == 1
    np.testing.assert_array_equal(cap.pop_audio(), [np.array([[0.25], [0.0]], dtype=np.float32)])
    np.testing.assert_array_equal(blocks, [np.array([0.25, 0.0], dtype=np.float32)])
    assert levels == [pytest.approx(0.25)]
    assert FakeInputStream.instances[0].kwargs["device"] == 0


def test_system_capture_stop_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        capture.sd, "query_devices", lambda: [{"name": "Loopback", "max_input_channels": 2}]
    )
    monkeypatch.setattr(capture.sd, "InputStream", FakeInputStream)
    cap = capture.SystemAudioCapture()
    cap.start()
    stream = FakeInputStream.instances[-1]

    cap.stop()
    cap.stop()

    assert not cap.running
    assert stream.stopped and stream.closed


def test_system_capture_rejects_missing_and_duplicate_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capture.sd, "query_devices", lambda: [{"name": "Microphone", "max_input_channels": 1}]
    )
    monkeypatch.setattr(capture.sd, "InputStream", FakeInputStream)
    cap = capture.SystemAudioCapture()
    with pytest.raises(capture.CaptureError, match="BlackHole"):
        cap.start()

    monkeypatch.setattr(
        capture.sd, "query_devices", lambda: [{"name": "BlackHole", "max_input_channels": 2}]
    )
    cap.start()
    with pytest.raises(capture.CaptureError, match="已在进行中"):
        cap.start()
    cap.stop()


def test_system_capture_open_failure_clears_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        capture.sd, "query_devices", lambda: [{"name": "BlackHole", "max_input_channels": 2}]
    )

    class FailingStream:
        def __init__(self, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("device busy")

    monkeypatch.setattr(capture.sd, "InputStream", FailingStream)
    cap = capture.SystemAudioCapture()
    with pytest.raises(capture.CaptureError, match="设备「BlackHole」失败"):
        cap.start()
    assert not cap.running


class FakeSCKStream:
    def __init__(self) -> None:
        self.stopped = False
        self.output_calls = 0

    def addStreamOutput_type_sampleHandlerQueue_error_(self, *args: object) -> None:
        self.output_calls += 1
        return None

    def startCaptureWithCompletionHandler_(self, callback: object) -> None:
        callback(None)  # type: ignore[operator]

    def stopCaptureWithCompletionHandler_(self, callback: object) -> None:
        self.stopped = True
        callback(None)  # type: ignore[operator]


class FakeAllocated:
    @classmethod
    def alloc(cls) -> "FakeAllocated":
        return cls()

    def init(self) -> "FakeAllocated":
        return self


class FakeSCKConfiguration(FakeAllocated):
    def setCapturesAudio_(self, value: bool) -> None:
        self.captures_audio = value

    def setExcludesCurrentProcessAudio_(self, value: bool) -> None:
        self.excludes_audio = value

    def setSampleRate_(self, value: int) -> None:
        self.sample_rate = value

    def setQueueDepth_(self, value: int) -> None:
        self.queue_depth = value


class FakeFilter(FakeAllocated):
    def initWithDisplay_excludingApplications_exceptingWindows_(
        self, *args: object
    ) -> "FakeFilter":
        return self


class FakeSCKStreamFactory(FakeAllocated):
    stream = FakeSCKStream()

    def initWithFilter_configuration_delegate_(self, *args: object) -> FakeSCKStream:
        self.__class__.stream = FakeSCKStream()
        return self.__class__.stream


class FakeShareableContent:
    @staticmethod
    def getShareableContentWithCompletionHandler_(callback: object) -> None:
        callback(SimpleNamespace(displays=lambda: [object()]), None)  # type: ignore[operator]


class FakeDelegate:
    @classmethod
    def alloc(cls) -> "FakeDelegate":
        return cls()

    def initWithCallback_(self, callback: object) -> "FakeDelegate":
        self.callback = callback
        return self


def fake_sck_modules() -> dict[str, object]:
    return {
        "Quartz": SimpleNamespace(
            CGPreflightScreenCaptureAccess=lambda: True,
            CGRequestScreenCaptureAccess=lambda: True,
        ),
        "sck": SimpleNamespace(
            SCShareableContent=FakeShareableContent,
            SCContentFilter=FakeFilter,
            SCStreamConfiguration=FakeSCKConfiguration,
            SCStream=FakeSCKStreamFactory,
        ),
    }


def test_sck_session_start_stop_uses_injected_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    blocks: list[tuple[np.ndarray, float]] = []
    monkeypatch.setattr(sck_session, "_sck_modules", fake_sck_modules)
    monkeypatch.setattr(sck_session, "_make_delegate_class", lambda: FakeDelegate)
    session = sck_session.SCKSession(lambda block, level: blocks.append((block, level)))

    session.start()
    assert session._stream is not None
    assert FakeSCKStreamFactory.stream.output_calls == 1
    session.stop()
    assert session._stream is None
    assert FakeSCKStreamFactory.stream.stopped


def test_sck_session_content_error_is_user_facing(monkeypatch: pytest.MonkeyPatch) -> None:
    class ErrorContent:
        @staticmethod
        def getShareableContentWithCompletionHandler_(callback: object) -> None:
            callback(None, "permission denied")  # type: ignore[operator]

    mods = fake_sck_modules()
    mods["sck"] = SimpleNamespace(SCShareableContent=ErrorContent)
    monkeypatch.setattr(sck_session, "_sck_modules", lambda: mods)
    session = sck_session.SCKSession(lambda _block, _level: None)

    with pytest.raises(SCKCaptureError, match="获取屏幕内容失败"):
        session.start()


def test_sck_delegate_audio_frame_resamples_and_reports_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[np.ndarray, float]] = []
    session = sck_session.SCKSession(lambda block, level: received.append((block, level)))

    session._on_audio_frame(np.ones(4800, dtype=np.float32), 48000)

    assert len(received[0][0]) == pytest.approx(1600, abs=2)
    assert received[0][1] == pytest.approx(1.0)


def test_sck_session_stop_before_start_is_safe() -> None:
    sck_session.SCKSession(lambda _block, _level: None).stop()


@dataclass
class StoredConfig:
    base_url: str
    model: str


def test_config_wizard_defaults_and_password_fields(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_wizard, "load_config", lambda _slot: None)
    dialog = config_wizard.ConfigWizard()
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    assert dialog.windowTitle() == "live-caption · 模型配置"
    assert dialog._asr_status.text() == "未测试"
    assert dialog._save_btn.isEnabled() is False
    assert dialog._asr_key.echoMode() == dialog._asr_key.EchoMode.Password
    assert dialog._trans_key.echoMode() == dialog._trans_key.EchoMode.Password


def test_config_wizard_existing_values_are_prefilled_without_key(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = {
        "asr": StoredConfig("https://asr.test/v1", "whisper-test"),
        "translate": StoredConfig("https://translate.test/v1", "chat-test"),
    }
    monkeypatch.setattr(config_wizard, "load_config", configs.__getitem__)
    dialog = config_wizard.ConfigWizard()
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    assert dialog._asr_url.text() == "https://asr.test/v1"
    assert dialog._asr_model.text() == "whisper-test"
    assert dialog._asr_key.placeholderText() == "密钥已保存（改的话重新填）"
    assert dialog._trans_url.text() == "https://translate.test/v1"
    assert dialog._trans_model.text() == "chat-test"


def test_config_wizard_missing_slot_fields_show_error(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_wizard, "load_config", lambda _slot: None)
    dialog = config_wizard.ConfigWizard()
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    dialog._test_slot(
        "asr", dialog._asr_url, dialog._asr_key, dialog._asr_model, dialog._asr_status
    )

    assert "请先填完" in dialog._asr_status.text()
    assert dialog._save_btn.isEnabled() is False


def test_config_wizard_save_requires_both_successes(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_wizard, "load_config", lambda _slot: None)
    dialog = config_wizard.ConfigWizard()
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    dialog._on_test_done(True, "连接成功", dialog._asr_status)
    assert dialog._save_btn.isEnabled() is False
    dialog._on_test_done(True, "连接成功", dialog._trans_status)
    assert dialog._save_btn.isEnabled() is True


def test_subtitle_settings_exposes_subtitle_ranges(qtbot: object) -> None:
    subtitle = MagicMock(font_size=30, opacity=0.9)
    dialog = subtitle_settings.SubtitleSettingsDialog(subtitle)
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    assert dialog._font_slider.minimum() == subtitle_settings.MIN_FONT
    assert dialog._font_slider.maximum() == subtitle_settings.MAX_FONT
    assert dialog._font_slider.value() == 30
    assert dialog._opacity_slider.minimum() == int(subtitle_settings.MIN_OPACITY * 100)
    assert dialog._opacity_slider.maximum() == int(subtitle_settings.MAX_OPACITY * 100)
    assert dialog._opacity_slider.value() == 90


def test_subtitle_settings_font_slider_applies_style(qtbot: object) -> None:
    subtitle = MagicMock(font_size=30, opacity=0.9)
    dialog = subtitle_settings.SubtitleSettingsDialog(subtitle)
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    dialog._font_slider.setValue(42)

    subtitle.apply_style.assert_called_with(font_size=42)
    assert dialog._font_val.text() == "42px"


def test_subtitle_settings_opacity_slider_applies_fraction(qtbot: object) -> None:
    subtitle = MagicMock(font_size=30, opacity=0.9)
    dialog = subtitle_settings.SubtitleSettingsDialog(subtitle)
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    dialog._opacity_slider.setValue(55)

    subtitle.apply_style.assert_called_with(opacity=0.55)
    assert dialog._opacity_val.text() == "55%"


def test_subtitle_settings_reset_emits_current_position(qtbot: object) -> None:
    subtitle = MagicMock(font_size=30, opacity=0.9)
    subtitle.current_pos.return_value = (12, 34)
    dialog = subtitle_settings.SubtitleSettingsDialog(subtitle)
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    dialog._reset_pos_btn.click()

    subtitle._position_at_bottom.assert_called_once_with()
    subtitle.position_changed.emit.assert_called_once_with(12, 34)
