"""音频设备探测与选择。

职责：
- 列出输入设备，识别"系统声音回环"类设备（BlackHole / loopback / WeMeet 等）
- 找不到时给出人话提示（含 Mac 安装 BlackHole 的指引）
"""

from __future__ import annotations

from dataclasses import dataclass

MAC_LOOPBACK_NAMES = ("blackhole", "loopback", "wemeet audio", "soundflower")
WIN_LOOPBACK_NAMES = ("loopback", "stereo mix", "wemeet audio")

# 采样率与声道：识别 API 的通用输入格式
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_MS = 10  # 每块 10ms → 每秒约 100 块
BLOCKSIZE = SAMPLE_RATE * BLOCK_MS // 1000


@dataclass(frozen=True)
class AudioDevice:
    """一个可用于捕获的输入设备。"""

    index: int
    name: str
    max_input_channels: int


def find_loopback_devices(names_and_channels: list[tuple[int, str, int]]) -> list[AudioDevice]:
    """从 (index, name, max_input_channels) 列表中挑出回环类设备。

    匹配规则：设备名包含任一回环关键词（大小写不敏感）且有输入声道。
    """
    keywords = (*MAC_LOOPBACK_NAMES, *WIN_LOOPBACK_NAMES)
    return [
        AudioDevice(index=i, name=name, max_input_channels=ch)
        for i, name, ch in names_and_channels
        if ch > 0 and any(k in name.lower() for k in keywords)
    ]


def pick_device(names_and_channels: list[tuple[int, str, int]]) -> AudioDevice | None:
    """选出首选捕获设备：优先名字最短的回环设备（BlackHole 2ch 优先于 16ch 等）。"""
    devices = find_loopback_devices(names_and_channels)
    if not devices:
        return None
    return min(devices, key=lambda d: (len(d.name), d.index))


def missing_device_hint() -> str:
    """找不到回环设备时给用户的人话指引。"""
    return (
        "没找到能捕获系统声音的设备。\n"
        "Mac 请安装免费的 BlackHole 虚拟声卡：https://existential.audio/blackhole/ ，"
        "安装后在「系统设置→声音→输出」建一个「多输出设备」（含 BlackHole 2ch + 你的扬声器），"
        "再回到本软件重试。Windows 会自动使用系统声音回环，无需安装。"
    )
