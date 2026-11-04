"""Issue #22 复现/验证脚本：真机走一遍「点开始捕获 → 电脑放音」。

用法（在项目根，用与 make run 同一个解释器）：

    # A. 自己放音：起窗 → 点「开始捕获」→ 用系统 say 放几句英文
    .venv/bin/python docs/qa/issue-22/tools/run_capture.py --sentences 2

    # B. 老板手工验收同款：起窗 → 点按钮 → 由**另一个终端**放音
    .venv/bin/python docs/qa/issue-22/tools/run_capture.py --hold 60
    say -v Samantha "Hello world"

    # C. 3 分钟真放音 soak（含句间静音），并打开「实时字幕」走真实识别管道
    .venv/bin/python docs/qa/issue-22/tools/run_capture.py --soak 180 --subtitles

它做的是和老板手工验收完全一样的三步，只是把「点按钮」和「放音」写进脚本，
好让证据可复现、可贴在 PR 里：

1. 起真实应用窗口（`app.ui.capture_test.CaptureTestWindow`，与 `make run` 同一个窗）；
2. 点真实的「开始捕获」按钮（`QPushButton.click()`，走同一个 `_toggle` 槽）；
3. 用系统 `say` 放英文语音（独立进程，不会被 `excludesCurrentProcessAudio` 排除掉）；
4. 全程事件循环照常转（放音用子进程 + 定时器轮询，不阻塞主线程）；
5. 崩溃时 faulthandler 会把 Python 栈打到 stderr；退出码 139 = SIGSEGV。

`--subtitles` 打开「实时字幕」，走真实识别/翻译管道（需要已配好模型服务）；
默认关闭，只验捕获链本身，不联网、不烧配额。
"""

from __future__ import annotations

import argparse
import faulthandler
import logging
import subprocess
import sys
import time

SENTENCES = [
    "The quick brown fox jumps over the lazy dog.",
    "Live caption turns whatever the computer plays into subtitles.",
    "Screen capture kit delivers real audio sample buffers to the callback.",
    "Silence in between so the segmenter can close a sentence.",
]

GAP_S = 1.2  # 句间静音，给断句器收句的机会


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--sentences", type=int, default=2, help="放几句语音（每句之间约 1.2 秒静音）")
    p.add_argument("--soak", type=float, default=0.0, help="总放音秒数；>0 时循环放音到该秒数")
    p.add_argument("--hold", type=float, default=0.0, help="点完按钮后空转多少秒（外部自己放音）")
    p.add_argument("--subtitles", action="store_true", help="打开「实时字幕」，走真实识别管道")
    p.add_argument("--click-after", type=float, default=1.5, help="窗口出现后多少秒点开始捕获")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.capture.sck import drop_report
    from app.capture.sck_capture import SCKSystemAudioCapture
    from app.ui.capture_test import CaptureTestWindow

    log = logging.getLogger("repro")
    app = QApplication(sys.argv)  # noqa: F841 - 事件循环宿主，需保持引用
    window = CaptureTestWindow()
    window.show()
    window._subtitle_check.setChecked(args.subtitles)  # 默认只验捕获链
    state = {"clicked": False, "started": 0.0, "rounds": 0, "proc": None}

    def click_start() -> None:
        faulthandler.enable()  # 段错误时把 Python 栈打到 stderr
        log.info("点击「开始捕获」")
        window._toggle_btn.click()
        state["clicked"] = True
        state["started"] = time.monotonic()
        log.info("点击返回，按钮文字=%r", window._toggle_btn.text())
        hook_subtitles()

    def hook_subtitles() -> None:
        """把真出路的字幕打日志（不改 UI 行为，只在原回调外再记一份）。"""
        pipeline = getattr(window, "_pipeline", None)
        if pipeline is None:
            return
        prev = pipeline._on_text

        def tap(original: str, translated: str, utt: object) -> None:
            log.info("字幕｜原文=%r 译文=%r", original, translated)
            if prev is not None:
                prev(original, translated, utt)

        pipeline.set_on_text(tap)

    def say_next() -> None:
        """放一句，句间静音，到点收尾。不阻塞事件循环。"""
        proc = state["proc"]
        if proc is not None:
            if proc.poll() is None:
                QTimer.singleShot(100, say_next)
                return
            state["proc"] = None
            QTimer.singleShot(int(GAP_S * 1000), say_next)
            return

        capture = window._sck_capture
        assert isinstance(capture, SCKSystemAudioCapture)
        elapsed = time.monotonic() - state["started"]
        if args.hold > 0:
            if elapsed < args.hold:
                QTimer.singleShot(500, say_next)
                return
            log.info(
                "空转结束（外部放音）：收到音频块 %d 个，运行中=%s，解析跳帧=%s",
                capture.blocks_received,
                capture.running,
                drop_report() or "无",
            )
        elif args.soak > 0:
            if elapsed < args.soak:
                text = SENTENCES[state["rounds"] % len(SENTENCES)]
                log.info("放音 #%d (%.0fs): %s", state["rounds"] + 1, elapsed, text)
                state["proc"] = subprocess.Popen(["say", "-v", "Samantha", text])
                state["rounds"] += 1
                QTimer.singleShot(100, say_next)
                return
            log.info(
                "soak 结束 %.0fs：收到音频块 %d 个，运行中=%s，解析跳帧=%s",
                elapsed,
                capture.blocks_received,
                capture.running,
                drop_report() or "无",
            )
        else:
            if state["rounds"] >= args.sentences:
                log.info(
                    "放音结束：收到音频块 %d 个，运行中=%s",
                    capture.blocks_received,
                    capture.running,
                )
                window._stop_all()
                app.quit()
                return
            text = SENTENCES[state["rounds"] % len(SENTENCES)]
            log.info("放音 #%d: %s", state["rounds"] + 1, text)
            state["proc"] = subprocess.Popen(["say", "-v", "Samantha", text])
            state["rounds"] += 1
            QTimer.singleShot(100, say_next)
            return

        window._stop_all()
        app.quit()

    def arm() -> None:
        if not state["clicked"]:
            QTimer.singleShot(200, arm)
            return
        QTimer.singleShot(800, say_next)

    QTimer.singleShot(int(args.click_after * 1000), click_start)
    QTimer.singleShot(int(args.click_after * 500), arm)
    rc = app.exec()
    log.info("事件循环退出 rc=%s（139=SIGSEGV，段错误）", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
