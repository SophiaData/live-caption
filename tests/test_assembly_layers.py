"""确定性测试：四个设备/界面装配层模块。"""

import ctypes
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import numpy as np
import pytest
from PySide6.QtCore import QObject, QTimer, Signal

from app.capture import capture, sck_session
from app.capture.sck import SCKCaptureError
from app.providers.openai_compatible import ModelConfig
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
        FakeSCKStream.instances.append(self)

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
        FakeSCKConfiguration.instance = self

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
    """CoreMedia 替身：48kHz、双声道**分声道连续**（planar）的 4800 帧满量程 float32 PCM。

    ASBD 用**真机实测的形状**（Issue #22，探针输出见 docs/qa/issue-22/logs/
    frame-format-measured.txt）：PyObjC 13 把 AudioStreamBasicDescription 摊平成
    9 元素元组 [采样率, formatID('lpcm'), flags, bytesPerPacket, framesPerPacket,
    bytesPerFrame, channels, bitsPerChannel, reserved]。以前这里写的是 7 元素
    (48000, 0, 0, 0, 0, 0, 2)——formatID/位深全 0，正是「假件不像真机」的一部分。
    真机帧的 mediaType='soun'，所以替身也得给（不给自己格式类型会被解析当成非音频挡掉）。

    带令牌校验：只认测试指定的那块 sample_buffer；生产如果把别的对象（例如 stream）
    传进解析，这里就返回 None，按「非音频帧」跳过。copy_status 非 0 模拟 PCM 拷贝失败。
    """

    sample_buffer: object = None
    copy_status: int = 0
    data_length: int = len(PCM_48K_STEREO)
    _FMT: object = object()
    _BLOCK: object = object()

    @classmethod
    def CMSampleBufferGetFormatDescription(cls, sample_buffer: object) -> object:
        return cls._FMT if sample_buffer is cls.sample_buffer else None

    @classmethod
    def CMAudioFormatDescriptionGetStreamBasicDescription(cls, fmt: object) -> object:
        if fmt is not cls._FMT:
            return None
        # 真机口径：48kHz / 'lpcm' / flags=41（IsFloat|IsPacked|IsNonInterleaved）/
        # 每声道 4 字节 / 2 声道 / 32 位。数据是全 1，交错与 planar 读出来一样，
        # 但 ASBD 必须自洽（flags 说 planar 却报 8 字节一帧会被解析判成「互相矛盾」跳帧）。
        return (48000, 1819304813, 41, 4, 1, 4, 2, 32, 0)

    @classmethod
    def CMFormatDescriptionGetMediaType(cls, fmt: object) -> int:
        return 1936684398 if fmt is cls._FMT else 0  # 'soun'

    @classmethod
    def CMSampleBufferGetDataBuffer(cls, sample_buffer: object) -> object:
        return cls._BLOCK if sample_buffer is cls.sample_buffer else None

    @classmethod
    def CMBlockBufferGetDataLength(cls, block_buffer: object) -> int:
        return cls.data_length if block_buffer is cls._BLOCK else 0

    @classmethod
    def CMBlockBufferCopyDataBytes(
        cls, block_buffer: object, _offset: int, length: int, destination: object
    ) -> tuple[int]:
        if block_buffer is not cls._BLOCK or cls.copy_status != 0:
            return (1,)
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


class _HoldingClock:
    """前 hold 次读到同一时刻（逼 _pump_until 真的转一圈），之后一次越过 deadline。"""

    def __init__(self, hold: int = 5) -> None:
        self._hold = hold
        self._reads = 0

    def __call__(self) -> float:
        self._reads += 1
        return 1000.0 if self._reads <= self._hold else 10_000.0


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    """替身的类级状态每条用例都归零，避免跨用例残留（子类共享同一个列表/实例）。"""
    FakeSCKStream.instances.clear()
    FakeSCKConfiguration.instance = None
    SilentWorker.created.clear()
    _FakeCoreMedia.sample_buffer = None
    _FakeCoreMedia.copy_status = 0
    _FakeCoreMedia.data_length = len(PCM_48K_STEREO)


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
    config = FakeSCKConfiguration.instance
    assert config is not None
    # 契约值写死（不引用常量，否则常量被改坏时这里是恒等式）：
    # 抓系统声音、排除本进程声音（TD-7 裁决，Issue #14）、请求 16k、queueDepth=3 保低延迟
    assert config.captures_audio is True
    assert config.excludes_audio is True  # TD-7 已清账；另有专测钉「生产确实传了 True」
    assert config.sample_rate == 16000
    assert config.queue_depth == 3
    stream = FakeSCKStream.instances[-1]
    assert stream.output_calls == 1
    assert stream.output_type == 1  # Apple 契约：SCStreamOutputTypeAudio = 1
    session.stop()
    assert session._stream is None
    assert stream.stopped


def test_sck_session_start_excludes_current_process_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """TD-7 裁决（Issue #14）：钉「生产确实传了 True」，不是常量间恒等式。

    用独立记录器只盯 setExcludesCurrentProcessAudio_ 的实参：把 sck_session.py 里的
    True 改回 False、删掉这行调用、或改成重复调用，本用例都会变红。
    """
    calls: list[bool] = []

    class RecordingConfig(FakeSCKConfiguration):
        def setExcludesCurrentProcessAudio_(self, value: bool) -> None:
            calls.append(value)

    mods = fake_sck_modules()
    mods["sck"] = SimpleNamespace(
        SCShareableContent=FakeShareableContent,
        SCContentFilter=FakeFilter,
        SCStreamConfiguration=RecordingConfig,
        SCStream=FakeSCKStreamFactory,
    )
    monkeypatch.setattr(sck_session, "_sck_modules", lambda: mods)
    monkeypatch.setattr(sck_session, "_make_delegate_class", lambda: FakeDelegate)
    session = sck_session.SCKSession(lambda block, level: None)

    session.start()
    assert calls == [True]  # 恰好一次调用，且实参为 True
    session.stop()


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

    delegate.stream_didOutputSampleBuffer_ofType_(None, object(), 1)  # SCStreamOutputTypeAudio
    assert len(received[0][0]) == pytest.approx(1600, abs=2)
    assert received[0][1] == pytest.approx(1.0)


def test_sck_delegate_forwards_sample_buffer_through_real_parse(
    monkeypatch: pytest.MonkeyPatch, real_delegate_cls: object
) -> None:
    """真链路：sample buffer → 真实 extract_audio_samples → delegate → on_block。

    只有 CoreMedia 这层 C 绑定是替身（sys.modules 注入，带令牌校验）；
    delegate 类体、extract_audio_samples、_interleaved_to_mono、resample_to_16k
    全是生产代码，未被 patch。
    """
    received: list[tuple[np.ndarray, float]] = []
    session = sck_session.SCKSession(lambda block, level: received.append((block, level)))
    monkeypatch.setitem(sys.modules, "CoreMedia", _FakeCoreMedia)
    buffer = object()
    _FakeCoreMedia.sample_buffer = buffer

    # selector 必须真注册在 ObjC 类上（历史上方法放在纯 Python 基类里 → PyObjC 不注册 → 收不到帧）
    assert real_delegate_cls.instancesRespondToSelector_(  # type: ignore[attr-defined]
        "stream:didOutputSampleBuffer:ofType:"
    )

    delegate = real_delegate_cls.alloc().initWithCallback_(session._on_audio_frame)  # type: ignore[attr-defined]

    delegate.stream_didOutputSampleBuffer_ofType_(object(), buffer, 0)
    assert received == []

    delegate.stream_didOutputSampleBuffer_ofType_(object(), buffer, 1)  # SCStreamOutputTypeAudio
    assert len(received) == 1
    # 48kHz / 双声道交错的 4800 帧 → 单声道 4800 点 → 重采样后约 1600 点，满量程
    assert len(received[0][0]) == pytest.approx(1600, abs=2)
    assert received[0][1] == pytest.approx(1.0)


def test_sck_delegate_skips_unparsable_frame_without_raising(
    monkeypatch: pytest.MonkeyPatch, real_delegate_cls: object
) -> None:
    """解析失败（PCM 拷贝返回非 0）时该帧静默跳过：不转发、也不把流搞崩。"""
    received: list[tuple[np.ndarray, float]] = []
    session = sck_session.SCKSession(lambda block, level: received.append((block, level)))
    monkeypatch.setitem(sys.modules, "CoreMedia", _FakeCoreMedia)
    _FakeCoreMedia.sample_buffer = object()
    _FakeCoreMedia.copy_status = 1
    delegate = real_delegate_cls.alloc().initWithCallback_(session._on_audio_frame)  # type: ignore[attr-defined]

    delegate.stream_didOutputSampleBuffer_ofType_(object(), _FakeCoreMedia.sample_buffer, 1)

    assert received == []


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


def test_sck_session_pump_drives_event_loop_until_callback_lands(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_pump_until` 必须真的在泵事件循环。

    完成回调由 QTimer 投递，只有泵体被执行才落地：删掉 sck_session.py:186 的
    `app.processEvents()`（生产注释里标「关键」的那一行）→ 回调永远收不到 → 本用例变红。
    假时钟限定泵体最多转 50 圈，绝不真等 15 秒。
    """

    class DeferredStartStream(FakeSCKStream):
        def startCaptureWithCompletionHandler_(self, callback: object) -> None:
            QTimer.singleShot(0, lambda: callback(None))  # type: ignore[misc]

    class DeferredStreamFactory(FakeAllocated):
        stream: DeferredStartStream

        def initWithFilter_configuration_delegate_(self, *args: object) -> DeferredStartStream:
            self.__class__.stream = DeferredStartStream()
            return self.__class__.stream

    mods = fake_sck_modules()
    mods["sck"] = SimpleNamespace(
        SCShareableContent=FakeShareableContent,
        SCContentFilter=FakeFilter,
        SCStreamConfiguration=FakeSCKConfiguration,
        SCStream=DeferredStreamFactory,
    )
    monkeypatch.setattr(sck_session, "_sck_modules", lambda: mods)
    monkeypatch.setattr(sck_session, "_make_delegate_class", lambda: FakeDelegate)
    monkeypatch.setattr(time, "monotonic", _HoldingClock(hold=50))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    session = sck_session.SCKSession(lambda _block, _level: None)

    session.start()

    assert session._stream is DeferredStreamFactory.stream
    assert session._stream.stopped is False


def test_sck_session_stop_before_start_is_safe() -> None:
    sck_session.SCKSession(lambda _block, _level: None).stop()


@dataclass
class StoredConfig:
    base_url: str
    model: str


class ImmediateWorker(QObject):
    """假连接测试线程：start() 同步真实 emit done 信号（不起线程、不联网）。"""

    done = Signal(bool, str)
    created: list["ImmediateWorker"] = []

    def __init__(self, config: object, mode: str) -> None:
        super().__init__()
        self.config = config
        self.mode = mode
        ImmediateWorker.created.append(self)

    def start(self) -> None:
        # 识别槽位必须映射到 "audio"，翻译槽位到 "chat"；映射写错时这里就会走失败分支
        if self.mode == "audio":
            self.done.emit(True, "连接成功")
        else:
            self.done.emit(False, "密钥无效（401）")


class SilentWorker(QObject):
    """假连接测试线程：记录被创建，但绝不启动真线程、不发信号、不联网。"""

    done = Signal(bool, str)
    created: list["SilentWorker"] = []

    def __init__(self, config: object, mode: str) -> None:
        super().__init__()
        self.config = config
        self.mode = mode
        SilentWorker.created.append(self)

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
    # 三格必须按 base_url/api_key/model 装配给 worker（装反仍会去连错服务）
    worker = ImmediateWorker.created[-1]
    assert worker.config == ModelConfig(
        base_url="https://asr.test", api_key="test-key", model="test-model"
    )
    assert worker.mode == "audio"


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
