"""量一次真机 SCK 音频帧的实际形状，作为解析代码与测试假件的依据。

用法（需要「屏幕录制」授权，并让屏幕保持亮着；屏幕上锁会让 SCK 报 0 个显示器）：

    .venv/bin/python docs/qa/issue-22/tools/probe_frame_format.py

它开一次真流、用 `say` 放一句英文，然后把**前几帧**的 ASBD 字段、块缓冲字节数、
`CMBlockBufferCopyDataBytes` 的返回码原样打出来。写 Issue #22 的格式判据
（只吃 32 位浮点交错 PCM）之前，先跑这个而不是读代码猜。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time


def main() -> int:
    import CoreMedia  # 主线程先加载，回调线程只做查表（Issue #22 的根因）

    for name in dir(CoreMedia):
        if name.startswith(("CMSampleBuffer", "CMBlockBuffer", "CMAudioFormat")):
            getattr(CoreMedia, name)

    from PySide6.QtWidgets import QApplication

    from app.capture import sck, sck_session

    app = QApplication([])
    records: list[dict[str, object]] = []
    counters = {"frames": 0, "parsed": 0, "dropped": 0}
    real_extract = sck_session.extract_audio_samples

    def probe(sample_buffer: object) -> dict[str, object]:
        fmt = CoreMedia.CMSampleBufferGetFormatDescription(sample_buffer)
        asbd = CoreMedia.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
        data_buf = CoreMedia.CMSampleBufferGetDataBuffer(sample_buffer)
        length = int(CoreMedia.CMBlockBufferGetDataLength(data_buf))
        fields = sck._asbd_fields(asbd)  # noqa: SLF001 - 探针就是要看内部解析结果
        return {
            "buffer_type": type(sample_buffer).__name__,
            "asbd_is_tuple": isinstance(asbd, tuple),
            "asbd_raw": list(asbd) if isinstance(asbd, tuple) else str(asbd),
            "rate_channels_formatid_flags_bits": list(fields) if fields else None,
            "block_bytes": length,
            "num_samples": int(CoreMedia.CMSampleBufferGetNumSamples(sample_buffer)),
        }

    def extract_and_record(sample_buffer: object) -> tuple[object, object] | None:
        counters["frames"] += 1
        if len(records) < 3:
            records.append(probe(sample_buffer))
        parsed = real_extract(sample_buffer)
        counters["parsed" if parsed is not None else "dropped"] += 1
        return parsed

    # 让 delegate 走我们的记录版（生产 delegate 只认 sck_session 里的这个名字）
    sck_session.extract_audio_samples = extract_and_record  # type: ignore[assignment]

    session = sck_session.SCKSession(lambda _block, _level: None)
    session.start()
    subprocess.run(["say", "-v", "Samantha", "Probing the real frame shape, one two three"], check=False)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    session.stop()
    app.processEvents()

    print(json.dumps({"frames": counters["frames"], "parsed": counters["parsed"], "dropped": counters["dropped"]}))
    print(json.dumps(records, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
