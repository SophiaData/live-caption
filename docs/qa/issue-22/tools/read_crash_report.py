"""把 macOS 崩溃报告（.ips）读成可读的栈：线程名 + 前 N 帧符号。

用法：

    .venv/bin/python docs/qa/issue-22/tools/read_crash_report.py \
        ~/Library/Logs/DiagnosticReports/python3.12-2026-11-04-125230.ips

老板在 Issue #22 里点名的两条崩溃报告都在这台机器上，这个脚本把它们的第一现场
（异常类型、崩溃线程、栈顶几帧）摊开——判断「崩在 SCK 音频回调线程的 PyObjC
值转换里」就是靠它，不用肉眼拼 JSON。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> tuple[dict[str, object], dict[str, object]]:
    """`.ips` = 一行元数据 + 一段 JSON 正文。"""
    head, body = path.read_text(encoding="utf-8").split("\n", 1)
    return json.loads(head), json.loads(body)


def frames(body: dict[str, object], limit: int) -> list[str]:
    index = int(str(body.get("faultingThread", 0)))
    thread = body["threads"][index]  # type: ignore[index]
    images = body["usedImages"]  # type: ignore[index]
    out = [
        f"崩溃线程 #{index}：name={thread.get('name')!r} queue={thread.get('queue')!r}"  # type: ignore[index]
    ]
    for i, frame in enumerate(thread["frames"][:limit]):  # type: ignore[index]
        image = images[frame["imageIndex"]]
        name = image.get("name") or "?"
        symbol = frame.get("symbol") or "(无符号)"
        line = frame.get("sourceLine")
        tail = f":{line}" if line else ""
        out.append(f"  #{i:<2} {name:<34} +{frame.get('imageOffset', 0):<8} {symbol}{tail}")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("reports", nargs="+", type=Path)
    p.add_argument("--frames", type=int, default=14, help="打印栈顶几帧")
    a = p.parse_args()
    for report in a.reports:
        meta, body = load(report)
        print(f"=== {report.name}（{meta.get('timestamp')}）===")
        print(f"异常：{body.get('exception')}")
        print(f"终止：{body.get('termination')}")
        print("\n".join(frames(body, a.frames)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
