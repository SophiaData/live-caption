"""判定真 SCK 音频帧的**声道存放方式**（交错 or 分声道连续 planar）。

为什么需要这个探针：ASBD 的 `mFormatFlags` 有两套历史常量表，光看 flags 容易读错位；
而把 planar 当交错解**不会崩也不报错**，只是把每个声道按 2 倍抽取（混叠 + 时长减半），
识别会变差——属于最难查的那类错。所以用「左右声道放不同频率」的音频直接量。

判读规则（每帧 2560 字节 = 640 个 float32，2 声道 320 帧）：
- 交错（L0 R0 L1 R1…）：块缓冲**前半**与**后半**都同时含左右两个频率；
  偶数下标只有左频、奇数下标只有右频。
- planar（LLLL…RRRR）：前半**只有**左频、后半**只有**右频；偶/奇下标各自都混着两个频率。

用法（要真流 + 出声，不进 CI）：

    .venv/bin/python docs/qa/issue-22/tools/probe_channel_layout.py

原始输出：docs/qa/issue-22/logs/channel-layout-measured.txt（2026-11-04 本机 macOS 27.2：
前半只有 1000Hz、后半只有 3000Hz → **planar**，与 flags=41 按 CoreAudio 常量拆位、
与 mBytesPerFrame=4（每声道口径）三者一致）。
"""

from __future__ import annotations

import ctypes
import faulthandler
import json
import logging
import math
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.WARNING, stream=sys.stdout)


def make_test_wav(path: Path, rate: int = 44100, seconds: float = 3.0) -> None:
    """左声道 1000Hz、右声道 3000Hz 的立体声 WAV（两个频率差得足够远，判读不会含糊）。"""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    left = (0.4 * np.sin(2 * math.pi * 1000 * t) * 32767).astype("<i2")
    right = (0.4 * np.sin(2 * math.pi * 3000 * t) * 32767).astype("<i2")
    interleaved = np.empty(n * 2, dtype="<i2")
    interleaved[0::2] = left
    interleaved[1::2] = right
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(interleaved.tobytes())


def main() -> int:
    faulthandler.enable()
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

    import objc  # noqa: F401

    from app.capture import sck, sck_session

    records: list[dict[str, object]] = []
    original = sck.extract_audio_samples

    def peaks(values: np.ndarray) -> list[list[float]]:
        """返回 (频率, 幅度) 前三名；静音帧返回空列表。"""
        if len(values) < 16 or float(np.abs(values).max()) == 0.0:
            return []
        spectrum = np.abs(np.fft.rfft(values * np.hanning(len(values))))
        freqs = np.fft.rfftfreq(len(values), d=1.0 / float(sck.TARGET_SAMPLE_RATE))
        top = np.argsort(spectrum)[::-1][:3]
        return [[int(round(float(freqs[i]))), round(float(spectrum[i]), 1)] for i in top]

    def probe(sample_buffer: object) -> dict[str, object]:
        import CoreMedia as CM  # 已在主线程预热过

        data_buf = CM.CMSampleBufferGetDataBuffer(sample_buffer)
        length = int(CM.CMBlockBufferGetDataLength(data_buf))
        buf = (ctypes.c_char * length)()
        result = CM.CMBlockBufferCopyDataBytes(data_buf, 0, length, buf)
        status = int(result[0] if isinstance(result, tuple) else result)
        floats = np.frombuffer(bytes(buf), dtype=np.float32)
        half = len(floats) // 2
        fmt = CM.CMSampleBufferGetFormatDescription(sample_buffer)
        return {
            "bytes": length,
            "copy_status": status,
            "rms": round(float(np.sqrt((floats**2).mean())), 5),
            "asbd": [
                round(float(x), 2)
                for x in CM.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
            ],
            "media_type": int(CM.CMFormatDescriptionGetMediaType(fmt)),
            "peaks_first_half": peaks(floats[:half]),
            "peaks_second_half": peaks(floats[half:]),
            "peaks_even_index": peaks(floats[0::2]),
            "peaks_odd_index": peaks(floats[1::2]),
        }

    def wrapped(sample_buffer: object) -> object:
        if len(records) < 3:
            try:
                one = probe(sample_buffer)
                if one["rms"] > 0.01:  # 丢静音帧，只留在放音期间的证据
                    records.append(one)
            except Exception as e:  # noqa: BLE001 - 探针自己出错也要留痕
                records.append({"error": f"{type(e).__name__}: {e}"})
        return original(sample_buffer)

    sck_session.extract_audio_samples = wrapped  # type: ignore[assignment]

    wav = Path("/tmp/live-caption-layout.wav")
    make_test_wav(wav)

    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    session = sck_session.SCKSession(lambda _block, _level: None)
    session.start()
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline and len(records) < 3:
        subprocess.run(["afplay", str(wav)], check=False)
        app.processEvents()
    session.stop()
    app.processEvents()

    print(json.dumps(records, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
