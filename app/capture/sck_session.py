"""ScreenCaptureKit PyObjC 会话实现（仅 macOS 导入）。

授权流程（API 均已在本机 macOS 26 运行时核实，2026-10-21）：
1. Quartz.CGPreflightScreenCaptureAccess() 查状态（False = 未授权）
2. 未授权 → Quartz.CGRequestScreenCaptureAccess() 触发系统弹窗
3. 弹窗被拒 → denied_hint 人话指引
4. 授权后 SCShareableContent.getShareableContentWithCompletionHandler_ 拿显示器列表
5. SCContentFilter.filterWithDisplay_excludingApplications_ 过滤整显示器
6. SCStreamConfiguration：capturesAudio=True、excludesCurrentProcessAudio=True（不抓自己）、
   sampleRate=16000、queueDepth 小值保低延迟；不请求视频
7. SCStream + addStreamOutput(SCStreamOutputTypeAudio=1)，delegate 实现音频帧回调

PyObjC 对象均为动态桥接，本模块类型统一按 Any 处理（mypy 对 ObjC 运行时
无静态信息，强类型化没有价值）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import numpy as np

from .sck import (
    SCK_OUTPUT_TYPE_AUDIO,
    TARGET_SAMPLE_RATE,
    SCKCaptureError,
    denied_hint,
    extract_audio_samples,
)

logger = logging.getLogger(__name__)


def _sck_modules() -> dict[str, Any]:
    """统一导入 PyObjC 相关模块（集中一处便于测试 mock）。"""
    import Quartz
    import ScreenCaptureKit as sck

    return {"Quartz": Quartz, "sck": sck}


class SCKStreamDelegate:  # pragma: no cover - PyObjC 桥由集成验证
    """SCStream delegate mixin：实现可选协议方法 stream:didOutputSampleBuffer:ofType:。

    PyObjC 中 NSObject 子类实现下划线形式的方法名即响应 selector。
    """

    def initWithCallback_(self: Any, on_audio: Callable[[np.ndarray, int], None]) -> Any:
        # mypy 不知道 NSObject 有 init，忽略类型检查
        self = super().init()  # type: ignore[misc]
        if self is not None:
            self._on_audio = on_audio
        return self

    def stream_didOutputSampleBuffer_ofType_(
        self: Any, stream: Any, sample_buffer: Any, frame_type: Any
    ) -> None:
        if int(frame_type) != SCK_OUTPUT_TYPE_AUDIO:
            return
        parsed = extract_audio_samples(sample_buffer)
        if parsed is None:
            return
        samples, rate = parsed
        if len(samples) > 0:
            self._on_audio(samples, rate)


def _make_delegate_class() -> Any:
    """生成 PyObjC delegate 类（延迟到 start 调用时，避免非 Mac 导入失败）。"""
    from Foundation import NSObject

    class _Delegate(SCKStreamDelegate, NSObject):  # type: ignore[misc]
        pass

    return _Delegate


class SCKSession:
    """一次 SCK 捕获会话：授权 → 配置 → 开流 → 停流。"""

    def __init__(self, on_block: Callable[[np.ndarray, float], None]) -> None:
        self._on_block = on_block
        self._stream: Any = None
        self._delegate: Any = None

    def start(self) -> None:
        mods = _sck_modules()
        Quartz = mods["Quartz"]
        sck = mods["sck"]
        from Foundation import NSDate, NSRunLoop

        # 1. 授权（preflight → 请求弹窗 → 拒绝给指引）
        if not Quartz.CGPreflightScreenCaptureAccess():
            granted = Quartz.CGRequestScreenCaptureAccess()
            if not granted:
                raise SCKCaptureError(denied_hint())

        # 2. 拿显示器（同步等回调，上限 15 秒）
        content_box: list[Any] = []

        def on_content(content: Any, error: Any) -> None:
            content_box.append((content, error))

        sck.SCShareableContent.getShareableContentWithCompletionHandler_(on_content)
        deadline = NSDate.dateWithTimeIntervalSinceNow_(15.0)
        while not content_box and NSDate.date().laterDate_(deadline) is not deadline:
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                "kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
        if not content_box:
            raise SCKCaptureError("获取屏幕内容超时，请重试。")
        content, error = content_box[0]
        if content is None:
            raise SCKCaptureError(
                f"获取屏幕内容失败：{error}。\n请确认已在本软件的「屏幕录制」授权中允许。"
            )
        displays = content.displays()
        if not displays or len(displays) == 0:
            raise SCKCaptureError("未找到活动的显示器，无法捕获声音。")

        # 3. 过滤器 + 配置（只声音，排除本应用自己的提示音）
        filt = sck.SCContentFilter.filterWithDisplay_excludingApplications_(displays[0], None)
        config = sck.SCStreamConfiguration.alloc().init()
        config.setCapturesAudio_(True)
        config.setExcludesCurrentProcessAudio_(True)
        config.setSampleRate_(TARGET_SAMPLE_RATE)
        config.setQueueDepth_(3)  # 低延迟

        # 4. 开流
        delegate_cls = _make_delegate_class()
        self._delegate = delegate_cls.alloc().initWithCallback_(self._on_audio_frame)
        stream = sck.SCStream.alloc().initWithFilter_configuration_delegate_(
            filt, config, self._delegate
        )
        try:
            out = stream.addStreamOutput_type_sampleHandlerQueue_error_(
                self._delegate, SCK_OUTPUT_TYPE_AUDIO, None, None
            )
            if isinstance(out, tuple) and out and out[0] is False:
                raise SCKCaptureError(f"挂载音频输出失败：{out[1] if len(out) > 1 else '未知'}")
        except SCKCaptureError:
            self._try_stop(stream)
            raise
        except Exception as e:  # noqa: BLE001 - 签名差异兜底：后续启动超时会暴露
            logger.warning("addStreamOutput 调用形式异常（继续启动）: %s", e)

        done_box: list[bool] = []

        def on_started(_: Any) -> None:
            done_box.append(True)

        stream.startCaptureWithCompletionHandler_(on_started)
        deadline = NSDate.dateWithTimeIntervalSinceNow_(15.0)
        while not done_box and NSDate.date().laterDate_(deadline) is not deadline:
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                "kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
        if not done_box:
            self._try_stop(stream)
            raise SCKCaptureError("启动捕获超时，请重试；若反复失败请重新授权。")
        self._stream = stream
        logger.info("SCK 流已启动")

    def _on_audio_frame(self, samples: np.ndarray, rate: int) -> None:
        """delegate 回调（SCK 音频队列线程）：重采样 + 音量，交给上层。"""
        from .sck import resample_to_16k

        mono16k = resample_to_16k(samples, rate)
        if len(mono16k) == 0:
            return
        level = float(np.abs(mono16k).max())
        self._on_block(mono16k, level)

    def _try_stop(self, stream: Any) -> None:
        try:
            stream.stopCaptureWithCompletionHandler_(lambda _: None)
        except Exception:  # noqa: BLE001 - 停止失败不影响主流程
            logger.exception("停止 SCK 流失败")

    def stop(self) -> None:
        if self._stream is not None:
            self._try_stop(self._stream)
            self._stream = None
