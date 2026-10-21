"""ScreenCaptureKit PyObjC 会话实现（仅 macOS 导入）。

授权流程（API 均已在本机 macOS 26 运行时核实，2026-10-21）：
1. Quartz.CGPreflightScreenCaptureAccess() 查状态（False = 未授权）
2. 未授权 → Quartz.CGRequestScreenCaptureAccess() 触发系统弹窗
3. 弹窗被拒 → denied_hint 人话指引
4. 授权后 SCShareableContent.getShareableContentWithCompletionHandler_ 拿显示器列表
5. SCContentFilter.initWithDisplay_excludingApplications_exceptingWindows_ 过滤整显示器
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
import objc

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


_DELEGATE_CLASS: Any = None


def _make_delegate_class() -> Any:
    """生成（并缓存）PyObjC delegate 类。

    重要两点（都曾导致收不到音频帧/崩溃）：
    1. stream:didOutputSampleBuffer:ofType: 必须直接定义在 NSObject 子类内 ——
       从纯 Python 基类继承的方法 PyObjC 不会注册成 ObjC selector。
    2. ObjC 类名必须全局唯一且只创建一次 —— 重复创建同名类会报 overriding，
       因此这里做模块级缓存，整个进程复用同一个类。
    """
    global _DELEGATE_CLASS
    if _DELEGATE_CLASS is not None:
        return _DELEGATE_CLASS

    from Foundation import NSObject

    class _LCDStreamDelegate(NSObject):  # type: ignore[misc]
        def initWithCallback_(self, on_audio):  # type: ignore[no-untyped-def]
            self = objc.super(_LCDStreamDelegate, self).init()
            if self is not None:
                self._on_audio = on_audio
            return self

        def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, frame_type):  # type: ignore[no-untyped-def]
            if int(frame_type) != SCK_OUTPUT_TYPE_AUDIO:
                return
            parsed = extract_audio_samples(sample_buffer)
            if parsed is None:
                return
            samples, rate = parsed
            if len(samples) > 0:
                self._on_audio(samples, rate)

    _DELEGATE_CLASS = _LCDStreamDelegate
    return _DELEGATE_CLASS


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
        self._pump_until(lambda: bool(content_box), timeout_s=15.0)
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
        filt = sck.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
            displays[0], None, None
        )
        config = sck.SCStreamConfiguration.alloc().init()
        config.setCapturesAudio_(True)
        config.setExcludesCurrentProcessAudio_(False)  # 不排除本进程（确保能收到所有系统声音）
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
        self._pump_until(lambda: bool(done_box), timeout_s=15.0)
        if not done_box:
            self._try_stop(stream)
            raise SCKCaptureError("启动捕获超时，请重试；若反复失败请重新授权。")
        self._stream = stream
        logger.info("SCK 流已启动")

    @staticmethod
    def _pump_until(predicate: Any, timeout_s: float) -> None:
        """等待 predicate 为真，期间驱动事件循环。

        关键：在 Qt 应用里主线程 runloop 已被 Qt 接管，手动跑 NSRunLoop.runMode
        无法泵出 SCK 回调（会一直超时）。所以有 QApplication 时用 processEvents，
        否则才退回 NSRunLoop。
        """
        import time

        from Foundation import NSDate, NSRunLoop

        deadline = time.monotonic() + timeout_s
        try:
            from PySide6.QtWidgets import QApplication

            has_qt = QApplication.instance() is not None
        except Exception:  # noqa: BLE001 - 无 Qt 环境
            has_qt = False

        if has_qt:
            app = QApplication.instance()
            while not predicate() and time.monotonic() < deadline:
                app.processEvents()  # type: ignore[union-attr]
                time.sleep(0.01)
        else:
            while not predicate() and time.monotonic() < deadline:
                NSRunLoop.currentRunLoop().runMode_beforeDate_(
                    "kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.05)
                )

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
