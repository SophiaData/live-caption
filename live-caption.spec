# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 live-caption 打成 macOS .app。

用法（本地）：.venv/bin/pyinstaller live-caption.spec
产物：dist/live-caption.app（发布用的 .dmg / .zip 名字由 CI 带上版本号与架构，Issue #20）

说明：
- 不打包 faster-whisper / ctranslate2（体积大，且用户可选云端识别）；
  本地识别是可选项，缺失时 LocalRecognizer 延迟 import 会失败但不影响主流程。
- macOS 打包只支持在 macOS 上构建（PyInstaller 限制）。
- Issue #20：构建解释器必须是 arm64，否则这里直接中止（不靠 runner 型号碰运气）；
  打完还有构建后架构门 `make verify-arch` 兜底。
"""

import pathlib
import sys
import tomllib

# 构建前架构门（Issue #20）：解释器不是 arm64 就中止打包。
# runner 换成 Intel、或有人在本机手打，都不会再静默产出「装得上、跑不动」的包。
# 确需 Intel 调试包：ALLOW_INTEL=1 pyinstaller live-caption.spec（产物不可发布，会打印「已绕过」）。
_ROOT = pathlib.Path(globals().get("SPECDIR", ".")).resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.release.arch import guard_interpreter_for_build  # noqa: E402

guard_interpreter_for_build()

# 版本号唯一来源：pyproject.toml。包内 Info.plist 与 CI 的产物名都取它，防止互相对不上号。
_VERSION = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["version"]

block_cipher = None

# 隐藏导入：延迟动态 import 的模块需显式声明
hiddenimports = [
    "app.ui.capture_test",
    "app.ui.subtitle_window",
    "app.ui.subtitle_settings",
    "app.ui.tray",
    "app.ui.config_wizard",
    "app.capture.sck_session",
    "app.capture.sck_capture",
    "app.pipeline.recognizer",
    "app.pipeline.translator",
]

a = Analysis(
    ["run_app.py"],
    pathex=[".", "app"],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除可选的本地识别重依赖（不打进包，减小体积）
        "faster_whisper",
        "ctranslate2",
        "torch",
        "transformers",
        # 排除测试相关
        "pytest",
        "pytestqt",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="live-caption",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 无终端窗口（GUI 应用）
    disable_windowed_traceback=False,
    argv_emulation=False,
    # Issue #20：架构显式声明为 arm64（老板已定：只做 Apple Silicon，不做 Intel、不做 universal2）。
    # 之前是 None，意思是「跟着构建机碰运气」；现在 PyInstaller 会把启动器钉成 arm64，
    # 拿不到 arm64 切片就直接报错，不再静默产出 Intel 包。构建后的 `make verify-arch` 再兜一层。
    target_arch="arm64",
    # ad-hoc 签名由 CI 的 `codesign --sign -` 这一步做（Issue #20 第 4 条）；
    # 这里保持 None：项目没有 Apple Developer 身份，不假装签名。
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="live-caption",
)

app = BUNDLE(
    coll,
    name="live-caption.app",
    icon=None,
    bundle_identifier="com.sophiadata.livecaption",
    info_plist={
        "CFBundleName": "live-caption",
        "CFBundleDisplayName": "live-caption 实时字幕",
        "CFBundleShortVersionString": _VERSION,  # 取自 pyproject.toml，与 CI 产物名同源（Issue #20）
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        # 需要屏幕录制权限才能捕获系统声音
        "NSScreenCaptureUsageDescription": "live-caption 需要捕获系统声音来生成实时字幕（只捕获声音，不录屏）。",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": False,
    },
)
