"""Issue #22 复现/回归压力脚本：反复「起真流 → 放一句 → 停流」，把首帧的崩溃逼出来。

用法：

    # 修复后应全过
    .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 14

    # 想再看一次「改之前为什么会崩」：--no-prewarm 把开流前的主线程预热变成空操作，
    # 并让本进程第一次触碰 CoreMedia 落回 SCK 的回调线程——首轮就 SIGSEGV（exit 139）。
    .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 2 --no-prewarm

为什么要反复起会话：崩的是**本进程第一次**在 SCK 音频队列线程上碰 CoreMedia 桥的那几帧。
手工验收（`make run` → 点开始捕获 → 放音）5 次里撞上 1 次；这个脚本每轮都新开一条流、
立刻放音，把那条路径反复走（改前 3/3 都在第 1 轮崩，见 logs/stress-baseline-*.log）。

屏幕熄灭时 `SCShareableContent` 会返回 0 个显示器（报「未找到活动的显示器」），
所以真机跑之前先 `caffeinate -di -u -t 900` 包一层。

注：`logs/stress-baseline-*.log` 与 `logs/app-attempts-before-fix.log` 里的栈写的是
`/tmp/stress_sck.py`——那是修复前的同一份脚本，也就是本文件的前身。
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
    p.add_argument(
        "--no-pool",
        action="store_true",
        help="把 delegate 里的 objc.autorelease_pool() 换成空上下文，单测「只靠预热够不够」",
    )
    p.add_argument(
        "--no-prewarm",
        action="store_true",
        help="绕开开流前的主线程预热，重现改之前的崩溃（3 个进程里 2 个会 SIGSEGV，退出码 139）",
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, stream=sys.stdout)
    faulthandler.enable()  # 段错误时把 Python 栈打到 stderr

    from app.capture import sck, sck_session
    from app.capture.sck import extract_audio_samples

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    print(f"[stress] HEAD={head.stdout.strip()} prewarm={not args.no_prewarm}", flush=True)

    stats = {"buffers": 0, "parsed": 0, "dropped": 0}

    def counting_extract(sample_buffer: object) -> tuple[object, object] | None:
        """记账用的薄包装：真解析照跑，只统计第一现场。"""
        stats["buffers"] += 1
        if args.no_prewarm and not sck.coremedia_loaded():
            # 改之前的崩溃现场：本进程**第一次**碰 CoreMedia（加载框架 + 解析函数符号 +
            # 随后第一次值转换）整套发生在 SCK 这条回调线程上。生产代码的两道闸
            # （开流前预热 + 非主线程拒绝现场加载）都已经被 --no-prewarm 绕开。
            sck.load_coremedia()
        parsed = extract_audio_samples(sample_buffer)
        stats["parsed" if parsed is not None else "dropped"] += 1
        return parsed

    # delegate 类是 ObjC 子类，方法体按模块全局名查函数——替换模块全局即可注入
    sck_session.extract_audio_samples = counting_extract  # type: ignore[assignment]
    if args.no_prewarm:
        sck_session.load_coremedia = lambda: {}  # type: ignore[assignment]
        print("[stress] --no-prewarm：开流前不预热，首帧在回调线程首次触碰 CoreMedia", flush=True)

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
        print(
            f"[stress] round {i + 1}/{args.rounds} ok buffers={stats['buffers']} "
            f"parsed={stats['parsed']} dropped={stats['dropped']}",
            flush=True,
        )

    print(f"[stress] 全部 {args.rounds} 轮完成，未崩溃 stats={stats}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
