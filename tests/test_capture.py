"""功能 #2 测试：设备选择与音频缓冲逻辑（纯逻辑层）。"""

import numpy as np
import pytest

from app.capture.buffer import AudioBuffer
from app.capture.devices import find_loopback_devices, missing_device_hint, pick_device

DEVICES = [
    (0, "MacBook Pro麦克风", 1),
    (2, "WeMeet Audio Device", 2),
    (5, "BlackHole 2ch", 2),
    (6, "BlackHole 16ch", 16),
]


class TestDevicePicking:
    def test_finds_loopback_devices(self) -> None:
        found = find_loopback_devices(DEVICES)
        names = [d.name for d in found]
        assert "WeMeet Audio Device" in names
        assert "BlackHole 2ch" in names

    def test_ignores_normal_mic(self) -> None:
        found = find_loopback_devices(DEVICES)
        assert all(d.name != "MacBook Pro麦克风" for d in found)

    def test_picks_shortest_name_first(self) -> None:
        device = pick_device(DEVICES)
        assert device is not None
        assert device.name == "BlackHole 2ch"

    def test_no_device_returns_none(self) -> None:
        assert pick_device([(0, "MacBook Pro麦克风", 1)]) is None

    def test_hint_is_actionable(self) -> None:
        hint = missing_device_hint()
        assert "BlackHole" in hint  # Mac 用户能照着装


class TestAudioBuffer:
    def test_push_pop_roundtrip(self) -> None:
        buf = AudioBuffer(max_blocks=10)
        b1 = np.zeros((16, 1), dtype=np.float32)
        b2 = np.ones((16, 1), dtype=np.float32)
        buf.push(b1)
        buf.push(b2)
        assert buf.blocks_received == 2
        popped = buf.pop_all()
        assert len(popped) == 2
        assert popped[0] is b1 and popped[1] is b2

    def test_pop_all_clears(self) -> None:
        buf = AudioBuffer(max_blocks=10)
        buf.push(np.zeros((16, 1), dtype=np.float32))
        buf.pop_all()
        assert buf.pop_all() == []

    def test_overflow_drops_oldest_and_counts(self) -> None:
        buf = AudioBuffer(max_blocks=3)
        for _ in range(5):
            buf.push(np.zeros((16, 1), dtype=np.float32))
        assert buf.dropped == 2
        assert len(buf.pop_all()) == 3

    def test_invalid_max_raises(self) -> None:
        with pytest.raises(ValueError):
            AudioBuffer(max_blocks=0)
