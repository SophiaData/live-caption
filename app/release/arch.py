"""构建期架构门（Issue #20）：打包前钉死「解释器是 arm64」，打包后钉死「产物是 arm64」。

背景：runner 一旦从 macos-14 换成 Intel，PyInstaller 就静默产出跑不动的 Intel 包，
以前没有任何一处代码能拦住这件事。这两道门把它变成机器保证：

    make check-arch          # 打包前：断言构建解释器架构（非 macOS 自动跳过）
    make verify-arch         # 打包后：逐个校验 .app 里的二进制架构，不合格即退出码 1

也可以直接调（CI 与 `live-caption.spec` 用的就是这条路径）：

    python -m app.release.arch interpreter --require arm64
    python -m app.release.arch bundle dist/live-caption.app --require arm64

退出码：0 = 通过或按平台跳过；1 = 不合格（硬失败，禁止发布）；2 = 命令行用法错误。
`--require` 就是判据本身（默认 arm64）；留着它，是为了让这道门能被「反向验证」——
拿它去要求 x86_64 打一个 arm64 的包，必须看到它红，证明门不是摆设。
"""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, TextIO

from app.release.macho import (
    SUPPORTED_REQUIREMENTS,
    MachOError,
    contains,
    only_family,
    read_arch_labels,
    render,
)

#: 动态库后缀：`.app` 里带这些后缀的文件都必须含要求的架构
LIBRARY_SUFFIXES: Final[tuple[str, ...]] = (".so", ".dylib")
_ALLOW_INTEL_ENV: Final = "ALLOW_INTEL"
#: 显式绕过时这些值不算绕过（ALLOW_INTEL=0 不等于允许 Intel）
_FALSY_ENV_VALUES: Final[frozenset[str]] = frozenset({"", "0", "false", "no"})
_INTEL_MACHINES: Final[frozenset[str]] = frozenset({"x86_64", "x86_64h", "i386"})


@dataclass(frozen=True)
class InterpreterVerdict:
    """打包前那道门的结论。"""

    ok: bool
    skipped: bool
    message: str


@dataclass(frozen=True)
class CheckItem:
    """产物里被检查过的一个文件；`problem` 非空 = 不合格。"""

    rel: str
    labels: tuple[str, ...]
    problem: str


@dataclass(frozen=True)
class BundleReport:
    """整包校验结果：主程序 + 全部内嵌动态库。"""

    bundle: Path
    require: str
    main: CheckItem
    libraries: tuple[CheckItem, ...]
    bundle_version: str

    @property
    def problems(self) -> tuple[CheckItem, ...]:
        return tuple(item for item in (self.main, *self.libraries) if item.problem)

    @property
    def ok(self) -> bool:
        return not self.problems


class GateError(Exception):
    """门本身跑不起来（给的不是 .app 目录、缺 Info.plist 等），与「产物不合格」是两回事。"""


def allow_intel_from_env(env: Mapping[str, str] | None = None) -> bool:
    """读 `ALLOW_INTEL`：非空且不等于 0/false/no 才算显式绕过。"""
    environ = os.environ if env is None else env
    return environ.get(_ALLOW_INTEL_ENV, "").strip().lower() not in _FALSY_ENV_VALUES


def inspect_interpreter(
    *,
    system: str,
    machine: str,
    executable: str,
    require: str = "arm64",
    allow_intel: bool = False,
) -> InterpreterVerdict:
    """纯函数版「打包前断言」：平台事实由参数传入，三种结果才都能被单元测试钉住。"""
    if system != "darwin":
        return InterpreterVerdict(
            ok=True,
            skipped=True,
            message=(
                f"⏭️ 跳过架构断言：构建机不是 macOS（sys.platform={system!r}、arch={machine}）。"
                "本门只管 .app 产物，Linux/Windows 的 CI 不受影响。"
            ),
        )
    if contains((machine,), require):
        return InterpreterVerdict(
            ok=True,
            skipped=False,
            message=f"✅ 构建解释器合格：{executable} → {machine}（要求 {require}）",
        )
    note = "Intel/Rosetta" if machine in _INTEL_MACHINES else machine
    if allow_intel:
        return InterpreterVerdict(
            ok=True,
            skipped=False,
            message=(
                f"⚠️ 已绕过架构断言（{_ALLOW_INTEL_ENV}=1）：解释器是 {machine}（{note}），"
                f"要求是 {require}。\n"
                "这样打出来的包**不能在 Apple 芯片上原生运行**，只能用于本地调试；"
                f"发布产物必须由 {require} 解释器打包，CI 里禁止带 {_ALLOW_INTEL_ENV}。\n"
                f"解释器：{executable}"
            ),
        )
    return InterpreterVerdict(
        ok=False,
        skipped=False,
        message=(
            f"❌ 本项目的解释器是 {machine}（{note}），打出来的包不能在 Apple 芯片上原生运行。\n"
            "请安装 arm64 的 Python 3.12 后：make setup PYTHON=/path/to/arm64/python3.12\n"
            f"（当前解释器：{executable}）\n"
            f"确需打一个只能在 Intel 上跑的调试包（不可发布），才用："
            f"{_ALLOW_INTEL_ENV}=1 make setup"
        ),
    )


def current_interpreter_verdict(
    require: str = "arm64",
    env: Mapping[str, str] | None = None,
) -> InterpreterVerdict:
    """对「现在跑这段代码的解释器」下结论。"""
    return inspect_interpreter(
        system=sys.platform,
        machine=platform.machine(),
        executable=sys.executable,
        require=require,
        allow_intel=allow_intel_from_env(env),
    )


def guard_interpreter_for_build(require: str = "arm64") -> None:
    """`live-caption.spec` 用的构建前硬门：解释器不合格就中止打包，不许静默出 Intel 包。"""
    verdict = current_interpreter_verdict(require)
    print(verdict.message)
    if not verdict.ok:
        raise SystemExit(1)


def _load_plist(stream: BinaryIO) -> Mapping[str, object]:
    try:
        loaded = plistlib.load(stream)
    except Exception as exc:  # plistlib 抛的错误类型很多（损坏二进制 plist、UnicodeError 等）
        raise GateError(f"Info.plist 读不出来：{exc}") from exc
    if not isinstance(loaded, dict):
        raise GateError("Info.plist 不是字典，读不出包信息")
    return loaded


def _read_bundle_info(bundle: Path) -> tuple[Path, str]:
    """从 Info.plist 拿主程序路径与包内版本号。"""
    info_path = bundle / "Contents" / "Info.plist"
    if not info_path.is_file():
        raise GateError(f"{bundle} 下没有 Contents/Info.plist——这不像一个 .app 包目录")
    with info_path.open("rb") as stream:
        plist = _load_plist(stream)
    executable = plist.get("CFBundleExecutable")
    if not isinstance(executable, str) or not executable:
        raise GateError(f"{info_path} 里没有可用的 CFBundleExecutable")
    version = plist.get("CFBundleShortVersionString")
    return (
        bundle / "Contents" / "MacOS" / executable,
        version if isinstance(version, str) else "（Info.plist 未写版本号）",
    )


def library_files(bundle: Path) -> list[Path]:
    """.app 里所有要校验架构的动态库。

    含坏软链：读不出架构就算不合格——门不静默放过任何它没验证过的东西。
    """
    candidates = [
        path
        for path in bundle.rglob("*")
        if path.suffix in LIBRARY_SUFFIXES and (path.is_file() or path.is_symlink())
    ]
    return sorted(candidates, key=lambda path: str(path.relative_to(bundle)))


def _problem(labels: tuple[str, ...], require: str, *, main: bool) -> str:
    """一个文件的判词：主程序要「只有」要求架构，动态库要「包含」要求架构。"""
    if main:
        if not labels:
            return "读不出任何架构切片"
        if not contains(labels, require):
            return f"主程序不含 {require}，实际是 {render(labels)}（这就是误打的包，装上也跑不动）"
        if not only_family(labels, require):
            return f"主程序必须只有 {require}，实际是 {render(labels)}（含别的架构）"
        return ""
    if not contains(labels, require):
        return f"不含 {require}，实际是 {render(labels)}"
    return ""


def _check(path: Path, bundle: Path, require: str, *, main: bool) -> CheckItem:
    rel = str(path.relative_to(bundle))
    try:
        labels = read_arch_labels(path)
    except MachOError as exc:
        return CheckItem(rel=rel, labels=(), problem=f"读不出架构：{exc}")
    return CheckItem(rel=rel, labels=labels, problem=_problem(labels, require, main=main))


def build_report(bundle: Path, require: str = "arm64") -> BundleReport:
    """逐个校验 .app：主程序「只有 arm64」+ 每个 `.so`/`.dylib`「包含 arm64」。"""
    if require not in SUPPORTED_REQUIREMENTS:
        raise ValueError(
            f"不认识的架构要求 {require!r}，只支持：{'、'.join(SUPPORTED_REQUIREMENTS)}"
        )
    if not bundle.is_dir():
        raise GateError(f"{bundle} 不是一个目录（.app 包）")
    main_path, version = _read_bundle_info(bundle)
    if not main_path.is_file():
        raise GateError(f"找不到主程序 {main_path}")
    main_item = _check(main_path, bundle, require, main=True)
    candidates = library_files(bundle)
    if not candidates:
        # 本项目一定要带 libpython 与 PySide6/numpy 的动态库；一个都找不到 = 打包步骤坏了，
        # 不能让「扫了个空目录」被当成通过。
        raise GateError(f"{bundle} 里一个 {'/'.join(LIBRARY_SUFFIXES)} 都没找到，不像打好的包")
    libraries = tuple(_check(path, bundle, require, main=False) for path in candidates)
    return BundleReport(
        bundle=bundle,
        require=require,
        main=main_item,
        libraries=libraries,
        bundle_version=version,
    )


def _format_item(item: CheckItem, *, prefix: str = "  ") -> str:
    line = f"{prefix}{item.rel} : {render(item.labels)}"
    return f"{line} —— {item.problem}" if item.problem else line


def format_report(report: BundleReport) -> str:
    """把结论渲染成发布证据：逐文件一行，人可以直接与 `lipo -archs` 对。"""
    lines = [
        f"=== 产物架构校验：{report.bundle}（要求 {report.require}）===",
        f"包内版本：{report.bundle_version}",
        _format_item(report.main, prefix="主程序（必须只有要求的架构）："),
        f"内嵌动态库（.so / .dylib 全量）共 {len(report.libraries)} 个，"
        f"逐个必须包含 {report.require}：",
    ]
    lines.extend(_format_item(item) for item in report.libraries)
    problems = report.problems
    if problems:
        lines.append(f"—— 不合格 {len(problems)} 项 ——")
        lines.extend(_format_item(item, prefix="  ✗ ") for item in problems)
        lines.append(f"结论：FAIL —— 产物不满足 {report.require}，禁止发布")
    else:
        lines.append(
            f"结论：PASS —— 主程序只有 {report.require}，"
            f"{len(report.libraries)} 个内嵌动态库全部含 {report.require}"
        )
    return "\n".join(lines)


def bundle_command(bundle: Path, require: str, out: TextIO | None = None) -> int:
    """`bundle` 子命令：打印全量校验结果，不合格返回 1。"""
    stream = sys.stdout if out is None else out
    report = build_report(bundle, require)
    print(format_report(report), file=stream)
    return 0 if report.ok else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.release.arch",
        description="构建期架构门（Issue #20）：解释器与打包产物的架构断言",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    interpreter = sub.add_parser(
        "interpreter",
        help="打包前：断言构建解释器架构（非 macOS 自动跳过，ALLOW_INTEL=1 可显式绕过）",
    )
    interpreter.add_argument("--require", default="arm64", choices=SUPPORTED_REQUIREMENTS)

    bundle = sub.add_parser("bundle", help="打包后：校验 .app 产物架构（硬失败门）")
    bundle.add_argument("app", type=Path, help="要校验的 .app 目录，例如 dist/live-caption.app")
    bundle.add_argument("--require", default="arm64", choices=SUPPORTED_REQUIREMENTS)
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    stream = sys.stdout if out is None else out
    args = _build_parser().parse_args(argv)
    if args.command == "interpreter":
        verdict = current_interpreter_verdict(args.require)
        print(verdict.message, file=stream)
        return 0 if verdict.ok else 1
    if args.command == "bundle":
        try:
            return bundle_command(args.app, args.require, stream)
        except GateError as exc:
            print(f"❌ 架构门无法执行：{exc}", file=stream)
            return 1
    raise AssertionError(f"argparse 放行了未知子命令：{args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
