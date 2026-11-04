"""Issue #22 要求 3：**真 CoreMedia 对象**进单元测试。

以前这条链只有假件（`test_assembly_layers._FakeCoreMedia`），真 `CMSampleBuffer`
从没进过测试——所以 236 项全绿也拦不住点「开始捕获」放音即 SIGSEGV。本文件用
CoreMedia 构造函数造真对象，覆盖老板点名的三种输入：正常 buffer / 空数据 /
非预期格式，外加空 sample buffer 与「非主线程驱动」。

真机实测依据（本机 macOS 27.2 + pyobjc 13，SCK 送来的音频帧；探针原始输出贴在
docs/qa/issue-22/README.md）：

- ASBD 由 PyObjC 摊平成 9 元素元组：`[16000.0, 1819304813('lpcm'), 41, 4, 1, 4, 2, 32, 0]`
  （flags=41 拆位是 IsFloat|IsPacked|**IsNonInterleaved**，mBytesPerFrame=4 是**每声道**，
  即分声道连续存放；`CMFormatDescriptionGetMediaType` 回 1936684398('soun')）
  → 采样率 / formatID / flags（IsFloat|IsPacked|IsNonMixable）/ bytesPerPacket /
  framesPerPacket / bytesPerFrame / channels / bits / reserved；
- 每帧块缓冲 2560 字节（= 20ms @16kHz 双声道 float32），`CMBlockBufferCopyDataBytes`
  返回 `(OSStatus=0, buffer)`。

结构性天花板（同一份说明写进 docs/architecture.md）：单元测试能覆盖「真对象 +
解析逻辑 + delegate selector」，但覆盖不到「SCK 在自己那条 GCD 队列线程上派发
首帧」——那一段只能靠真机复现与 soak（docs/qa/issue-22/）。两层都要有。
"""

from __future__ import annotations

import ctypes
import struct
import threading
from collections.abc import Callable
from types import SimpleNamespace

import numpy as np
import pytest

sck = pytest.importorskip("app.capture.sck", reason="Mac 捕获链")
CoreMedia = pytest.importorskip("CoreMedia", reason="CoreMedia 只在 macOS 存在")

from app.capture import sck_session  # noqa: E402
from app.capture.sck import (  # noqa: E402
    drop_details,
    drop_report,
    extract_audio_samples,
    load_coremedia,
    reset_drop_report,
)

# 真机实测值（见模块 docstring），不是猜的
LPCM_FORMAT_ID = 1819304813  # 'lpcm'
# 真机 flags=41 = IsFloat(1) | IsPacked(8) | **IsNonInterleaved(32)**，即分声道连续存放，
# 配 mBytesPerFrame=4（每声道 4 字节）。用左右声道不同频率的立体声测出来，
# 见 docs/qa/issue-22/logs/channel-layout-measured.txt。
FLOAT32_FLAGS = 41
FLOAT32_PLANAR_BYTES_PER_FRAME = 4
# 交错口径：flags 不带 32，mBytesPerFrame=声道数×4（真机不是这种，但解析也支持）
FLOAT32_INTERLEAVED_FLAGS = 9
INT16_FLAGS = 12  # 有 IsPacked，没有 IsFloat —— 非预期格式
BYTES_PER_SAMPLE = 4

# CMBlockBuffer 不拥有传入的内存；源缓冲一旦被 GC，CoreMedia 就读到野指针。
# 这正是本票的问题类型（裸指针跨边界），所以测试里必须显式把它们留活。
_keepalive: list[object] = []


def build_sample_buffer(
    pcm: bytes,
    *,
    frames: int,
    rate: int = 16000,
    channels: int = 2,
    flags: int = FLOAT32_FLAGS,
    bits: int = 32,
) -> object:
    """用 CoreMedia 构造函数造一个**真** CMSampleBufferRef（不是替身）。

    三个状态码都当场断言：构造失败就是测试自己的错，不许让后面的断言去猜。
    """
    _keepalive.append(pcm)
    status, block = CoreMedia.CMBlockBufferCreateWithMemoryBlock(
        None, None, max(len(pcm), 1), None, None, 0, len(pcm), 0, None
    )
    assert int(status) == 0, f"块缓冲构造失败 OSStatus={int(status)}"
    if pcm:
        arr = (ctypes.c_char * len(pcm)).from_buffer_copy(pcm)
        _keepalive.append(arr)
        replace = CoreMedia.CMBlockBufferReplaceDataBytes(arr, block, 0, len(pcm))
        assert int(replace if not isinstance(replace, tuple) else replace[0]) == 0
    # 交错时 mBytesPerFrame 是「一帧所有声道」，planar 时是「一帧一个声道」——
    # 真机实测正是 planar 且 mBytesPerFrame=4（每声道），这里按同一个口径写。
    per_channel_bytes = bits // 8
    bytes_per_frame = per_channel_bytes if flags & 32 else per_channel_bytes * channels
    asbd = (
        float(rate),
        LPCM_FORMAT_ID,
        flags,
        bytes_per_frame,
        1,
        bytes_per_frame,
        channels,
        bits,
        0,
    )
    status, fmt = CoreMedia.CMAudioFormatDescriptionCreate(None, asbd, 0, None, 0, None, None, None)
    assert int(status) == 0, f"格式描述构造失败 OSStatus={int(status)}"
    status, sample_buffer = CoreMedia.CMSampleBufferCreateReady(
        None, block, fmt, frames, 0, None, 0, None, None
    )
    assert int(status) == 0, f"sample buffer 构造失败 OSStatus={int(status)}"
    return sample_buffer


def stereo_float32(values: list[float]) -> bytes:
    """双声道 float32，**分声道连续存放**（真机 SCK 送来的就是这种）：前半是左声道、
    后半是右声道。这里左右取同值（与 `say` 的单声道语音一致），所以单声道混合的结果
    就等于 values 本身——断言因此看得见具体数值。
    """
    packed = b"".join(struct.pack("<f", v) for v in values)
    return packed + packed


def stereo_float32_interleaved(values: list[float]) -> bytes:
    """双声道 float32，**交错**存放：L0 R0 L1 R1 …（左右同值，混合结果同样等于 values）。"""
    out = bytearray()
    for v in values:
        out += struct.pack("<f", v)
        out += struct.pack("<f", v)
    return bytes(out)


def planar_float32(left: list[float], right: list[float]) -> bytes:
    """左右声道**不同**的 planar 帧：用来证明两种存放方式不能混着解。"""
    return b"".join(struct.pack("<f", v) for v in left + right)


@pytest.fixture(autouse=True)
def _clean_drops() -> None:
    reset_drop_report()
    load_coremedia()  # 真桥必须在主线程先加载好（被测试的正是这条约束）


class TestRealCoreMediaBuffer:
    def test_normal_buffer_yields_expected_mono_float32(self) -> None:
        """正常帧：真对象进链 → 声道平均后的样本值必须逐个对得上（不只看长度）。"""
        # 每个值同时放左/右声道（与 `say` 的单声道语音一致）→ N 个值 = N 帧
        values = [0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09]
        sample_buffer = build_sample_buffer(stereo_float32(values), frames=len(values))

        parsed = extract_audio_samples(sample_buffer)

        assert parsed is not None, f"真 buffer 解析失败：{drop_report()}"
        samples, rate = parsed
        assert rate == 16000
        assert len(samples) == len(values)
        np.testing.assert_allclose(samples, values, atol=1e-6)  # 左右同值 → 平均=原值
        assert drop_report() == {}

    def test_real_format_description_matches_measured_asbd(self) -> None:
        """把「真 ASBD 摊平成 9 元素元组」钉住：假件与解析代码都按这份形状写。

        元组里 index 6=声道数、7=位深；PyObjC 换版本改成结构体对象时这条会红，
        提醒去重测真机形状（历史上就是形状猜错才出过「声道数读到 0」这类问题）。
        """
        sample_buffer = build_sample_buffer(stereo_float32([0.5, 0.5]), frames=2)

        fmt = CoreMedia.CMSampleBufferGetFormatDescription(sample_buffer)
        asbd = CoreMedia.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)

        if isinstance(asbd, tuple):
            assert len(asbd) == 9, f"ASBD 形状变了：{asbd!r}"
            fields = sck._asbd_fields(asbd)  # noqa: SLF001 - 就是要验这条解析路径
            # 第 6 个是 mBytesPerFrame：真机=4（每声道），planar 判据的第二路
            assert fields == (
                16000,
                2,
                LPCM_FORMAT_ID,
                FLOAT32_FLAGS,
                32,
                FLOAT32_PLANAR_BYTES_PER_FRAME,
            )
        else:
            assert sck._asbd_fields(asbd) == (16000, 2, LPCM_FORMAT_ID, FLOAT32_FLAGS, 32, 4)  # noqa: SLF001

    def test_empty_data_buffer_is_skipped_with_readable_reason(self) -> None:
        """空数据：真 sample buffer 但没有数据块 → 跳帧 + 人话记录，不返回垃圾、不崩。"""
        # dataLength=0 时 CoreMedia 不给块缓冲（OSStatus=-12704），拿到的就是
        # 「有格式、没数据」的真 buffer —— 正是空数据输入。
        status, fmt = CoreMedia.CMAudioFormatDescriptionCreate(
            None,
            (16000.0, LPCM_FORMAT_ID, FLOAT32_FLAGS, 4, 1, 4, 2, 32, 0),
            0,
            None,
            0,
            None,
            None,
            None,
        )
        assert int(status) == 0
        status, sample_buffer = CoreMedia.CMSampleBufferCreateReady(
            None, None, fmt, 0, 0, None, 0, None, None
        )
        assert int(status) == 0

        assert extract_audio_samples(sample_buffer) is None
        assert any("数据块缓冲" in reason for reason in drop_report()), drop_report()

    def test_unexpected_format_is_skipped_instead_of_misread(self) -> None:
        """非预期格式（真 int16 PCM buffer）：必须跳帧并说清原因，不能当 float32 硬解。"""
        pcm = b"".join(struct.pack("<h", int(3000 * i)) for i in range(10))
        sample_buffer = build_sample_buffer(pcm, frames=5, rate=48000, flags=INT16_FLAGS, bits=16)

        parsed = extract_audio_samples(sample_buffer)

        assert parsed is None
        assert any("位深不是 32 位浮点" in reason for reason in drop_report()), drop_report()

    def test_nil_sample_buffer_is_skipped(self) -> None:
        """SCK 递来空指针：判空就走，绝不进 CoreMedia。"""
        assert extract_audio_samples(None) is None
        assert any("空 sample buffer" in reason for reason in drop_report()), drop_report()

    def test_copy_failure_status_is_reported_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """拷贝返回码非 0：跳帧并把 OSStatus 写进人话里（返回码必须判，Issue #22 要求 2）。"""
        sample_buffer = build_sample_buffer(stereo_float32([0.25, 0.25]), frames=1)
        bridge = dict(load_coremedia())
        bridge["CMBlockBufferCopyDataBytes"] = lambda *_a: (8946, None)
        monkeypatch.setattr(sck, "_CM", bridge)  # _CM_MODULE 不动 → 缓存命中这份替身桥

        assert extract_audio_samples(sample_buffer) is None
        assert any("拷贝 PCM 失败" in reason for reason in drop_report()), drop_report()
        assert "OSStatus=8946" in " ".join(drop_details().values()), drop_details()


class TestRealBufferThroughDelegate:
    """真 buffer 走真 delegate（假 delegate 类以前把整段类体都 patch 掉了）。"""

    def test_delegate_forwards_real_frame_to_session(self) -> None:
        received: list[tuple[np.ndarray, int]] = []
        delegate_cls = sck_session._make_delegate_class()
        delegate = delegate_cls.alloc().initWithCallback_(
            lambda samples, rate: received.append((samples, rate))
        )

        values = [0.125] * 8
        sample_buffer = build_sample_buffer(stereo_float32(values), frames=len(values))
        delegate.stream_didOutputSampleBuffer_ofType_(None, sample_buffer, 1)

        assert len(received) == 1
        np.testing.assert_allclose(received[0][0], [0.125] * len(values), atol=1e-6)
        assert drop_report() == {}

    def test_delegate_ignores_non_audio_frame_type(self) -> None:
        received: list[tuple[np.ndarray, int]] = []
        delegate_cls = sck_session._make_delegate_class()
        delegate = delegate_cls.alloc().initWithCallback_(
            lambda samples, rate: received.append((samples, rate))
        )
        sample_buffer = build_sample_buffer(stereo_float32([0.2, 0.2]), frames=2)

        delegate.stream_didOutputSampleBuffer_ofType_(
            None, sample_buffer, 0
        )  # SCStreamOutputTypeScreen

        assert received == []
        assert drop_report() == {}  # 视频帧本来就该走掉，不算解析失败

    def test_real_buffers_survive_non_main_thread_after_warmup(self) -> None:
        """预热之后，从别的线程驱动真 buffer 也只是普通函数调用（Issue #22 的场景）。

        崩的从来不是「解析」本身，而是**首次加载**发生在非主线程：所以这条用例先把
        桥在主线程预热（fixture 已做），再从工作线程喂 200 帧真 buffer。
        """
        assert sck.coremedia_loaded()  # fixture 已在主线程预热
        n_values = 8
        frames = build_sample_buffer(stereo_float32([0.4] * n_values), frames=n_values)
        results: list[int] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                for _ in range(200):
                    parsed = extract_audio_samples(frames)
                    results.append(0 if parsed is None else len(parsed[0]))
            except BaseException as e:  # noqa: BLE001 - 测试里要抓下来才好断言
                errors.append(e)

        thread = threading.Thread(target=work)
        thread.start()
        thread.join(timeout=60)
        assert not thread.is_alive(), "工作线程卡住"
        assert errors == []
        assert results == [n_values] * 200


class TestSecondDefenseLineIsPinned:
    """第二条防线（回调套 autorelease pool）也得有测试钉住，删掉就该红（评审意见）。"""

    def test_audio_frame_callback_enters_and_exits_an_autorelease_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import objc

        events: list[str] = []

        class _SpyPool:
            def __enter__(self) -> None:
                events.append("enter")

            def __exit__(self, *_exc: object) -> bool:
                events.append("exit")
                return False

        monkeypatch.setattr(objc, "autorelease_pool", lambda: _SpyPool())
        seen: list[int] = []
        delegate = (
            sck_session._make_delegate_class()
            .alloc()
            .initWithCallback_(  # type: ignore[attr-defined]
                lambda samples, _rate: seen.append(len(samples))
            )
        )
        buffer = build_sample_buffer(stereo_float32([0.3, 0.3]), frames=2)

        delegate.stream_didOutputSampleBuffer_ofType_(None, buffer, 1)

        assert events == ["enter", "exit"], f"音频帧必须在池里进出一次：{events}"
        assert seen == [2]

    def test_non_audio_frame_returns_before_touching_the_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """视频帧早退：不建池、不解析、不记账（不算丢帧）。"""
        import objc

        events: list[str] = []
        monkeypatch.setattr(objc, "autorelease_pool", lambda: events.append("pool") or _NullPool())
        seen: list[int] = []
        delegate = (
            sck_session._make_delegate_class()
            .alloc()
            .initWithCallback_(  # type: ignore[attr-defined]
                lambda samples, _rate: seen.append(len(samples))
            )
        )

        delegate.stream_didOutputSampleBuffer_ofType_(
            None, build_sample_buffer(stereo_float32([0.3, 0.3]), frames=2), 0
        )

        assert events == [] and seen == [] and drop_report() == {}


class _NullPool:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return False


class TestNoLoadOnForeignThread:
    """兜底闸：没预热时绝不在回调线程现场加载 CoreMedia——那正是 Issue #22 的崩溃现场。"""

    def test_unwarmed_bridge_refuses_to_load_off_main_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        buffer = build_sample_buffer(stereo_float32([0.25, 0.25]), frames=2)
        # 模拟「开流前没预热」：桥取不到 + 现场加载禁止
        monkeypatch.setattr(sck, "_cached_bridge", lambda: None)
        monkeypatch.setattr(
            sck,
            "load_coremedia",
            lambda: (_ for _ in ()).throw(AssertionError("非主线程不许现场加载 CoreMedia")),
        )
        outcome: dict[str, object] = {}

        def work() -> None:
            outcome["parsed"] = extract_audio_samples(buffer)
            outcome["drops"] = drop_report()

        thread = threading.Thread(target=work)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "工作线程卡住"

        assert outcome["parsed"] is None
        assert any("CoreMedia 桥没在开流前" in reason for reason in outcome["drops"]), outcome[
            "drops"
        ]

    def test_hot_path_does_no_import_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """解析一帧的热路径不执行任何 import——这是「回调线程一个 import 都不留」的实测口径。

        做法：先确保桥已缓存，再把 `builtins.__import__` 换成会炸的版本，解析一帧真 buffer。
        """
        buffer = build_sample_buffer(stereo_float32([0.25, 0.25]), frames=2)
        assert extract_audio_samples(buffer) is not None

        import builtins

        def exploding_import(*_args: object, **_kw: object) -> None:
            raise AssertionError("热路径里不该再执行 import")

        monkeypatch.setattr(builtins, "__import__", exploding_import)
        assert extract_audio_samples(buffer) is not None, drop_report()

    def test_bridge_exception_is_accounted_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """桥调用抛 Python 异常时也必须进记账——评审指出老写法只包了第一处调用。"""
        buffer = build_sample_buffer(stereo_float32([0.25, 0.25]), frames=2)
        bridge = dict(load_coremedia())

        def boom(*_args: object) -> None:
            raise TypeError("depythonifying 'pointer', got 'int'")

        bridge["CMAudioFormatDescriptionGetStreamBasicDescription"] = boom  # type: ignore[assignment]
        monkeypatch.setattr(sck, "_CM", bridge)

        assert extract_audio_samples(buffer) is None
        assert any("调用 CoreMedia 时出错" in reason for reason in drop_report()), drop_report()
        assert "depythonifying" in " ".join(drop_details().values()), drop_details()

    def test_planar_frame_mixes_channels_not_adjacent_samples(self) -> None:
        """真机形状（flags=41 含 NonInterleaved、mBytesPerFrame=4）：左 1000、右 3000
        这类「两个声道不同」的数据必须按声道混合，不能当交错两两平均。

        这条是回归闸门：把它读成交错会得到 [0.2, 0.35]（L0 与 L1 平均、L1 与 R0 平均），
        是彻底错的声音，但**不会崩也不会报错**——只能靠这条用例钉住。
        """
        left = [0.4, 0.2, 0.6]
        right = [0.0, 0.6, 0.2]
        buffer = build_sample_buffer(planar_float32(left, right), frames=3)

        parsed = extract_audio_samples(buffer)
        assert parsed is not None, drop_report()
        samples, _rate = parsed
        expected = [(a + b) / 2 for a, b in zip(left, right, strict=True)]
        assert list(np.round(samples, 5)) == pytest.approx(expected, abs=1e-5), samples

    def test_interleaved_frame_still_works(self) -> None:
        """交错帧（flags=9）走另一条混合路径，同样要给出正确答案。"""
        left = [0.4, 0.2, 0.6]
        right = [0.0, 0.6, 0.2]
        interleaved = b"".join(
            struct.pack("<f", a) + struct.pack("<f", b) for a, b in zip(left, right, strict=True)
        )
        buffer = build_sample_buffer(interleaved, frames=3, flags=FLOAT32_INTERLEAVED_FLAGS)

        parsed = extract_audio_samples(buffer)
        assert parsed is not None, drop_report()
        samples, _rate = parsed
        expected = [(a + b) / 2 for a, b in zip(left, right, strict=True)]
        assert list(np.round(samples, 5)) == pytest.approx(expected, abs=1e-5), samples


class TestWarmUpBeforeStream:
    """开流之前必须在主线程把 CoreMedia 加载好——这条是崩溃根因的回归闸。

    如果谁把加载挪回解析里（或删除预热），`order` 的第一项就不再是
    `load_coremedia`，这条用例立刻红；那正是 236 项假件测试当时拦不住的东西。
    """

    def test_session_start_loads_bridge_before_opening_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_assembly_layers import (  # noqa: PLC0415
            FakeDelegate,
            FakeFilter,
            FakeSCKConfiguration,
            FakeShareableContent,
        )

        order: list[str] = []

        class RecordingStream:
            def addStreamOutput_type_sampleHandlerQueue_error_(self, *_args: object) -> None:
                order.append("addStreamOutput")

            def startCaptureWithCompletionHandler_(self, callback: object) -> None:
                order.append("startCapture")
                assert callback is not None
                callback(None)  # type: ignore[operator]

            def stopCaptureWithCompletionHandler_(self, callback: object) -> None:
                callback(None)  # type: ignore[operator]

        stream = RecordingStream()

        class RecordingStreamFactory:
            @classmethod
            def alloc(cls) -> RecordingStreamFactory:
                return cls()

            def initWithFilter_configuration_delegate_(self, *_args: object) -> RecordingStream:
                return stream

        mods = {
            "Quartz": SimpleNamespace(
                CGPreflightScreenCaptureAccess=lambda: True,
                CGRequestScreenCaptureAccess=lambda: True,
            ),
            "sck": SimpleNamespace(
                SCShareableContent=FakeShareableContent,
                SCContentFilter=FakeFilter,
                SCStreamConfiguration=FakeSCKConfiguration,
                SCStream=RecordingStreamFactory,
            ),
        }

        real_load = sck.load_coremedia
        seen: dict[str, object] = {}

        def spy_load() -> dict[str, object]:
            order.append("load_coremedia")
            seen["thread"] = (
                "main" if threading.current_thread() is threading.main_thread() else "other"
            )
            bridge = real_load()  # 真的走生产预热，不返回空壳：符号取没取到也要验
            seen["keys"] = tuple(sorted(bridge))
            return bridge

        monkeypatch.setattr(sck_session, "_sck_modules", lambda: mods)
        monkeypatch.setattr(sck_session, "_make_delegate_class", lambda: FakeDelegate)
        monkeypatch.setattr(sck_session, "load_coremedia", spy_load)

        sck_session.SCKSession(lambda _block, _level: None).start()

        assert order == ["load_coremedia", "addStreamOutput", "startCapture"], order
        assert seen["thread"] == "main", "预热必须在主线程（这条就是 #22 的根因）"
        assert seen["keys"] == tuple(sorted(sck._COREMEDIA_FUNCTIONS)), seen["keys"]

    def test_start_reports_bridge_load_failure_in_plain_words(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """加载失败也必须是人话错误，不许留下半启动的流。"""
        from tests.test_assembly_layers import FakeDelegate  # noqa: PLC0415

        monkeypatch.setattr(sck_session, "_make_delegate_class", lambda: FakeDelegate)

        def boom() -> dict[str, object]:
            raise ImportError("no CoreMedia")

        monkeypatch.setattr(sck_session, "load_coremedia", boom)

        # 预热失败必须在「授权 / 建流」之前止步：让后续每一步一碰就炸，而不是靠返回空壳。
        def forbidden(step: str) -> Callable[..., None]:
            def _raise(*_args: object, **_kwargs: object) -> None:
                raise AssertionError(f"预热失败后不该走到「{step}」")

            return _raise

        mods2 = {
            "Quartz": SimpleNamespace(
                CGPreflightScreenCaptureAccess=forbidden("授权检查"),
                CGRequestScreenCaptureAccess=forbidden("授权请求"),
            ),
            "sck": SimpleNamespace(SCStream=SimpleNamespace(alloc=forbidden("建流"))),
        }
        monkeypatch.setattr(sck_session, "_sck_modules", lambda: mods2)

        with pytest.raises(sck.SCKCaptureError, match="音频解析组件加载失败"):
            sck_session.SCKSession(lambda _block, _level: None).start()
