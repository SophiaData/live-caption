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

Issue #22（点「开始捕获」放音即 SIGSEGV）：这条链上的 CoreMedia 桥**必须在开流前于
主线程加载好**，见 `load_coremedia()`；解析过程中每一步的裸指针都要判空、返回码要判，
取不到就跳帧并留下人话告警（`drop_report()`），不再静默丢帧。
"""

from __future__ import annotations

import ctypes
import logging
import platform
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

MIN_MACOS_MAJOR = 13
TARGET_SAMPLE_RATE = 16000
SCK_OUTPUT_TYPE_AUDIO = 1  # 已核实：SCStreamOutputTypeAudio = 1

# 本模块用到的 CoreMedia 函数（名字固定；首次解析必须在主线程完成，见 load_coremedia）
_COREMEDIA_FUNCTIONS = (
    "CMSampleBufferGetFormatDescription",
    "CMAudioFormatDescriptionGetStreamBasicDescription",
    "CMSampleBufferGetDataBuffer",
    "CMBlockBufferGetDataLength",
    "CMBlockBufferCopyDataBytes",
)

_CM: dict[str, Any] | None = None
_CM_MODULE: Any = None

# 解析失败的记账：原因（人话）→ 累计跳过的帧数。见 _reject()
_drops: dict[str, int] = {}
_warned: set[str] = set()
_WARN_EVERY = 100  # 同一原因每累计 100 帧再提醒一次，避免 50 帧/秒刷屏

# 真机实测（2026-11-04，本机 macOS 27.2，SCK 送来的音频帧）：
# ASBD = 采样率 16000 / formatID 'lpcm' / flags 41（float32 + packed + 不可混音）/
# 2 声道 / 32 位；块缓冲 2560 字节/帧（= 20ms）。判据按这份实测写，不猜。
_LPCM_FORMAT_ID = 1819304813  # 'lpcm'
_FLAG_IS_FLOAT = 1 << 0
_FLAG_NON_INTERLEAVED = 1 << 4
_BYTES_PER_SAMPLE = 4


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


def load_coremedia() -> dict[str, Any]:
    """把 CoreMedia 的取数据函数取好并缓存——**必须在开流之前、在主线程调用**。

    Issue #22 的根因：PyObjC 的框架模块是惰性的，第一次碰 `CoreMedia.某函数` 才
    真正去加载框架、注册类型与包装对象，这过程会产生 autoreleased 的 ObjC 对象。
    SCK 把音频帧派发在它自己建的 GCD 队列上（崩溃报告里的线程名
    `com.screenCaptureKit.audioSampleHandlerQueue`），那条线程既没有 ObjC
    autorelease pool，也不该和主线程并发改 ObjC 运行时。原来
    `extract_audio_samples()` 里那句 `import CoreMedia` 就是让这套首次初始化发生在
    回调线程上：真机的「反复起真流」压力脚本 3/3 都在**第一帧** SIGSEGV
    （`KERN_INVALID_ADDRESS at 0x10`，栈在 PyObjC 的 `id_to_python` → `objc_msgSend`，
    即「把刚取回来的指针当对象发消息」；老板手工验收 2/2、另 5 次里也崩过 1 次）。
    挪回主线程后同一压力脚本 0/36 轮不崩；两条防线都进代码后合计 46 次真流会话、
    约 2.1 万帧、0 崩溃（证据与重跑命令见 docs/qa/issue-22/）。

    已经在 sys.modules 里时，本函数只是一次字典查表 + 缓存比较，回调线程每帧调用也
    不会再触发加载；测试用假 CoreMedia 顶掉 sys.modules 时会自动重解析。
    """
    global _CM, _CM_MODULE
    import CoreMedia

    if _CM is not None and _CM_MODULE is CoreMedia:
        return _CM
    _CM = {name: getattr(CoreMedia, name) for name in _COREMEDIA_FUNCTIONS}
    _CM_MODULE = CoreMedia
    return _CM


def coremedia_loaded() -> bool:
    """CoreMedia 桥是否已经加载好（开流前的预热检查用）。"""
    return _CM is not None


def drop_report() -> dict[str, int]:
    """解析失败原因（人话）→ 累计跳过的帧数。测试与 soak 靠它证明没有「不吭声的丢帧」。"""
    return dict(_drops)


def reset_drop_report() -> None:
    """清空记账（只在测试里用）。"""
    _drops.clear()
    _warned.clear()


def _reject(reason: str) -> None:
    """记一笔「这帧没出音频」并保证人看得见：同一原因首次就 WARNING，之后每 100 帧再报一次。

    Issue #22 要求 2：不能把段错误换成静默丢帧。Python 抓不到 SIGSEGV，所以这里
    不是「兜住崩溃」，而是**在解引用裸指针之前**就不往下走，并把原因写成人话留下痕迹。
    """
    total = _drops.get(reason, 0) + 1
    _drops[reason] = total
    if reason not in _warned:
        _warned.add(reason)
        logger.warning("音频帧被跳过：%s（同类已累计 %d 帧）", reason, total)
    elif total % _WARN_EVERY == 0:
        logger.warning("音频帧仍在被跳过：%s（已累计 %d 帧）", reason, total)


def _asbd_fields(asbd: Any) -> tuple[int, int, int, int, int] | None:
    """ASBD → (采样率, 声道数, formatID, flags, 每声道位深)；读不出来返回 None。

    PyObjC 13 在真机上把这个结构体摊平成 9 元素元组（实测，见 docs/qa/issue-22）；
    旧绑定给的是结构体对象，两种形状都支持。取不到的字段按 0 = 「不知道」处理，
    由调用方决定跳过；不猜、不填默认值。
    """
    try:
        if isinstance(asbd, tuple):

            def at(i: int) -> int:
                return int(asbd[i]) if i < len(asbd) else 0

            return at(0), at(6), at(1), at(2), at(7)
        return (
            int(asbd.mSampleRate),
            int(asbd.mChannelsPerFrame),
            int(asbd.mFormatID),
            int(asbd.mFormatFlags),
            int(asbd.mBitsPerChannel),
        )
    except Exception:  # noqa: BLE001 - 形状不认识就是「非预期格式」，由调用方跳帧并告警
        return None


def extract_audio_samples(sample_buffer: object) -> tuple[np.ndarray, int] | None:
    """从 CMSampleBuffer 抽 (float32 单声道数组, 采样率)；拿不到就跳帧并留人话记录。

    每一步拿到的都是 CoreMedia 裸指针，所以逐步判空、判返回码、判格式（Issue #22 要求 2）：
    空 sample buffer、空格式描述、空 ASBD、空数据块、长度 0、拷贝返回码非 0、
    不是 32 位浮点交错 PCM——全部跳帧 + `drop_report()` 记账，绝不带着空指针往下走。
    """
    cm = load_coremedia()

    if sample_buffer is None:
        _reject("SCK 回调给了空 sample buffer")

        return None

    try:
        fmt = cm["CMSampleBufferGetFormatDescription"](sample_buffer)
    except Exception as e:  # noqa: BLE001 - Python 层异常（不是段错误），照样跳帧留痕
        _reject(f"取格式描述时出错：{type(e).__name__}: {e}")

        return None
    if fmt is None:
        _reject("这帧没有格式描述（CMSampleBufferGetFormatDescription 返回空）")

        return None

    asbd = cm["CMAudioFormatDescriptionGetStreamBasicDescription"](fmt)
    if asbd is None:
        _reject("格式描述里没有 ASBD（取采样率/声道数的依据）")

        return None
    fields = _asbd_fields(asbd)
    if fields is None:
        _reject("ASBD 形状不认识，读不出采样率/声道数")

        return None
    rate, channels, format_id, flags, bits = fields
    if rate <= 0 or channels <= 0:
        _reject(f"ASBD 里的采样率/声道数不可用（rate={rate}, channels={channels}）")

        return None
    # 字段为 0 当「不知道」处理（沿用历史行为）；一旦明确报出不是 32 位浮点交错 PCM，
    # 就必须跳帧——按 float32 硬解析 int16/分离声道数据会得到垃圾音频，那比丢一帧更糟。
    if format_id not in (0, _LPCM_FORMAT_ID):
        _reject(f"音频不是线性 PCM（formatID={format_id}）")

        return None
    if bits not in (0, 32):
        _reject(f"音频位深不是 32 位浮点（bits={bits}）")

        return None
    if flags and not flags & _FLAG_IS_FLOAT:
        _reject(f"音频不是浮点格式（flags={flags}）")

        return None
    if flags & _FLAG_NON_INTERLEAVED:
        _reject("音频是分声道存放的缓冲（非交错），本版本只吃交错帧")

        return None

    data_buf = cm["CMSampleBufferGetDataBuffer"](sample_buffer)
    if data_buf is None:
        _reject("这帧没有数据块缓冲（CMSampleBufferGetDataBuffer 返回空）")

        return None

    length = int(cm["CMBlockBufferGetDataLength"](data_buf))
    if length <= 0:
        _reject(f"这帧的音频数据长度为 {length}")

        return None
    if length % (_BYTES_PER_SAMPLE * channels) != 0:
        _reject(f"数据长度 {length} 不是 声道数×4 字节的整数倍，按非预期格式跳过")

        return None

    buf = (ctypes.c_char * length)()
    result = cm["CMBlockBufferCopyDataBytes"](data_buf, 0, length, buf)
    # 返回 (OSStatus, buffer)；非 0 就是拷贝失败，绝不能把没填过的缓冲当音频用
    status = int(result[0] if isinstance(result, tuple) else result)
    if status != 0:
        _reject(f"拷贝 PCM 失败（OSStatus={status}）")

        return None

    samples = _interleaved_to_mono(bytes(buf), channels)
    if len(samples) == 0:
        _reject("拷贝后没有样本")

        return None
    return samples, rate
