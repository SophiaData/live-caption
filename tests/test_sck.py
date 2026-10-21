"""功能 #2b 测试：SCK 纯逻辑层（版本门槛、重采样、声道混合、人话指引）。"""

import numpy as np
import pytest

from app.capture.sck import (
    SCKCaptureError,
    _interleaved_to_mono,
    denied_hint,
    macos_version,
    resample_to_16k,
    unsupported_hint,
)
from app.capture.sck_capture import normalize_sck_frame


class TestVersionGate:
    def test_macos_version_shape(self) -> None:
        major, _minor = macos_version()
        assert major == 0 or major >= 10  # Mac 上合理版本，非 Mac 返回 0

    def test_hints_are_actionable(self) -> None:
        assert "macOS 13" in unsupported_hint() or "macOS" in unsupported_hint()
        assert "屏幕录制" in denied_hint()


class TestAudioProcessing:
    def test_interleaved_stereo_to_mono(self) -> None:
        # 双声道交错：左=1.0 右=-1.0 → 单声道平均=0
        stereo = np.array([1.0, -1.0, 1.0, -1.0], dtype=np.float32)
        mono = _interleaved_to_mono(stereo.tobytes(), 2)
        assert len(mono) == 2
        np.testing.assert_allclose(mono, [0.0, 0.0], atol=1e-6)

    def test_mono_passthrough(self) -> None:
        mono_in = np.array([0.1, 0.2], dtype=np.float32)
        out = _interleaved_to_mono(mono_in.tobytes(), 1)
        np.testing.assert_allclose(out, mono_in, atol=1e-6)

    def test_resample_48k_to_16k(self) -> None:
        # 48kHz 的 4800 样本 = 0.1s → 16k 应约 1600 样本
        samples = np.ones(4800, dtype=np.float32)
        out = resample_to_16k(samples, 48000)
        assert abs(len(out) - 1600) <= 2
        np.testing.assert_allclose(out, 1.0, atol=1e-6)

    def test_resample_passthrough_when_16k(self) -> None:
        samples = np.array([0.5, 0.5], dtype=np.float32)
        out = resample_to_16k(samples, 16000)
        # 同采样率不重采样；值与输入一致（可能拷贝，不保证同一对象）
        np.testing.assert_array_equal(out, samples)

    def test_resample_empty(self) -> None:
        assert len(resample_to_16k(np.array([], dtype=np.float32), 48000)) == 0


class TestNormalize:
    def test_none_in_none_out(self) -> None:
        assert normalize_sck_frame(None) is None
        assert normalize_sck_frame((np.array([], dtype=np.float32), 48000)) is None

    def test_normal_frame(self) -> None:
        block, level = normalize_sck_frame((np.ones(4800, dtype=np.float32), 48000))
        assert abs(len(block) - 1600) <= 2
        assert level == pytest.approx(1.0)


class TestCaptureGate:
    def test_start_on_unsupported_raises_with_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模拟 macOS<13：start 必须给人话错误而非崩溃，且绝不触碰 PyObjC。"""
        from app.capture import sck_capture
        from app.capture.sck_capture import SCKSystemAudioCapture

        monkeypatch.setattr(sck_capture, "macos_version", lambda: (12, 0))
        cap = SCKSystemAudioCapture()
        with pytest.raises(SCKCaptureError) as ei:
            cap.start()
        assert "macOS 13" in str(ei.value)  # 提示里含系统版本要求（人话）

    def test_start_double_raises(self) -> None:
        from app.capture.sck_capture import SCKSystemAudioCapture

        cap = SCKSystemAudioCapture()
        cap._session = object()  # 模拟已在捕获
        with pytest.raises(SCKCaptureError, match="已在"):
            cap.start()
