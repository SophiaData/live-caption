"""Mach-O 文件头解析：读出一个二进制里到底装了哪些 CPU 架构切片。

Issue #20 的构建后架构门用它判定「这个产物是不是真的 Apple Silicon 原生」。
不用 `lipo`/`otool` 的原因：门必须能在任何机器上被单元测试覆盖（合成字节即可），
也不该把发布判定绑在 Xcode 命令行工具链上。只读文件头的表（几十字节），
所以对着几百 MB 的 Qt 动态库也几乎不花时间。

结论与 `lipo -archs` 一致（CI 里仍会原样打印 `lipo -archs` 作为交叉印证的发布证据）。

常量取自 `<mach/machine.h>` 与 `<mach/loader.h>`；未知架构一律原样打印成
`cpu0x……`，让它必然不满足要求（门是「不合格就失败」，不是「认不出来就放过」）。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO, Final, Literal

# thin Mach-O magic：大端读出来是 0xfeedface/0xfeedfacf，小端文件读出来是它的字节序反转
_MH_MAGIC_32: Final = 0xFEEDFACE
_MH_MAGIC_64: Final = 0xFEEDFACF
_MH_CIGAM_32: Final = 0xCEFAEDFE
_MH_CIGAM_64: Final = 0xCFFAEDFE
# fat（多架构）Mach-O magic
_FAT_MAGIC: Final = 0xCAFEBABE
_FAT_MAGIC_64: Final = 0xCAFEBABF
_FAT_CIGAM: Final = 0xBEBAFECA
_FAT_CIGAM_64: Final = 0xBFBAFECA

_THIN_MAGICS: Final[dict[int, str]] = {
    _MH_MAGIC_32: ">",
    _MH_CIGAM_32: "<",
    _MH_MAGIC_64: ">",
    _MH_CIGAM_64: "<",
}
# magic -> (字段字节序, 是不是 fat_arch_64)
_FAT_MAGICS: Final[dict[int, tuple[str, bool]]] = {
    _FAT_MAGIC: (">", False),
    _FAT_CIGAM: ("<", False),
    _FAT_MAGIC_64: (">", True),
    _FAT_CIGAM_64: ("<", True),
}

# CPU_TYPE_*：低位是 CPU 家族，0x01000000 表示 64 位 ABI
_CPU_TYPE_ARM: Final = 0x0000000C
_CPU_TYPE_ARM64: Final = 0x0100000C
_CPU_TYPE_X86: Final = 0x00000007
_CPU_TYPE_X86_64: Final = 0x01000007
_CPU_TYPE_PPC: Final = 0x00000012
_CPU_TYPE_PPC64: Final = 0x01000012

# CPU_SUBTYPE_* 里真正表示架构的低位（高位是能力位/硬件位，lipo 同样忽略）
_CPU_SUBTYPE_MASK: Final = 0x00000FFF
_CPU_SUBTYPE_ARM64E: Final = 0x00000002
_CPU_SUBTYPE_X86_64_H: Final = 0x00000008

_ARM_SUBTYPE_NAMES: Final[dict[int, str]] = {
    0: "arm",
    5: "armv6",
    6: "armv6m",
    8: "armv7f",
    9: "armv7",
    10: "armv7s",
    11: "armv7k",
    12: "armv8",
}

#: 判定「Apple Silicon 原生」时算数的架构标签（arm64e 是 arm64 的指针认证变体，同为原生）
ARM64_LABELS: Final[frozenset[str]] = frozenset({"arm64", "arm64e"})
X86_64_LABELS: Final[frozenset[str]] = frozenset({"x86_64", "x86_64h"})

#: 门支持的架构要求 -> 该家族的标签集合；写别的架构直接报错，不猜
_ARCH_FAMILIES: Final[dict[str, frozenset[str]]] = {
    "arm64": ARM64_LABELS,
    "x86_64": X86_64_LABELS,
}

#: 架构门允许的 `--require` 取值（命令行用它做可选值，测试用它做变异靶子）
SUPPORTED_REQUIREMENTS: Final[tuple[str, ...]] = tuple(sorted(_ARCH_FAMILIES))

#: fat 头部声明的切片数上限：超过就当文件头不可信（畸形头不能变成「静默放行」）
_MAX_FAT_ARCHES: Final = 64

# struct 的字节序符号 -> int.from_bytes 的 byteorder
_BYTEORDER: Final[dict[str, Literal["little", "big"]]] = {"<": "little", ">": "big"}

#: 一次读多少文件头：12 字节正好够 thin Mach-O 的 magic + cputype + cpusubtype
_HEADER_BYTES: Final = 12
#: fat 头里 magic 与切片数各 4 字节
_FAT_HEADER_BYTES: Final = 8


class MachOError(Exception):
    """这个文件读不出架构：不是 Mach-O、太小、或头部畸形。"""


def required_family(require: str) -> frozenset[str]:
    """把「要求的架构」翻译成对应的标签集合；不认识的要求直接报错，不猜。"""
    try:
        return _ARCH_FAMILIES[require]
    except KeyError:
        known = "、".join(sorted(_ARCH_FAMILIES))
        raise ValueError(f"不认识的架构要求 {require!r}，只支持：{known}") from None


def contains(arch_labels: tuple[str, ...], require: str) -> bool:
    """切片里是否**包含**要求的架构（universal2 的 `x86_64 arm64` 算包含）。"""
    return bool(set(arch_labels) & required_family(require))


def only_family(arch_labels: tuple[str, ...], require: str) -> bool:
    """切片是否**只有**要求架构家族：挡住误打的 Intel 包，也挡住 universal2。"""
    family = required_family(require)
    return bool(arch_labels) and set(arch_labels) <= family


def render(arch_labels: tuple[str, ...]) -> str:
    """把切片渲染成一行，顺序与 `lipo -archs` 一致（文件里切片的排列顺序）。"""
    return " ".join(arch_labels) if arch_labels else "（无架构切片）"


def _arch_name(cputype: int, cpusubtype: int) -> str:
    subtype = cpusubtype & _CPU_SUBTYPE_MASK
    if cputype == _CPU_TYPE_ARM64:
        return "arm64e" if subtype == _CPU_SUBTYPE_ARM64E else "arm64"
    if cputype == _CPU_TYPE_X86_64:
        return "x86_64h" if subtype == _CPU_SUBTYPE_X86_64_H else "x86_64"
    if cputype == _CPU_TYPE_ARM:
        return _ARM_SUBTYPE_NAMES.get(subtype, f"arm(sub={subtype})")
    if cputype == _CPU_TYPE_X86:
        return "i386"
    if cputype == _CPU_TYPE_PPC64:
        return "ppc64"
    if cputype == _CPU_TYPE_PPC:
        return "ppc"
    return f"cpu0x{cputype:08x}"


def _read_thin(head: bytes, endian: str) -> tuple[str, ...]:
    """thin Mach-O：magic(4) + cputype(4) + cpusubtype(4)，全在已读到的头里。

    magic 已在调用处比对过（`_THIN_MAGICS` 认它），这里只按同一字节序取后面两个字段。
    """
    _magic, cputype, cpusubtype = struct.unpack(f"{endian}Iii", head[:_HEADER_BYTES])
    return (_arch_name(cputype, cpusubtype),)


def _read_fat(stream: BinaryIO, head: bytes, endian: str, wide: bool, size: int) -> tuple[str, ...]:
    """fat Mach-O：fat_header + nfat_arch 个 fat_arch（或 fat_arch_64）表项。"""
    entry_size = 32 if wide else 20
    fmt = f"{endian}iiQQII" if wide else f"{endian}iiIII"
    count = int.from_bytes(head[4:_FAT_HEADER_BYTES], _BYTEORDER[endian])
    if count <= 0 or count > _MAX_FAT_ARCHES:
        raise MachOError(f"fat 头部声明的切片数不可信：{count}（允许 1~{_MAX_FAT_ARCHES}）")
    stream.seek(_FAT_HEADER_BYTES)
    table = stream.read(count * entry_size)
    if len(table) < count * entry_size:
        raise MachOError(f"fat 切片表不完整（整个文件只有 {size} 字节）")
    labels: list[str] = []
    for index in range(count):
        fields = struct.unpack(fmt, table[index * entry_size : (index + 1) * entry_size])
        cputype, cpusubtype, offset, slice_size = fields[0], fields[1], fields[2], fields[3]
        if offset + slice_size > size:
            raise MachOError(
                f"fat 第 {index + 1} 个切片越界（偏移 {offset} + 长度 {slice_size} > 文件 {size}）"
            )
        name = _arch_name(cputype, cpusubtype)
        if name not in labels:
            labels.append(name)
    return tuple(labels)


def _require_head(head: bytes, needed: int) -> bytes:
    if len(head) < needed:
        raise MachOError(f"文件头不足 {needed} 字节（整个文件只有 {len(head)} 字节），不是 Mach-O")
    return head


def read_arch_labels(path: Path) -> tuple[str, ...]:
    """读出文件的架构切片标签（顺序同 `lipo -archs`）；读不出就抛 `MachOError`。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise MachOError(f"读不到文件：{exc}") from exc
    try:
        with path.open("rb") as stream:
            head = stream.read(_HEADER_BYTES)
            if len(head) < 4:
                raise MachOError(f"文件太小（{len(head)} 字节），不是 Mach-O")
            magic = int.from_bytes(head[:4], "big")
            thin_endian = _THIN_MAGICS.get(magic)
            if thin_endian is not None:
                return _read_thin(_require_head(head, _HEADER_BYTES), thin_endian)
            fat = _FAT_MAGICS.get(magic)
            if fat is not None:
                return _read_fat(
                    stream, _require_head(head, _FAT_HEADER_BYTES), fat[0], fat[1], size
                )
            raise MachOError(f"不是 Mach-O 二进制（头 4 字节 = 0x{magic:08x}）")
    except OSError as exc:
        raise MachOError(f"读文件失败：{exc}") from exc
