"""音频增强重构单元测试：enhancer 组合链 + audio_pipe 立体声 + 可用性探测。

mock 掉 Apollo/FlashSR 模型，无需真实权重或 GPU。
运行：.venv312/Scripts/python.exe test_audio_enhance.py
"""

import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, ".")

from src.core.enhancer import Enhancer, Backend, DeviceInfo


def _fake_model(out_sr, tag, calls):
    """A stand-in model whose enhance() records call order and returns
    (audio, out_sr). It tags the audio so we can assert the chain order."""
    m = mock.MagicMock()

    def enhance(audio, input_sr, target_sr=None, progress_callback=None):
        calls.append((tag, input_sr))
        if progress_callback:
            progress_callback(1.0)
        # mark: prepend a row count change is awkward; just pass through shape
        return audio, out_sr

    m.enhance.side_effect = enhance
    return m


class TestEnhancerChain(unittest.TestCase):

    def _enhancer(self):
        e = Enhancer()
        e._device_info = DeviceInfo(Backend.CPU, "CPU", 0)
        return e

    def test_apollo_only(self):
        e = self._enhancer()
        calls = []
        e._apollo = _fake_model(44100, "apollo", calls)
        e.set_apollo_enabled(True)
        audio = np.zeros((2, 1000), dtype=np.float32)
        out, sr = e.enhance_full(audio, 44100)
        self.assertEqual(sr, 44100)
        self.assertEqual([c[0] for c in calls], ["apollo"])

    def test_flashsr_only(self):
        e = self._enhancer()
        calls = []
        e._flashsr = _fake_model(48000, "flashsr", calls)
        e.set_flashsr_enabled(True)
        audio = np.zeros((2, 1000), dtype=np.float32)
        out, sr = e.enhance_full(audio, 32000)
        self.assertEqual(sr, 48000)
        self.assertEqual([c[0] for c in calls], ["flashsr"])

    def test_both_chained_apollo_then_flashsr(self):
        e = self._enhancer()
        calls = []
        e._apollo = _fake_model(44100, "apollo", calls)
        e._flashsr = _fake_model(48000, "flashsr", calls)
        e.set_apollo_enabled(True)
        e.set_flashsr_enabled(True)
        audio = np.zeros((2, 1000), dtype=np.float32)
        out, sr = e.enhance_full(audio, 44100)
        # Apollo runs first (input 44100), FlashSR second (input = apollo out 44100)
        self.assertEqual([c[0] for c in calls], ["apollo", "flashsr"])
        self.assertEqual(calls[0][1], 44100)
        self.assertEqual(calls[1][1], 44100)
        self.assertEqual(sr, 48000)

    def test_none_enabled_raises(self):
        e = self._enhancer()
        with self.assertRaises(RuntimeError):
            e.enhance_full(np.zeros((2, 100), dtype=np.float32), 44100)

    def test_progress_callback_reaches_one(self):
        e = self._enhancer()
        calls = []
        e._apollo = _fake_model(44100, "apollo", calls)
        e._flashsr = _fake_model(48000, "flashsr", calls)
        e.set_apollo_enabled(True)
        e.set_flashsr_enabled(True)
        seen = []
        e.enhance_full(np.zeros((2, 100), dtype=np.float32), 44100,
                       progress_callback=lambda p: seen.append(p))
        self.assertAlmostEqual(seen[-1], 1.0)

    def test_fp16_change_forces_reload(self):
        """Switching precision on an already-loaded model must reload it."""
        e = self._enhancer()
        e.set_apollo_enabled(True)
        load_count = {"n": 0}

        def fake_load():
            load_count["n"] += 1
            e._apollo = mock.MagicMock()
            e._apollo_loaded_fp16 = e._apollo_fp16
            return True

        e._load_apollo = fake_load
        # first load (fp32)
        e.set_apollo_fp16(False)
        e.load_models()
        self.assertEqual(load_count["n"], 1)
        # same precision → no reload
        e.load_models()
        self.assertEqual(load_count["n"], 1)
        # switch to fp16 → reload
        e.set_apollo_fp16(True)
        e.load_models()
        self.assertEqual(load_count["n"], 2)


class TestAvailability(unittest.TestCase):

    def test_available_false_when_weights_missing(self):
        e = Enhancer()
        e._device_info = DeviceInfo(Backend.CPU, "CPU", 0)
        with mock.patch("pathlib.Path.exists", return_value=False):
            avail = e.available()
        self.assertFalse(avail["apollo"])
        self.assertFalse(avail["flashsr"])

    def test_available_true_when_weights_present(self):
        e = Enhancer()
        e._device_info = DeviceInfo(Backend.CPU, "CPU", 0)
        with mock.patch("pathlib.Path.exists", return_value=True):
            avail = e.available()
        self.assertTrue(avail["apollo"])
        self.assertTrue(avail["flashsr"])


class TestAudioPipeStereo(unittest.TestCase):
    """解码 → 增强 → 写 WAV 的立体声往返（mock enhancer + PyAV，真实 soundfile）。"""

    def test_stereo_roundtrip_writes_2ch_wav(self):
        import soundfile as sf
        from src.core.audio_pipe import AudioPipeline, PipelineState

        # enhancer that returns stereo unchanged at 48k
        enhancer = mock.MagicMock()
        enhancer.enhance_full.side_effect = (
            lambda audio, sr, progress_callback=None: (audio, 48000)
        )

        pipe = AudioPipeline(enhancer)
        # bypass PyAV: feed a known stereo array
        stereo = np.random.randn(2, 24000).astype(np.float32) * 0.1
        pipe._decode_full_audio = mock.MagicMock(return_value=(stereo, 48000))

        statuses = []
        pipe.set_status_callback(lambda s: statuses.append(s))
        # call worker synchronously with matching generation
        pipe._generation = 1
        pipe._worker("fake://url", None, 1)

        final = pipe.status
        self.assertEqual(final.state, PipelineState.READY)
        self.assertIsNotNone(final.enhanced_file)
        data, sr = sf.read(final.enhanced_file, dtype="float32")
        self.assertEqual(sr, 48000)
        self.assertEqual(data.ndim, 2)
        self.assertEqual(data.shape[1], 2)  # stereo preserved
        pipe.cleanup()

    def test_mono_roundtrip_writes_1ch(self):
        import soundfile as sf
        from src.core.audio_pipe import AudioPipeline, PipelineState

        enhancer = mock.MagicMock()
        enhancer.enhance_full.side_effect = (
            lambda audio, sr, progress_callback=None: (audio, 44100)
        )
        pipe = AudioPipeline(enhancer)
        mono = np.random.randn(1, 12000).astype(np.float32) * 0.1
        pipe._decode_full_audio = mock.MagicMock(return_value=(mono, 44100))
        pipe._generation = 1
        pipe._worker("fake://url", None, 1)

        data, sr = sf.read(pipe.status.enhanced_file, dtype="float32")
        self.assertEqual(sr, 44100)
        # soundfile returns 1-D for mono
        self.assertEqual(data.ndim, 1)
        pipe.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
