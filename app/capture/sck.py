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

音频样本链：CMSampleBuffer → 块缓冲 float32（真机是**分声道连续**，交错也支持）→ numpy 单声道 → 16k

Issue #22（点「开始捕获」放音即 SIGSEGV）：本模块的硬约束是
**CoreMedia 桥的首次加载必须发生在开流之前的主线程**（`load_coremedia()`）——
`import CoreMedia` 时它的 `_setup()` 会去加载框架、注册包装类与元数据，这活儿发生在
ScreenCaptureKit 自己那条音频队列线程上就会崩（真机证据见 docs/qa/issue-22/）。
解析过程中每一步的裸指针都判空、返回码都判、格式也判；取不到就跳帧，并把原因写成人话
告警 + 计数（`drop_report()`），不再静默丢帧。
"""

from __future__ import annotations

import ctypes
import logging
import platform
import sys
import threading
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

MIN_MACOS_MAJOR = 13
TARGET_SAMPLE_RATE = 16000
SCK_OUTPUT_TYPE_AUDIO = 1  # 已核实：SCStreamOutputTypeAudio = 1

# 本模块用到的 CoreMedia 函数（名字固定；首次解析必须在主线程完成，见 load_coremedia）
_COREMEDIA_FUNCTIONS = (
    "CMSampleBufferGetFormatDescription",
    "CMFormatDescriptionGetMediaType",
    "CMAudioFormatDescriptionGetStreamBasicDescription",
    "CMSampleBufferGetDataBuffer",
    "CMBlockBufferGetDataLength",
    "CMBlockBufferCopyDataBytes",
)

_CM: dict[str, Any] | None = None
_CM_MODULE: Any = None

# 解析失败的记账：**稳定原因码**做键（不要用整句人话——里面一带数值就成了无限多的键，
# 限流也就失效了），人话文案与计数分开。见 _reject()。
_drops: dict[str, int] = {}
_drop_details: dict[str, str] = {}
_drop_lock = threading.Lock()
_WARN_EVERY = 100  # 同一原因每累计 100 帧再提醒一次，避免 50 帧/秒刷屏

# 真机实测（2026-11-04，本机 macOS 27.2，SCK 送来的音频帧）：
# ASBD = 采样率 16000 / formatID 'lpcm' / flags 41 / 2 声道 / 32 位，
# mBytesPerFrame=4（**每声道** 4 字节，不是每帧 8）、块缓冲 2560 字节/320 帧（= 20ms）。
# flags 41 按 CoreAudio 的常量逐位拆开是
# kLinearPCMFormatFlagIsFloat(1) | kLinearPCMFormatFlagIsPacked(8)
# | kLinearPCMFormatFlagIsNonInterleaved(32)
# ——**真机送来的是「分声道连续存放」（planar），不是交错**。这条是用「左 1000Hz、右 3000Hz」
# 的立体声测出来的：块缓冲前半的频谱峰只有 1000Hz、后半只有 3000Hz（交错的话两半都会
# 同时含 1000 与 3000），见 docs/qa/issue-22/logs/channel-layout-measured.txt。
_LPCM_FORMAT_ID = 1819304813  # 'lpcm'
_MEDIA_TYPE_SOUN = 1936684398  # 'soun'（真机实测：CMFormatDescriptionGetMediaType 回这个）
_FLAG_IS_FLOAT = 1 << 0  # kLinearPCMFormatFlagIsFloat
_FLAG_NON_INTERLEAVED = 1 << 5  # kLinearPCMFormatFlagIsNonInterleaved（注意不是 1<<4）
_BYTES_PER_SAMPLE = 4


# 跳帧原因码 → 人话（进日志的文案）
_DROP_REASONS = {
    "EMPTY_BUFFER": "SCK 回调给了空 sample buffer",
    "BRIDGE_MISSING": "CoreMedia 桥没在开流前于主线程加载（拒绝在回调线程现场加载）",
    "BRIDGE_CALL_FAILED": "调用 CoreMedia 时出错",
    "NO_FORMAT": "这帧没有格式描述（CMSampleBufferGetFormatDescription 返回空）",
    "NO_ASBD": "格式描述里没有 ASBD（取采样率/声道数的依据）",
    "BAD_ASBD_SHAPE": "ASBD 形状不认识，读不出采样率/声道数",
    "BAD_RATE_OR_CHANNELS": "ASBD 里的采样率或声道数不可用",
    "NOT_LPCM": "音频不是线性 PCM",
    "NOT_FLOAT32": "音频位深不是 32 位浮点",
    "NOT_FLOAT": "音频不是浮点格式",
    "NOT_AUDIO_FORMAT": "格式描述不是音频（mediaType 不是 'soun'），不当 ASBD 来源",
    "LAYOUT_CONFLICT": "声道存放方式两个判据互相矛盾，不敢猜，跳帧",
    "NO_DATA_BLOCK": "这帧没有数据块缓冲（CMSampleBufferGetDataBuffer 返回空）",
    "EMPTY_DATA": "这帧的音频数据长度不大于 0",
    "BAD_DATA_LENGTH": "数据长度不是 声道数×4 字节的整数倍",
    "COPY_FAILED": "拷贝 PCM 失败（CoreMedia 返回码非 0）",
    "NO_SAMPLES": "拷贝后没有样本",
}


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


def _planar_to_mono(data: bytes, channels: int) -> np.ndarray:
    """分声道连续存放（planar）→ 单声道 float32。SCK 真机送来的就是这种（flags 带 32）。

    块缓冲里是 `channels` 段连续的单声道数据，每段 `样本数 × 4` 字节，取平均即单声道。
    planar 与交错读反了**不会崩**，但会把每个声道按 2 倍抽取（混叠 + 时长减半），
    识别明显变差——所以必须按 flags 分支，不能一律当交错解。
    """
    samples = np.frombuffer(data, dtype=np.float32)
    if channels == 1:
        return samples.copy()
    per_channel = len(samples) // channels
    if per_channel == 0:
        return np.array([], dtype=np.float32)
    stacked = samples[: per_channel * channels].reshape(channels, per_channel)
    return stacked.mean(axis=0).astype(np.float32)


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


def _cached_bridge() -> dict[str, Any] | None:
    """已加载就取缓存；`sys.modules` 里的 CoreMedia 被换掉（测试注入假件）就重解析。

    这里**不做 import**：热路径（SCK 回调线程）每帧只走到这里就返回，
    一次 `sys.modules` 查表 + 一次身份比较，没有加载动作、也没有 import 语句。
    """
    global _CM, _CM_MODULE
    module = sys.modules.get("CoreMedia")
    if module is None:
        return None
    if _CM is not None and _CM_MODULE is module:
        return _CM
    _CM = {name: getattr(module, name) for name in _COREMEDIA_FUNCTIONS}
    _CM_MODULE = module
    return _CM


def load_coremedia() -> dict[str, Any]:
    """把 CoreMedia 的取数据函数取好并缓存——**必须在开流之前、在主线程调用**。

    Issue #22 的实测结论（机制到「哪一次触碰不安全」这一层为止，更深的 PyObjC 内部
    原因没有定位，见 docs/qa/issue-22/README.md 的差分实验）：`import CoreMedia` 会跑
    它自己的 `_setup()`——加载框架镜像、注册包装类与函数符号，这过程要创建 ObjC 对象。
    这件事原先发生在 ScreenCaptureKit **自己创建**的那条音频队列线程上（崩溃报告线程名
    `com.screenCaptureKit.audioSampleHandlerQueue`）：那条线程不是 Python 建的，也没有
    ObjC autorelease pool。差分实验（同一压力脚本，一次只改一个变量）：
    现状 3/3 在首轮会话的最初几帧 SIGSEGV；只在主线程 `import CoreMedia` 0/6 轮；
    再额外解析函数符号 0/36 轮；只把回调包进 autorelease pool 0/28 轮。
    → 加载与池子是同一处问题的两面，两条防线都进代码。

    已经加载过时本函数只是一次字典查表 + 模块身份比较；测试用假 CoreMedia 顶掉
    `sys.modules` 时会自动重解析（这条自愈契约有注释钉在 tests/test_sck_real_buffer.py）。
    """
    import CoreMedia  # noqa: F401 - 这句的全部意义就是「在本线程把框架加载进来」

    bridge = _cached_bridge()
    assert bridge is not None  # 刚 import 完，sys.modules 里必然有
    return bridge


def coremedia_loaded() -> bool:
    """CoreMedia 桥是否已加载好。

    生产里唯一的调用点是 `extract_audio_samples()` 的兜底闸：没预热就**绝不在
    回调线程现场加载**（那正是 Issue #22 的崩溃场景）——宁可跳帧并大声报警，也不
    把「可能段错误」留在运行路径上。
    """
    return _CM is not None


def drop_report() -> dict[str, int]:
    """跳帧原因（人话）→ 累计帧数。测试与 soak 靠它证明没有「不吭声的丢帧」。"""
    with _drop_lock:
        return {_DROP_REASONS.get(code, code): n for code, n in _drops.items()}


def drop_details() -> dict[str, str]:
    """每个跳帧原因最后一次看到的细节（数值、OSStatus、异常文本）。给人看的是人话，
    给排查用的是这里——两者分开，限流的键才不会被飘动的数值撑爆。"""
    with _drop_lock:
        return dict(_drop_details)


def reset_drop_report() -> None:
    """清空记账（只在测试与诊断脚本里用）。"""
    with _drop_lock:
        _drops.clear()
        _drop_details.clear()


def _reject(code: str, detail: str = "") -> None:
    """记一笔「这帧没出音频」并保证人看得见：同一原因首次就 WARNING，之后每 100 帧再报一次。

    Issue #22 要求 2：不能把段错误换成静默丢帧。Python 抓不到 SIGSEGV，所以这里
    不是「兜住崩溃」，而是**在解引用裸指针之前**就不往下走，并把原因写成人话留下痕迹。
    键是稳定原因码（数值只进 detail），否则 50 帧/秒的飘动数值会把键撑爆、限流失效。
    """
    with _drop_lock:
        total = _drops.get(code, 0) + 1
        _drops[code] = total
        if detail:
            _drop_details[code] = detail
        first = total == 1
        due = total % _WARN_EVERY == 0
        shown = _drop_details.get(code, "")
    reason = _DROP_REASONS.get(code, code)
    if first:
        logger.warning(
            "音频帧被跳过：%s%s（同类已累计 1 帧）", reason, f"：{shown}" if shown else ""
        )
    elif due:
        logger.warning("音频帧仍在被跳过：%s（已累计 %d 帧，最后一次：%s）", reason, total, shown)


def _asbd_fields(asbd: Any) -> tuple[int, int, int, int, int, int] | None:
    """ASBD → (采样率, 声道数, formatID, flags, 每声道位深, 每帧字节数)；读不出来返回 None。

    PyObjC 13 在真机上把这个结构体摊平成 9 元素元组（实测，见 docs/qa/issue-22）：
    `[采样率, formatID, flags, bytesPerPacket, framesPerPacket, bytesPerFrame, 声道数,
    位深, reserved]`。旧绑定给的是结构体对象，两种形状都支持。

    取不到的字段按 0 = 「不知道」处理，由调用方决定跳过；不猜、不填默认值。
    **降级要心里有数**：如果哪天绑定只回 7 元素，`at(7)`（位深）与 `at(5)`
    （bytesPerFrame）都会成 0，格式判据与「声道存放方式」判据就都退化成「不知道」，
    此时只剩采样率/声道数/flags 在把关。
    """
    try:
        if isinstance(asbd, tuple):

            def at(i: int) -> int:
                return int(asbd[i]) if i < len(asbd) else 0

            return at(0), at(6), at(1), at(2), at(7), at(5)
        return (
            int(asbd.mSampleRate),
            int(asbd.mChannelsPerFrame),
            int(asbd.mFormatID),
            int(asbd.mFormatFlags),
            int(asbd.mBitsPerChannel),
            int(asbd.mBytesPerFrame),
        )
    except Exception:  # noqa: BLE001 - 形状不认识就是「非预期格式」，由调用方跳帧并告警
        return None


def extract_audio_samples(sample_buffer: object) -> tuple[np.ndarray, int] | None:
    """从 CMSampleBuffer 抽 (float32 单声道数组, 采样率)；拿不到就跳帧并留人话记录。

    这条链上拿到的每一个都是 CoreMedia 裸指针，所以逐步判空、判返回码、判格式
    （Issue #22 要求 2）：空 sample buffer、空格式描述、空 ASBD、ASBD 形状不认识、
    采样率/声道数不可用、不是 32 位浮点交错 PCM、空数据块、长度不合法、拷贝返回码非 0
    ——全部跳帧 + `_reject()` 记账（人话进日志、计数进 `drop_report()`），
    绝不带着空/坏指针往下走。

    兜底闸：桥没预热时**绝不在非主线程现场加载**（那就是 Issue #22 的崩溃现场）；
    宁可跳帧大声报警，也不把可能段错误的动作留在运行路径上。
    """
    cm = _cached_bridge()

    if sample_buffer is None:
        _reject("EMPTY_BUFFER")
        return None

    if cm is None:
        # 没缓存：只有主线程允许现场加载（测试直接调用解析函数走这条）；
        # 非主线程就是「开流前没预热」的回归，宁可跳帧大声报警，绝不在回调线程加载。
        if threading.current_thread() is not threading.main_thread():
            _reject(
                "BRIDGE_MISSING", "开流前必须在主线程 load_coremedia()（SCKSession.start() 负责）"
            )
            return None
        cm = load_coremedia()

    try:
        fmt = cm["CMSampleBufferGetFormatDescription"](sample_buffer)
        if fmt is None:
            _reject("NO_FORMAT")
            return None
        # 先把「这不是音频格式描述」挡掉：拿非音频的 fmt 去取 ASBD 就是裸指针往下走
        media_type = int(cm["CMFormatDescriptionGetMediaType"](fmt))
        if media_type != _MEDIA_TYPE_SOUN:
            _reject("NOT_AUDIO_FORMAT", f"mediaType={media_type}")
            return None

        asbd = cm["CMAudioFormatDescriptionGetStreamBasicDescription"](fmt)
        if asbd is None:
            _reject("NO_ASBD")
            return None
        fields = _asbd_fields(asbd)
        if fields is None:
            _reject("BAD_ASBD_SHAPE", type(asbd).__name__)
            return None
        rate, channels, format_id, flags, bits, bytes_per_frame = fields
        if rate <= 0 or channels <= 0:
            _reject("BAD_RATE_OR_CHANNELS", f"rate={rate}, channels={channels}")
            return None
        # 字段为 0 当「不知道」处理（沿用历史行为）；一旦明确报出不是 32 位浮点交错 PCM，
        # 就必须跳帧——按 float32 硬解析 int16/分离声道数据会得到垃圾音频，那比丢一帧更糟。
        # 判据按真机实测写（flags=41、bits=32、2560 字节/帧），不误伤真帧。
        if format_id not in (0, _LPCM_FORMAT_ID):
            _reject("NOT_LPCM", f"formatID={format_id}")
            return None
        # 只支持 float32：float64（bits=64）等专业链路会在这里跳帧并吭声，这是取舍不是 bug
        if bits not in (0, 32):
            _reject("NOT_FLOAT32", f"bits={bits}")
            return None
        if flags and not flags & _FLAG_IS_FLOAT:
            _reject("NOT_FLOAT", f"flags={flags}")
            return None
        # 声道存放方式：**两个独立判据**都要看，因为它们来自两套不同的口径
        # （flags 是常量表，mBytesPerFrame 是数据本身）。真机两条都指 planar：
        # flags 带 32、mBytesPerFrame=4（每声道 4 字节）。互相矛盾就不猜、跳帧吭声。
        by_flag = bool(flags & _FLAG_NON_INTERLEAVED) if flags else None
        by_size: bool | None = None
        if bytes_per_frame > 0 and channels > 1:
            if bytes_per_frame == _BYTES_PER_SAMPLE:
                by_size = True
            elif bytes_per_frame == _BYTES_PER_SAMPLE * channels:
                by_size = False
        if by_flag is not None and by_size is not None and by_flag != by_size:
            _reject(
                "LAYOUT_CONFLICT",
                f"flags={flags}, bytesPerFrame={bytes_per_frame}, channels={channels}",
            )
            return None
        non_interleaved = bool(by_flag if by_flag is not None else by_size)

        data_buf = cm["CMSampleBufferGetDataBuffer"](sample_buffer)
        if data_buf is None:
            _reject("NO_DATA_BLOCK")
            return None

        length = int(cm["CMBlockBufferGetDataLength"](data_buf))
        if length <= 0:
            _reject("EMPTY_DATA", f"length={length}")
            return None
        if length % (_BYTES_PER_SAMPLE * channels) != 0:
            _reject("BAD_DATA_LENGTH", f"length={length}, channels={channels}")
            return None

        buf = (ctypes.c_char * length)()
        result = cm["CMBlockBufferCopyDataBytes"](data_buf, 0, length, buf)
        # 返回 (OSStatus, buffer)；非 0 就是拷贝失败，绝不能把没填过的缓冲当音频用
        status = int(result[0] if isinstance(result, tuple) else result)
        if status != 0:
            _reject("COPY_FAILED", f"OSStatus={status}")
            return None

        to_mono = _planar_to_mono if non_interleaved else _interleaved_to_mono
        samples = to_mono(bytes(buf), channels)
        if len(samples) == 0:
            _reject("NO_SAMPLES", f"length={length}, channels={channels}")
            return None
        return samples, rate
    except Exception as e:  # noqa: BLE001 - 只兜 Python 层异常（段错误兜不住，也不打算兜），照样记账吭声
        _reject("BRIDGE_CALL_FAILED", f"{type(e).__name__}: {e}")
        return None
