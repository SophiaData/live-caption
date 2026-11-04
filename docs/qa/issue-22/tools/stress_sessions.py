"""Issue #22 复现/回归压力脚本：反复「起真流 → 放一句 → 停流」，把第一帧的崩溃逼出来。

用法：

    # 修复前这条必崩（首轮第一帧 SIGSEGV，exit 139）；修复后应全过
    .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 14

为什么要反复起会话：崩溃只发生在**本进程第一次**在 SCK 音频队列线程上加载 CoreMedia
桥的那一帧。手工验收（`make run` → 点开始捕获 → 放音）5 次里撞上过 1 次；这个脚本
每轮都新开一条流、立刻放音，把第一帧的路径反复走，稳定复现（修复前 4/4）。

屏幕熄灭时 `SCShareableContent` 会返回 0 个显示器（报「未找到活动的显示器」），
所以真机跑之前先 `caffeinate -di -u -t 900` 包一层。

注：`logs/stress-baseline-*.log`、`logs/app-attempts-before-fix.log` 里的栈写的是
`/tmp/stress_sck.py`——那是修复前的同一份脚本，就是本文件的前身。
"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import subprocess
import sys
import time


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=14, help="起停多少条真流")
    p.add_argument("--sentence", default="Round, one two three, this is real system audio")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)
    faulthandler.enable()  # 段错误时把 Python 栈打到 stderr

    import objc  # noqa: F401  # 与生产一致：PyObjC 运行时可用

    from app.capture import sck_session
    from app.capture.sck import extract_audio_samples

    stats = {"buffers": 0, "parsed": 0, "dropped": 0}

    def counting_extract(sample_buffer: object) -> tuple[object, object] | None:
        """记账用的薄包装：真解析照跑，只统计第一现场。"""
        stats["buffers"] += 1
        parsed = extract_audio_samples(sample_buffer)
        stats["parsed" if parsed is not None else "dropped"] += 1
        return parsed

    # delegate 类是 ObjC 子类，方法体按模块全局名查函数——替换模块全局即可注入
    sck_session.extract_audio_samples = counting_extract  # type: ignore[assignment]

    from PySide6.QtWidgets import QApplication

    app = QApplication([])

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    for i in range(args.rounds):
        session = sck_session.SCKSession(lambda _block, _level: None)
        session.start()
        subprocess.run(["say", "-v", "Samantha", args.sentence], check=False)
        pump(0.6)
        session.stop()
        pump(0.4)
        print(f"[stress] round {i + 1}/{args.rounds} ok buffers={stats['buffers']} "
              f"parsed={stats['parsed']} dropped={stats['dropped']}", flush=True)

    print(f"[stress] 全部 {args.rounds} 轮完成，未崩溃 stats={stats}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
