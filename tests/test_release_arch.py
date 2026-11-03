"""构建期架构门的用例（Issue #20）。

合成 Mach-O 字节即可覆盖：不需要 Xcode 工具链，也不需要真的打一个 .app。
门的行为按「不合格必须变红」来钉——把判据改弱（比如让动态库不再检查架构）这些用例会立刻失败。
"""

from __future__ import annotations

import platform
import plistlib
import struct
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.release import arch as arch_gate
from app.release import macho
from app.release.arch import GateError, build_report, inspect_interpreter, main
from app.release.macho import MachOError, read_arch_labels

# <mach/machine.h>：CPU_TYPE_*
CPU_X86: int = 0x00000007
CPU_X86_64: int = 0x01000007
CPU_ARM: int = 0x0000000C
CPU_ARM64: int = 0x0100000C
# <mach/loader.h>：magic
MH_MAGIC_64: int = 0xFEEDFACF
MH_MAGIC_32: int = 0xFEEDFACE
FAT_MAGIC: int = 0xCAFEBABE
FAT_MAGIC_64: int = 0xCAFEBABF

_SUBTYPE_ARM64_ALL = 0
_SUBTYPE_ARM64_E = 2
_SUBTYPE_X86_64_ALL = 3
_SUBTYPE_X86_64_H = 8


def thin_binary(cputype: int, cpusubtype: int = _SUBTYPE_ARM64_ALL, *, endian: str = "<") -> bytes:
    """造一个能读出架构的 thin Mach-O 假文件（头 12 字节是解析需要的，后面是填充）。"""
    magic = MH_MAGIC_64 if cputype & 0x01000000 else MH_MAGIC_32
    return struct.pack(f"{endian}IiiII", magic, cputype, cpusubtype, 2, 0) + b"\x00" * 64


def fat_binary(entries: Sequence[tuple[int, int]], *, wide: bool = False) -> bytes:
    """造一个 fat（多架构）假文件；entries 是 (cputype, cpusubtype) 列表，顺序即切片顺序。"""
    fmt = ">iiQQII" if wide else ">iiIII"
    magic = FAT_MAGIC_64 if wide else FAT_MAGIC
    table = b""
    offset = 8192
    for cputype, cpusubtype in entries:
        args = (
            (cputype, cpusubtype, offset, 64, 0, 0)
            if wide
            else (cputype, cpusubtype, offset, 64, 0)
        )
        table += struct.pack(fmt, *args)
        offset += 64
    header = struct.pack(">II", magic, len(entries)) + table
    return header + b"\x00" * max(0, offset - len(header))


def make_app(
    root: Path,
    *,
    main_bytes: bytes,
    libs: dict[str, bytes],
    version: str = "0.1.0",
    executable: str = "live-caption",
) -> Path:
    """造一个最小 .app 目录树，够架构门用（主程序 + 任意位置的内嵌动态库）。"""
    bundle = root / "live-caption.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    plist = {
        "CFBundleExecutable": executable,
        "CFBundleIdentifier": "com.sophiadata.livecaption",
        "CFBundleShortVersionString": version,
    }
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))
    (bundle / "Contents" / "MacOS" / executable).write_bytes(main_bytes)
    for rel, data in libs.items():
        path = bundle / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return bundle


ARM64_MAIN = thin_binary(CPU_ARM64)
X86_64_LIB = thin_binary(CPU_X86_64, _SUBTYPE_X86_64_ALL)
UNIVERSAL2_LIB = fat_binary([(CPU_X86_64, _SUBTYPE_X86_64_ALL), (CPU_ARM64, _SUBTYPE_ARM64_ALL)])


# ---------------------------------------------------------------- macho 解析


@pytest.mark.parametrize(
    ("cputype", "subtype", "expected"),
    [
        (CPU_ARM64, _SUBTYPE_ARM64_ALL, "arm64"),
        (CPU_ARM64, _SUBTYPE_ARM64_E, "arm64e"),
        (CPU_X86_64, _SUBTYPE_X86_64_ALL, "x86_64"),
        (CPU_X86_64, _SUBTYPE_X86_64_H, "x86_64h"),
        (CPU_X86, 3, "i386"),
        (CPU_ARM, 9, "armv7"),
    ],
)
def test_thin_labels(tmp_path: Path, cputype: int, subtype: int, expected: str) -> None:
    path = tmp_path / "lib.so"
    path.write_bytes(thin_binary(cputype, subtype))
    assert read_arch_labels(path) == (expected,)


@pytest.mark.parametrize("endian", ["<", ">"])
def test_thin_reads_both_byte_orders(tmp_path: Path, endian: str) -> None:
    path = tmp_path / "lib.so"
    path.write_bytes(thin_binary(CPU_ARM64, endian=endian))
    assert read_arch_labels(path) == ("arm64",)


def test_fat_keeps_slice_order(tmp_path: Path) -> None:
    path = tmp_path / "lib.dylib"
    path.write_bytes(
        fat_binary([(CPU_X86_64, _SUBTYPE_X86_64_ALL), (CPU_ARM64, _SUBTYPE_ARM64_ALL)])
    )
    assert read_arch_labels(path) == ("x86_64", "arm64")


def test_fat64(tmp_path: Path) -> None:
    path = tmp_path / "lib.dylib"
    path.write_bytes(fat_binary([(CPU_ARM64, _SUBTYPE_ARM64_ALL)], wide=True))
    assert read_arch_labels(path) == ("arm64",)


def test_fat_dedupes_same_arch_label(tmp_path: Path) -> None:
    path = tmp_path / "lib.dylib"
    path.write_bytes(fat_binary([(CPU_ARM64, 0), (CPU_ARM64, 1)]))
    assert read_arch_labels(path) == ("arm64",)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x7fELF" + b"\x00" * 40,
        b"#!/bin/sh\necho hi\n",
        struct.pack(">I", FAT_MAGIC) + b"\x00" * 4,
        ARM64_MAIN[:8],
    ],
    ids=["空文件", "Linux ELF", "脚本", "fat 头读不到切片表", "thin 头不完整"],
)
def test_unreadable_files_raise(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "lib.so"
    path.write_bytes(data)
    with pytest.raises(MachOError):
        read_arch_labels(path)


def test_absurd_slice_count_is_error(tmp_path: Path) -> None:
    path = tmp_path / "lib.so"
    path.write_bytes(struct.pack(">II", FAT_MAGIC, 100_000) + b"\x00" * 64)
    with pytest.raises(MachOError, match="切片数不可信"):
        read_arch_labels(path)


def test_slice_out_of_bounds_is_error(tmp_path: Path) -> None:
    path = tmp_path / "lib.so"
    path.write_bytes(
        struct.pack(">II", FAT_MAGIC, 1) + struct.pack(">iiIII", CPU_ARM64, 0, 10**9, 9, 0)
    )
    with pytest.raises(MachOError, match="越界"):
        read_arch_labels(path)


def test_missing_file_is_error(tmp_path: Path) -> None:
    with pytest.raises(MachOError, match="读不到文件"):
        read_arch_labels(tmp_path / "nope.so")


def test_unknown_cputype_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "lib.so"
    path.write_bytes(thin_binary(0x010000FF))
    labels = read_arch_labels(path)
    assert labels[0].startswith("cpu0x")
    assert not macho.contains(labels, "arm64")


# ------------------------------------------------------- 判定：包含 / 只有


def test_universal2_contains_arm64_but_is_not_arm64_only() -> None:
    labels = ("x86_64", "arm64")
    assert macho.contains(labels, "arm64") is True
    assert macho.only_family(labels, "arm64") is False


def test_arm64e_counts_as_apple_silicon() -> None:
    assert macho.only_family(("arm64e",), "arm64") is True


def test_intel_only_fails_arm64_requirement() -> None:
    assert macho.contains(("x86_64",), "arm64") is False


def test_empty_labels_never_pass() -> None:
    assert macho.contains((), "arm64") is False
    assert macho.only_family((), "arm64") is False


def test_unknown_requirement_raises() -> None:
    with pytest.raises(ValueError, match="不认识的架构要求"):
        macho.required_family("universal2")


# ------------------------------------------------------- 门之一：构建前解释器


def test_interpreter_gate_passes_on_arm64_mac() -> None:
    verdict = inspect_interpreter(
        system="darwin", machine="arm64", executable="/x/bin/python", require="arm64"
    )
    assert verdict.ok and not verdict.skipped
    assert "合格" in verdict.message


def test_interpreter_gate_fails_humanely_on_intel_mac() -> None:
    verdict = inspect_interpreter(
        system="darwin", machine="x86_64", executable="/x/bin/python", require="arm64"
    )
    assert verdict.ok is False
    assert "不能在 Apple 芯片上原生运行" in verdict.message
    assert "make setup PYTHON=" in verdict.message  # 人话里带下一步怎么办
    assert "ALLOW_INTEL=1" in verdict.message  # 明确告诉他绕过的方式（而不是偷偷放行）


def test_interpreter_gate_bypass_is_explicit_and_visible() -> None:
    verdict = inspect_interpreter(
        system="darwin",
        machine="x86_64",
        executable="/x/bin/python",
        require="arm64",
        allow_intel=True,
    )
    assert verdict.ok is True
    assert "已绕过" in verdict.message
    assert "不能" in verdict.message and "本地调试" in verdict.message


def test_interpreter_gate_skips_non_macos_so_linux_ci_is_unaffected() -> None:
    for system, machine in (("linux", "x86_64"), ("win32", "AMD64")):
        verdict = inspect_interpreter(
            system=system, machine=machine, executable="/x/bin/python", require="arm64"
        )
        assert verdict.ok and verdict.skipped
        assert "跳过" in verdict.message


def test_interpreter_gate_require_is_the_judgement_itself() -> None:
    """`--require` 是判据，不是装饰：换成 x86_64 时 Intel 解释器才合格。"""
    intel = inspect_interpreter(
        system="darwin", machine="x86_64", executable="/x", require="x86_64"
    )
    apple = inspect_interpreter(system="darwin", machine="arm64", executable="/x", require="x86_64")
    assert intel.ok is True
    assert apple.ok is False


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),
        ({"ALLOW_INTEL": "1"}, True),
        ({"ALLOW_INTEL": "true"}, True),
        ({"ALLOW_INTEL": "0"}, False),
        ({"ALLOW_INTEL": ""}, False),
    ],
)
def test_allow_intel_env_reading(env: dict[str, str], expected: bool) -> None:
    assert arch_gate.allow_intel_from_env(env) is expected


def test_cli_interpreter_exits_zero_on_supported_build_hosts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """合法的构建机（macOS+arm64，或非 macOS）跑这条命令必须是 0。"""
    if sys.platform == "darwin" and platform.machine() not in macho.ARM64_LABELS:
        pytest.skip("本机是 Intel 解释器：这道门本来就该红，这正是 Issue #20 要的行为")
    assert main(["interpreter"]) == 0
    assert capsys.readouterr().out.strip()


# ------------------------------------------------------- 门之二：构建后产物


def _good_bundle(root: Path) -> Path:
    return make_app(
        root,
        main_bytes=ARM64_MAIN,
        libs={
            "Contents/Frameworks/libpython3.12.dylib": ARM64_MAIN,
            "Contents/Resources/PySide6/QtCore.abi3.so": UNIVERSAL2_LIB,  # Qt 轮子是 universal2
            "Contents/Resources/_sounddevice_data/deep/nested/numpy/_core/_multiarray_umath.so": (
                ARM64_MAIN
            ),
        },
    )


def test_good_bundle_passes(tmp_path: Path) -> None:
    report = build_report(_good_bundle(tmp_path), "arm64")
    assert report.ok, report.problems
    assert report.main.rel == "Contents/MacOS/live-caption"
    assert report.bundle_version == "0.1.0"


def test_forged_intel_only_library_fails_the_gate(tmp_path: Path) -> None:
    """Issue #20 验收第 2 条的机器版：往产物里塞一个只含 x86_64 的 .so，门必须红。"""
    bundle = _good_bundle(tmp_path)
    forged = bundle / "Contents" / "Resources" / "libimpostor.dylib"
    forged.write_bytes(X86_64_LIB)
    report = build_report(bundle, "arm64")
    assert report.ok is False
    problems = {item.rel: item.problem for item in report.problems}
    assert "Contents/Resources/libimpostor.dylib" in problems
    assert "不含 arm64" in problems["Contents/Resources/libimpostor.dylib"]


def test_universal2_main_executable_fails_arm64_only_rule(tmp_path: Path) -> None:
    """主程序要求「只有 arm64」：误打成 universal2 也算不合格（老板已定不做 universal2）。"""
    bundle = _good_bundle(tmp_path)
    (bundle / "Contents" / "MacOS" / "live-caption").write_bytes(UNIVERSAL2_LIB)
    report = build_report(bundle, "arm64")
    assert report.ok is False
    assert "只有 arm64" in report.main.problem


@pytest.mark.parametrize(
    "junk",
    [b"#!/bin/sh\nnot a binary\n", b"", b"\x7fELF" + b"\x00" * 40],
)
def test_non_macho_library_fails_gate(tmp_path: Path, junk: bytes) -> None:
    """认不出架构的文件不许静默放过——门是「不合格就失败」，不是「看不见就没事」。"""
    bundle = _good_bundle(tmp_path)
    (bundle / "Contents" / "Frameworks" / "libjunk.dylib").write_bytes(junk)
    report = build_report(bundle, "arm64")
    assert report.ok is False
    assert any("读不出架构" in item.problem for item in report.problems)


def test_broken_symlink_fails_gate(tmp_path: Path) -> None:
    bundle = _good_bundle(tmp_path)
    link = bundle / "Contents" / "Frameworks" / "libghost.dylib"
    link.symlink_to(bundle / "Contents" / "Frameworks" / "nowhere.dylib")
    report = build_report(bundle, "arm64")
    assert report.ok is False
    assert any("libghost.dylib" == Path(item.rel).name for item in report.problems)


def test_reversed_requirement_fails_on_arm64_bundle(tmp_path: Path) -> None:
    """把判据反过来要求 x86_64，arm64 的包必须红：证明红绿是产物决定的，不是写死的。"""
    bundle = _good_bundle(tmp_path)
    assert build_report(bundle, "arm64").ok is True
    report = build_report(bundle, "x86_64")
    assert report.ok is False
    assert "不含 x86_64" in report.main.problem


def test_gate_rejects_unknown_requirement(tmp_path: Path) -> None:
    bundle = _good_bundle(tmp_path)
    with pytest.raises(ValueError, match="不认识的架构要求"):
        build_report(bundle, "universal2")


def test_bundle_without_any_library_is_rejected(tmp_path: Path) -> None:
    """一个 `.so`/`.dylib` 都扫不到 = 打包步骤坏了，不许当成「没东西可查所以通过」。"""
    bundle = make_app(tmp_path, main_bytes=ARM64_MAIN, libs={})
    with pytest.raises(GateError, match="都没找到"):
        build_report(bundle, "arm64")


@pytest.mark.parametrize(
    ("broken", "match"),
    [
        ("not_a_dir", "不是一个目录"),
        ("no_plist", "没有 Contents/Info.plist"),
        ("bad_plist", "CFBundleExecutable"),
        ("missing_main", "找不到主程序"),
    ],
)
def test_gate_errors_when_input_is_not_a_bundle(tmp_path: Path, broken: str, match: str) -> None:
    if broken == "not_a_dir":
        target = tmp_path / "live-caption.app"
        target.write_bytes(b"not a directory")
    elif broken == "no_plist":
        target = tmp_path / "live-caption.app"
        (target / "Contents" / "MacOS").mkdir(parents=True)
    elif broken == "bad_plist":
        target = make_app(tmp_path, main_bytes=ARM64_MAIN, libs={})
        (target / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleName": "x"}))
    else:
        target = make_app(tmp_path, main_bytes=ARM64_MAIN, libs={})
        plist = plistlib.loads((target / "Contents" / "Info.plist").read_bytes())
        plist["CFBundleExecutable"] = "ghost"  # 指向一个不存在的主程序
        (target / "Contents" / "Info.plist").write_bytes(plistlib.dumps(plist))
    with pytest.raises(GateError, match=match):
        build_report(target, "arm64")


def test_cli_bundle_prints_full_evidence_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _good_bundle(tmp_path)
    assert main(["bundle", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert "要求 arm64" in out
    assert "Contents/MacOS/live-caption : arm64" in out
    assert "Contents/Resources/PySide6/QtCore.abi3.so : x86_64 arm64" in out
    assert "结论：PASS" in out
    assert "3 个内嵌动态库全部含 arm64" in out


def test_cli_bundle_exits_one_on_forged_library(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _good_bundle(tmp_path)
    (bundle / "Contents" / "Resources" / "libimpostor.dylib").write_bytes(X86_64_LIB)
    assert main(["bundle", str(bundle)]) == 1
    out = capsys.readouterr().out
    assert "结论：FAIL" in out
    assert "Contents/Resources/libimpostor.dylib" in out
    assert "不含 arm64" in out


def test_cli_gate_cannot_be_silently_disabled(tmp_path: Path) -> None:
    """`--require` 只接受 arm64/x86_64：想传 universal2/none 之类绕过，命令行直接拒。"""
    with pytest.raises(SystemExit) as exit_info:
        main(["bundle", str(_good_bundle(tmp_path)), "--require", "universal2"])
    assert exit_info.value.code == 2


def test_library_files_sorted_and_relative(tmp_path: Path) -> None:
    bundle = _good_bundle(tmp_path)
    rels = [str(path.relative_to(bundle)) for path in arch_gate.library_files(bundle)]
    assert rels == sorted(rels)
    assert len(rels) == 3
