# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 live-caption 打成 macOS .app。

用法（本地）：.venv/bin/pyinstaller live-caption.spec
产物：dist/live-caption.app

说明：
- 不打包 faster-whisper / ctranslate2（体积大，且用户可选云端识别）；
  本地识别是可选项，缺失时 LocalRecognizer 延迟 import 会失败但不影响主流程。
- macOS 打包只支持在 macOS 上构建（PyInstaller 限制）。
"""

import sys

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
    ["app/main.py"],
    pathex=[],
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
    target_arch=None,
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
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        # 需要屏幕录制权限才能捕获系统声音
        "NSScreenCaptureUsageDescription": "live-caption 需要捕获系统声音来生成实时字幕（只捕获声音，不录屏）。",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": False,
    },
)
