"""确定性测试：四个设备/界面装配层模块。"""

import ctypes
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal

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
    instances: list["FakeSCKStream"] = []

    def __init__(self) -> None:
        self.stopped = False
        self.output_calls = 0
        self.output_type: object = None
        self.__class__.instances.append(self)

    def addStreamOutput_type_sampleHandlerQueue_error_(self, *args: object) -> None:
        self.output_calls += 1
        self.output_type = args[1]
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
    instance: "FakeSCKConfiguration | None" = None

    def __init__(self) -> None:
        self.captures_audio: bool | None = None
        self.excludes_audio: bool | None = None
        self.sample_rate: int | None = None
        self.queue_depth: int | None = None
        self.__class__.instance = self

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


PCM_48K_STEREO = np.ones(4800 * 2, dtype=np.float32).tobytes()


class _FakeCoreMedia:
    """CoreMedia 替身：48kHz、双声道交错的 4800 帧满量程 float32 PCM。"""

    @staticmethod
    def CMSampleBufferGetFormatDescription(_sample_buffer: object) -> object:
        return "format-description"

    @staticmethod
    def CMAudioFormatDescriptionGetStreamBasicDescription(_fmt: object) -> tuple[int, ...]:
        return (48000, 0, 0, 0, 0, 0, 2)

    @staticmethod
    def CMSampleBufferGetDataBuffer(_sample_buffer: object) -> object:
        return "block-buffer"

    @staticmethod
    def CMBlockBufferGetDataLength(_block_buffer: object) -> int:
        return len(PCM_48K_STEREO)

    @staticmethod
    def CMBlockBufferCopyDataBytes(
        _block_buffer: object, _offset: int, length: int, destination: object
    ) -> tuple[int]:
        ctypes.memmove(destination, PCM_48K_STEREO[:length], length)
        return (0,)


class _JumpingClock:
    """每读一次就跳过 100 秒的假单调时钟：让 _pump_until 的超时立即到期，不真等。"""

    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        value = self._now
        self._now += 100.0
        return value


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
    FakeSCKStream.instances.clear()
    FakeSCKConfiguration.instance = None
    monkeypatch.setattr(sck_session, "_sck_modules", fake_sck_modules)
    monkeypatch.setattr(sck_session, "_make_delegate_class", lambda: FakeDelegate)
    session = sck_session.SCKSession(lambda block, level: blocks.append((block, level)))

    session.start()
    assert session._stream is not None
    config = FakeSCKConfiguration.instance
    assert config is not None
    assert config.captures_audio is True
    assert config.excludes_audio is False
    assert config.sample_rate == sck_session.TARGET_SAMPLE_RATE
    assert config.queue_depth == 3
    stream = FakeSCKStream.instances[-1]
    assert stream.output_calls == 1
    assert stream.output_type == sck_session.SCK_OUTPUT_TYPE_AUDIO
    session.stop()
    assert session._stream is None
    assert stream.stopped


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


@pytest.fixture(scope="module")
def real_delegate_cls() -> object:
    """真实的 PyObjC delegate 类（ObjC 类名每进程只能注册一次，走生产缓存）。"""
    return sck_session._make_delegate_class()


def test_sck_delegate_audio_frame_resamples_and_reports_level(
    monkeypatch: pytest.MonkeyPatch, real_delegate_cls: object
) -> None:
    received: list[tuple[np.ndarray, float]] = []
    session = sck_session.SCKSession(lambda block, level: received.append((block, level)))
    monkeypatch.setattr(
        sck_session,
        "extract_audio_samples",
        lambda _sample_buffer: (np.ones(4800, dtype=np.float32), 48000),
    )
    delegate = real_delegate_cls.alloc().initWithCallback_(session._on_audio_frame)  # type: ignore[attr-defined]

    delegate.stream_didOutputSampleBuffer_ofType_(None, object(), 0)
    assert received == []

    delegate.stream_didOutputSampleBuffer_ofType_(None, object(), sck_session.SCK_OUTPUT_TYPE_AUDIO)
    assert len(received[0][0]) == pytest.approx(1600, abs=2)
    assert received[0][1] == pytest.approx(1.0)


def test_sck_delegate_forwards_sample_buffer_through_real_parse(
    monkeypatch: pytest.MonkeyPatch, real_delegate_cls: object
) -> None:
    """真链路：sample buffer → 真实 extract_audio_samples → delegate → on_block。

    CoreMedia 用替身模块（sys.modules 注入），不碰真实音频设备；
    extract_audio_samples 与 delegate 类体都是生产代码，未被 patch。
    """
    received: list[tuple[np.ndarray, float]] = []
    session = sck_session.SCKSession(lambda block, level: received.append((block, level)))
    monkeypatch.setitem(sys.modules, "CoreMedia", _FakeCoreMedia)
    delegate = real_delegate_cls.alloc().initWithCallback_(session._on_audio_frame)  # type: ignore[attr-defined]

    delegate.stream_didOutputSampleBuffer_ofType_(None, object(), 0)
    assert received == []

    delegate.stream_didOutputSampleBuffer_ofType_(None, object(), sck_session.SCK_OUTPUT_TYPE_AUDIO)
    assert len(received) == 1
    # 48kHz / 双声道交错的 4800 帧 → 单声道 4800 点 → 重采样后约 1600 点，满量程
    assert len(received[0][0]) == pytest.approx(1600, abs=2)
    assert received[0][1] == pytest.approx(1.0)


def test_sck_session_add_output_error_stops_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    class ErrorStream(FakeSCKStream):
        def addStreamOutput_type_sampleHandlerQueue_error_(self, *args: object) -> tuple[bool, str]:
            return False, "output failed"

    class ErrorStreamFactory(FakeAllocated):
        stream: ErrorStream

        def initWithFilter_configuration_delegate_(self, *args: object) -> ErrorStream:
            self.__class__.stream = ErrorStream()
            return self.__class__.stream

    mods = fake_sck_modules()
    mods["sck"] = SimpleNamespace(
        SCShareableContent=FakeShareableContent,
        SCContentFilter=FakeFilter,
        SCStreamConfiguration=FakeSCKConfiguration,
        SCStream=ErrorStreamFactory,
    )
    monkeypatch.setattr(sck_session, "_sck_modules", lambda: mods)
    monkeypatch.setattr(sck_session, "_make_delegate_class", lambda: FakeDelegate)

    with pytest.raises(SCKCaptureError, match="挂载音频输出失败：output failed"):
        sck_session.SCKSession(lambda _block, _level: None).start()
    assert ErrorStreamFactory.stream.stopped


def test_sck_session_add_output_signature_error_falls_back_to_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """addStreamOutput 调用形式异常时不崩，交给后续「启动捕获超时」暴露。"""

    class SignatureErrorStream(FakeSCKStream):
        def addStreamOutput_type_sampleHandlerQueue_error_(self, *args: object) -> None:
            raise RuntimeError("unrecognized selector")

        def startCaptureWithCompletionHandler_(self, _callback: object) -> None:
            return None

    class SignatureErrorStreamFactory(FakeAllocated):
        stream: SignatureErrorStream

        def initWithFilter_configuration_delegate_(self, *args: object) -> SignatureErrorStream:
            self.__class__.stream = SignatureErrorStream()
            return self.__class__.stream

    mods = fake_sck_modules()
    mods["sck"] = SimpleNamespace(
        SCShareableContent=FakeShareableContent,
        SCContentFilter=FakeFilter,
        SCStreamConfiguration=FakeSCKConfiguration,
        SCStream=SignatureErrorStreamFactory,
    )
    monkeypatch.setattr(sck_session, "_sck_modules", lambda: mods)
    monkeypatch.setattr(sck_session, "_make_delegate_class", lambda: FakeDelegate)
    monkeypatch.setattr(time, "monotonic", _JumpingClock())

    with pytest.raises(SCKCaptureError, match="启动捕获超时"):
        sck_session.SCKSession(lambda _block, _level: None).start()
    assert SignatureErrorStreamFactory.stream.stopped


def test_sck_session_start_timeout_stops_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingStream(FakeSCKStream):
        def startCaptureWithCompletionHandler_(self, _callback: object) -> None:
            return None

    class HangingStreamFactory(FakeAllocated):
        stream: HangingStream

        def initWithFilter_configuration_delegate_(self, *args: object) -> HangingStream:
            self.__class__.stream = HangingStream()
            return self.__class__.stream

    mods = fake_sck_modules()
    mods["sck"] = SimpleNamespace(
        SCShareableContent=FakeShareableContent,
        SCContentFilter=FakeFilter,
        SCStreamConfiguration=FakeSCKConfiguration,
        SCStream=HangingStreamFactory,
    )
    monkeypatch.setattr(sck_session, "_sck_modules", lambda: mods)
    monkeypatch.setattr(sck_session, "_make_delegate_class", lambda: FakeDelegate)
    monkeypatch.setattr(time, "monotonic", _JumpingClock())

    with pytest.raises(SCKCaptureError, match="启动捕获超时"):
        sck_session.SCKSession(lambda _block, _level: None).start()
    assert HangingStreamFactory.stream.stopped


def test_sck_session_content_timeout_is_user_facing(monkeypatch: pytest.MonkeyPatch) -> None:
    class SilentContent:
        @staticmethod
        def getShareableContentWithCompletionHandler_(_callback: object) -> None:
            return None

    mods = fake_sck_modules()
    mods["sck"] = SimpleNamespace(SCShareableContent=SilentContent)
    monkeypatch.setattr(sck_session, "_sck_modules", lambda: mods)
    monkeypatch.setattr(time, "monotonic", _JumpingClock())

    with pytest.raises(SCKCaptureError, match="获取屏幕内容超时"):
        sck_session.SCKSession(lambda _block, _level: None).start()


def test_sck_session_stop_before_start_is_safe() -> None:
    sck_session.SCKSession(lambda _block, _level: None).stop()


@dataclass
class StoredConfig:
    base_url: str
    model: str


class ImmediateWorker(QObject):
    """假连接测试线程：start() 同步真实 emit done 信号（不起线程、不联网）。"""

    done = Signal(bool, str)

    def __init__(self, _config: object, mode: str) -> None:
        super().__init__()
        self._mode = mode

    def start(self) -> None:
        # 识别槽位必须映射到 "audio"，翻译槽位到 "chat"；映射写错时这里就会走失败分支
        if self._mode == "audio":
            self.done.emit(True, "连接成功")
        else:
            self.done.emit(False, "密钥无效（401）")


class SilentWorker(QObject):
    """假连接测试线程：记录被创建，但绝不启动真线程、不发信号、不联网。"""

    done = Signal(bool, str)
    created: list["SilentWorker"] = []

    def __init__(self, _config: object, _mode: str) -> None:
        super().__init__()
        self.__class__.created.append(self)

    def start(self) -> None:
        return None


def test_config_wizard_worker_done_signal_updates_status(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_wizard, "load_config", lambda _slot: None)
    monkeypatch.setattr(config_wizard, "TestConnectionWorker", ImmediateWorker)
    dialog = config_wizard.ConfigWizard()
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    dialog._asr_url.setText("https://asr.test")
    dialog._asr_key.setText("test-key")
    dialog._asr_model.setText("test-model")

    dialog._asr_test_btn.click()

    assert dialog._asr_status.text() == "✅ 连接成功"


def test_config_wizard_worker_done_signal_shows_failure_and_keeps_save_disabled(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败分支同样经真信号投递：❌ 文案来自 msg，保存按钮保持禁用。"""
    monkeypatch.setattr(config_wizard, "load_config", lambda _slot: None)
    monkeypatch.setattr(config_wizard, "TestConnectionWorker", ImmediateWorker)
    dialog = config_wizard.ConfigWizard()
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    dialog._asr_url.setText("https://asr.test")
    dialog._asr_key.setText("test-key")
    dialog._asr_model.setText("test-model")
    dialog._trans_url.setText("https://translate.test")
    dialog._trans_key.setText("translate-key")
    dialog._trans_model.setText("translate-model")

    dialog._trans_test_btn.click()

    assert dialog._trans_status.text() == "❌ 密钥无效（401）"
    assert dialog._asr_status.text() == "未测试"
    assert dialog._save_btn.isEnabled() is False


def test_config_wizard_save_button_signal_persists_both_slots(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_config = MagicMock()
    monkeypatch.setattr(config_wizard, "load_config", lambda _slot: None)
    monkeypatch.setattr(config_wizard, "save_config", save_config)
    dialog = config_wizard.ConfigWizard()
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    dialog._asr_url.setText("https://asr.test")
    dialog._asr_key.setText("asr-key")
    dialog._asr_model.setText("asr-model")
    dialog._trans_url.setText("https://translate.test")
    dialog._trans_key.setText("translate-key")
    dialog._trans_model.setText("translate-model")
    dialog._on_test_done(True, "连接成功", dialog._asr_status)
    dialog._on_test_done(True, "连接成功", dialog._trans_status)

    dialog._save_btn.click()

    assert save_config.call_args_list == [
        call("asr", "https://asr.test", "asr-model", "asr-key"),
        call("translate", "https://translate.test", "translate-model", "translate-key"),
    ]
    assert dialog.result() == dialog.DialogCode.Accepted


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


@pytest.mark.parametrize(
    ("url", "key", "model"),
    [
        ("", "test-key", "test-model"),
        ("https://test", "", "test-model"),
        ("https://test", "test-key", ""),
    ],
)
def test_config_wizard_missing_slot_fields_show_error(
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    key: str,
    model: str,
) -> None:
    monkeypatch.setattr(config_wizard, "load_config", lambda _slot: None)
    monkeypatch.setattr(config_wizard, "TestConnectionWorker", SilentWorker)
    SilentWorker.created.clear()
    dialog = config_wizard.ConfigWizard()
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    dialog._asr_url.setText(url)
    dialog._asr_key.setText(key)
    dialog._asr_model.setText(model)
    dialog._test_slot(
        "asr", dialog._asr_url, dialog._asr_key, dialog._asr_model, dialog._asr_status
    )

    assert "请先填完" in dialog._asr_status.text()
    assert dialog._save_btn.isEnabled() is False
    assert SilentWorker.created == []


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
