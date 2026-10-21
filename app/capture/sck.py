"""Mac 免驱动系统声音捕获（ScreenCaptureKit）。

macOS 13+ 上通过 ScreenCaptureKit 捕获显示器输出的音频（不录画面），
用户零安装，首次使用弹一次系统标准「屏幕录制/屏幕与系统音频录制」授权框。

本模块所有 PyObjC API 均已在本机 macOS 26 运行时核实（2026-10-21）：
- 授权 preflight：Quartz.CGPreflightScreenCaptureAccess / CGRequestScreenCaptureAccess
- 内容枚举：SCShareableContent.getShareableContentWithCompletionHandler_（get 前缀系）
- 过滤器：SCContentFilter.filterWithDisplay_includingWindows_excludingApplications_
- 流：SCStream.alloc().initWithFilter_configuration_delegate_
- 输出挂载：addStreamOutput_type_sampleHandlerQueue_error_，类型 SCStreamOutputTypeAudio=1
- delegate 可选方法：stream_didOutputSampleBuffer_ofType_
- 音频开关：SCStreamConfiguration.setCapturesAudio_ / setExcludesCurrentProcessAudio_
- 采样率：setSampleRate_（SCK 会把系统 48k 重采样到指定采样率，16k 合法）

音频样本链：CMSampleBuffer → CoreAudioBufferList → float32 交错字节 → numpy 单声道 → 16k
"""

from __future__ import annotations

import logging
import platform

import numpy as np

logger = logging.getLogger(__name__)

MIN_MACOS_MAJOR = 13
TARGET_SAMPLE_RATE = 16000
SCK_OUTPUT_TYPE_AUDIO = 1  # 已核实：SCStreamOutputTypeAudio = 1


class SCKCaptureError(Exception):
    """SCK 捕获失败，message 为给用户的人话。"""


def macos_version() -> tuple[int, int]:
    """返回 (major, minor)，非 macOS 返回 (0, 0)。"""
    try:
        parts = platform.mac_ver()[0].split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except Exception:  # noqa: BLE001 - 非 Mac 或解析失败，按不支持处理
        return (0, 0)


def unsupported_hint() -> str:
    return (
        "免驱动捕获需要 macOS 13 Ventura 及以上。\n"
        "你的系统较老，可改用备用方案：安装免费的 BlackHole 虚拟声卡"
        "（https://existential.audio/blackhole/ ）后在「捕获测试」页选择它。"
    )


def denied_hint() -> str:
    return (
        "未获得「屏幕录制」授权，无法捕获系统声音。\n"
        "请在 系统设置 → 隐私与安全性 → 屏幕录制 中勾选本软件后重试。\n"
        "说明：本功能只捕获声音、不截取画面，也不含麦克风。"
    )


def _interleaved_to_mono(data: bytes, channels: int) -> np.ndarray:
    """交错多声道 float32 字节流 → 单声道 float32 数组（声道取平均）。纯逻辑，可测。"""
    arr = np.frombuffer(data, dtype=np.float32)
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)
    return arr.astype(np.float32)


def resample_to_16k(samples: np.ndarray, src_rate: int) -> np.ndarray:
    """线性重采样到 16kHz。纯逻辑，可测。"""
    if src_rate == TARGET_SAMPLE_RATE or len(samples) == 0:
        return samples.astype(np.float32)
    duration = len(samples) / src_rate
    target_len = max(1, int(duration * TARGET_SAMPLE_RATE))
    src_idx = np.linspace(0, len(samples) - 1, target_len)
    return np.interp(src_idx, np.arange(len(samples)), samples).astype(np.float32)  # type: ignore[no-any-return]


def extract_audio_samples(sample_buffer: object) -> tuple[np.ndarray, int] | None:
    """从 CMSampleBuffer 抽 (float32 数组, 采样率)。返回 None 表示非音频帧。

    已在本机 macОС 26 实测通过的 API 用法：
    - ASBD 由 PyObjC 返回为元组（非结构体），声道数在 [6]、采样率在 [0]
    - PCM 用 CMSampleBufferGetDataBuffer + CMBlockBufferCopyDataBytes，
      out 参数必须是 ctypes 数组（长度=字节数）
    """
    try:
        import ctypes

        import CoreMedia

        fmt = CoreMedia.CMSampleBufferGetFormatDescription(sample_buffer)
        if fmt is None:
            return None
        asbd = CoreMedia.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
        if asbd is None:
            return None
        if isinstance(asbd, tuple):
            rate = int(asbd[0]) or 16000
            channels = int(asbd[6]) or 1
        else:
            rate = int(asbd.mSampleRate) or 16000
            channels = int(asbd.mChannelsPerFrame) or 1

        data_buf = CoreMedia.CMSampleBufferGetDataBuffer(sample_buffer)
        if data_buf is None:
            return None
        length = int(CoreMedia.CMBlockBufferGetDataLength(data_buf))
        if length <= 0:
            return None
        buf = (ctypes.c_char * length)()
        result = CoreMedia.CMBlockBufferCopyDataBytes(data_buf, 0, length, buf)
        # 返回 (status, buffer)；status==0 表示成功
        status = result[0] if isinstance(result, tuple) else result
        if status != 0:
            return None
        raw = bytes(buf)
        samples = _interleaved_to_mono(raw, channels)
        return samples, rate
    except Exception:  # noqa: BLE001 - 任何解析失败都按"跳过该帧"处理，不让流崩
        logger.exception("音频帧解析失败，跳过")
        return None
