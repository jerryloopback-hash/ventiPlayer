"""音频增强单元测试：enhancer 组合链 + 流式 OLA/重采样等价性 + 流式管线。

mock 掉 Apollo/FlashSR 模型，无需真实权重或 GPU。
运行：.venv312/Scripts/python.exe tests/test_audio_enhance.py
"""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np
import torch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

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

    def test_load_errors_recorded_by_name(self):
        """加载失败时按名记录，供 UI 指名道姓地报错。"""
        e = self._enhancer()
        e.set_apollo_enabled(True)
        e.set_flashsr_enabled(True)
        e._load_apollo = lambda: False
        e._load_flashsr = lambda: True
        self.assertFalse(e.load_models())
        self.assertEqual(e.last_load_errors, ["Apollo"])


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


# ─── 流式组件等价性 ──────────────────────────────────────────────────────

def _batch_ola_reference(x: np.ndarray, window: int, hop: int, fade: int,
                         infer, mode: str) -> np.ndarray:
    """批量 enhance() 的 overlap-add 参照实现（复刻 Apollo/FlashSR 循环）。

    mode: "apollo"（互补 fade）/"flashsr"（仅后窗 fade_in）。"""
    nch, n = x.shape
    xt = torch.from_numpy(x)
    result = torch.zeros(nch, n)
    counter = torch.zeros(nch, n)
    fin = torch.linspace(0.0, 1.0, fade)
    fout = 1.0 - fin
    i = 0
    while i < n:
        length = min(window, n - i)
        part = xt[:, i:i + length]
        if length < window:
            part = torch.nn.functional.pad(part, (0, window - length))
        out = infer(part)[:, :length]
        w = torch.ones(length)
        if i > 0 and length > fade:
            w[:fade] = fin
        if mode == "apollo" and (i + hop) < n and length > fade:
            w[length - fade:] = fout
        result[:, i:i + length] += out * w
        counter[:, i:i + length] += w
        i += hop
    counter.clamp_(min=1e-8)
    return (result / counter).numpy()


def _context_infer():
    """带上下文的推理：沿时间做小 FIR，输出依赖窗口内相邻样本，
    能检验接缝权重与 padding 是否与批量版一致（纯点状映射会掩盖权重错误）。"""
    k = torch.tensor([0.2, 0.5, 0.2, 0.1])

    def infer(w: torch.Tensor) -> torch.Tensor:
        padded = torch.nn.functional.pad(w, (len(k) - 1, 0))
        return torch.nn.functional.conv1d(
            padded.unsqueeze(1), k.flip(0).view(1, 1, -1)).squeeze(1)

    return infer


class TestStreamingOLA(unittest.TestCase):
    """StreamingOLA 必须与批量 overlap-add 逐样本一致。"""

    W, H, F = 100, 70, 30  # window = hop + fade

    def _run_stream(self, x, mode, block_size):
        from src.models.stream_ola import StreamingOLA
        fin = torch.linspace(0.0, 1.0, self.F)
        fout = 1.0 - fin if mode == "apollo" else None
        ola = StreamingOLA(x.shape[0], self.W, self.H, self.F,
                           infer_fn=_context_infer(),
                           fade_in_vec=fin, fade_out_vec=fout)
        outs = []
        pos = 0
        while pos < x.shape[1]:
            xb = x[:, pos:pos + block_size]
            last = pos + block_size >= x.shape[1]
            out = ola.process(xb, last=last)
            if out is not None:
                outs.append(out)
            pos += block_size
        # 收尾空块：触发 OLA 处理剩余不足一窗的尾部
        tail = ola.process(np.zeros((x.shape[0], 0), np.float32), last=True)
        if tail is not None:
            outs.append(tail)
        return np.concatenate(outs, axis=1)

    def _check(self, n, mode, block_size):
        rng = np.random.default_rng(42)
        x = rng.standard_normal((2, n)).astype(np.float32) * 0.1
        got = self._run_stream(x, mode, block_size)
        ref = _batch_ola_reference(
            x, self.W, self.H, self.F, _context_infer(), mode)
        self.assertEqual(got.shape[1], n, f"n={n} mode={mode} block={block_size}")
        np.testing.assert_allclose(
            got, ref, atol=1e-5,
            err_msg=f"n={n} mode={mode} block={block_size}")

    def test_apollo_various_lengths(self):
        for n in (10, self.F + 1, self.W - 1, self.W, self.W + 1,
                  2 * self.H, 3 * self.H, 3 * self.H + 1, 5 * self.W):
            for block in (37, 71, 1000):
                self._check(n, "apollo", block)

    def test_flashsr_various_lengths(self):
        for n in (10, self.W, self.W + 1, 2 * self.H, 3 * self.H, 5 * self.W):
            self._check(n, "flashsr", 71)

    def test_exact_multiple_of_hop(self):
        # 输入恰为 hop 整数倍：末窗只覆盖 hop 个真实样本
        for mode in ("apollo", "flashsr"):
            self._check(3 * self.H, mode, 500)
            self._check(4 * self.H, mode, self.H)


class TestStreamingResampler(unittest.TestCase):
    """流式分块重采样必须与整轨 resample_poly 一致（除极边缘样本）。"""

    def test_44100_to_48000(self):
        from scipy.signal import resample_poly
        from src.models.stream_ola import StreamingResampler
        rng = np.random.default_rng(7)
        x = rng.standard_normal((2, 50000)).astype(np.float32) * 0.1
        whole = np.stack([resample_poly(ch, 160, 147) for ch in x])

        rs = StreamingResampler(44100, 48000, 2)
        outs = []
        pos = 0
        block = 4096
        while pos < x.shape[1]:
            xb = x[:, pos:pos + block]
            last = pos + block >= x.shape[1]
            outs.append(rs.process(xb, last=last))
            pos += block
        stream = np.concatenate(outs, axis=1)

        self.assertLess(abs(stream.shape[1] - whole.shape[1]), 3)
        m = min(stream.shape[1], whole.shape[1])
        np.testing.assert_allclose(
            stream[:, 200:m - 200], whole[:, 200:m - 200], atol=1e-4)

    def test_same_rate_passthrough(self):
        from src.models.stream_ola import StreamingResampler
        rs = StreamingResampler(48000, 48000, 2)
        rng = np.random.default_rng(1)
        x = rng.standard_normal((2, 10000)).astype(np.float32)
        out = rs.process(x, last=True)
        np.testing.assert_allclose(out, x, atol=1e-6)


# ─── 流式管线端到端 ──────────────────────────────────────────────────────

class _FakeStreamModel:
    """假模型：native_sr 固定、推理 = 输入 × 2 的线性映射。"""

    def __init__(self, native_sr):
        self.native_sr = native_sr
        self.vram_checked = False

    def check_vram(self):
        self.vram_checked = True

    def make_stream_ola(self, nch):
        from src.models.stream_ola import StreamingOLA
        window, hop, fade = 4000, 3000, 1000
        return StreamingOLA(
            nch, window, hop, fade,
            infer_fn=lambda w: w * 2.0,
            fade_in_vec=torch.linspace(0, 1, fade), fade_out_vec=None,
        )


class TestProgressiveWav(unittest.TestCase):
    """预分配渐进 WAV：头尺寸恒定、未写区域读回零、finalize 修正到实际长度。"""

    def test_presized_header_progressive_write_finalize(self):
        import soundfile as sf
        from src.core.audio_pipe import _ProgressiveWav

        d = tempfile.mkdtemp(prefix="venti_test_")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "out.wav")
        sr, nch = 48000, 2
        estimated = 10000
        w = _ProgressiveWav(path, sr, nch, estimated)
        # 创建后文件即为"完整"尺寸（估计长度），mpv 可安全打开
        self.assertEqual(os.path.getsize(path), 44 + estimated * nch * 4)
        b1 = np.ones((nch, 4000), dtype=np.float32) * 0.5
        b2 = np.ones((nch, 3500), dtype=np.float32) * 0.25
        w.write(b1)
        w.write(b2)
        # 未 finalize 时读文件：已写区域是数据，未写区域是零
        data, _ = sf.read(path, dtype="float32")
        self.assertEqual(data.shape[0], estimated)  # 头声称全尺寸
        np.testing.assert_allclose(data[:4000], b1.T, atol=1e-6)
        np.testing.assert_allclose(data[4000:7500], b2.T, atol=1e-6)
        np.testing.assert_allclose(data[7500:], 0.0, atol=1e-9)
        # finalize：截断到实际长度并修正头
        w.finalize()
        data, sr2 = sf.read(path, dtype="float32")
        self.assertEqual(sr2, sr)
        self.assertEqual(data.shape[0], 7500)
        np.testing.assert_allclose(data[:4000], b1.T, atol=1e-6)
        np.testing.assert_allclose(data[4000:], b2.T, atol=1e-6)

    def test_actual_exceeds_estimate(self):
        import soundfile as sf
        from src.core.audio_pipe import _ProgressiveWav

        d = tempfile.mkdtemp(prefix="venti_test_")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "out.wav")
        w = _ProgressiveWav(path, 44100, 1, 100)
        x = np.ones((1, 500), dtype=np.float32) * 0.3  # 远超预分配
        w.write(x)
        w.finalize()
        data, _ = sf.read(path, dtype="float32")
        self.assertEqual(data.shape[0], 500)
        np.testing.assert_allclose(np.ravel(data), x[0], atol=1e-6)


def _fake_enhancer(models):
    enh = mock.MagicMock()
    enh.gpu_lock = threading.Lock()
    enh.stream_chain.side_effect = lambda: list(models)
    return enh


class TestAudioPipeStreaming(unittest.TestCase):
    """解码 → 增强 → 写 WAV 的流式端到端（mock 解码与模型，真实 soundfile）。"""

    def _make_pipe(self, models):
        from src.core.audio_pipe import AudioPipeline
        pipe = AudioPipeline(_fake_enhancer(models))
        self.addCleanup(pipe.cleanup)
        return pipe

    def test_stereo_roundtrip_writes_2ch_wav(self):
        import soundfile as sf
        from src.core.audio_pipe import PipelineState

        model = _FakeStreamModel(48000)
        pipe = self._make_pipe([model])

        block1 = np.ones((2, 5000), dtype=np.float32) * 0.5
        block2 = np.ones((2, 9000), dtype=np.float32) * 0.5
        pipe._decode_stream = lambda url, headers: (
            48000, 0.0, iter([(block1, False), (block2, True)]))

        statuses = []
        pipe.set_status_callback(lambda s: statuses.append(s))
        pipe._worker("fake://url", None, 0.0, gen=1)

        final = pipe.status
        self.assertEqual(final.state, PipelineState.READY)
        self.assertEqual(final.source_url, "fake://url")
        self.assertEqual(final.output_sr, 48000)
        self.assertTrue(model.vram_checked)
        data, sr = sf.read(final.enhanced_file, dtype="float32")
        self.assertEqual(sr, 48000)
        self.assertEqual(data.ndim, 2)
        self.assertEqual(data.shape[1], 2)   # stereo preserved
        self.assertEqual(data.shape[0], 5000 + 9000)
        # 线性模型（×2）+ 归一化 OLA → 输出恰为输入 ×2
        expected = np.vstack([block1.T, block2.T]) * 2.0
        np.testing.assert_allclose(data, expected, atol=1e-6)

    def test_resample_stage_inserted_for_wrong_sr(self):
        """源采样率 ≠ 模型 native_sr 时应自动插入重采样阶段。"""
        import soundfile as sf
        from src.core.audio_pipe import PipelineState

        model = _FakeStreamModel(48000)
        pipe = self._make_pipe([model])
        n = 10000
        x = np.linspace(-1, 1, n, dtype=np.float32)[np.newaxis, :]
        pipe._decode_stream = lambda url, headers: (
            44100, 0.0, iter([(x, True)]))
        pipe._worker("fake://url", None, 0.0, gen=1)

        final = pipe.status
        self.assertEqual(final.state, PipelineState.READY)
        self.assertEqual(final.output_sr, 48000)
        data, sr = sf.read(final.enhanced_file, dtype="float32")
        self.assertEqual(sr, 48000)
        # 44100→48000 重采样后长度应略增
        self.assertGreater(data.shape[0], n)
        self.assertLess(data.shape[0], int(n * 48000 / 44100) + 16)

    def test_progressive_mode_reports_frontier(self):
        """总时长已知 → 渐进模式：ENHANCING 状态携带文件与递增写入前沿。"""
        import soundfile as sf
        from src.core.audio_pipe import PipelineState

        model = _FakeStreamModel(48000)
        pipe = self._make_pipe([model])
        b1 = np.ones((2, 5000), np.float32) * 0.5
        b2 = np.ones((2, 4000), np.float32) * 0.5
        pipe._decode_stream = lambda url, headers: (
            48000, 30.0, iter([(b1, False), (b2, True)]))

        statuses = []
        pipe.set_status_callback(lambda s: statuses.append(s))
        pipe._worker("fake://url", None, 0.0, gen=1)

        final = pipe.status
        self.assertEqual(final.state, PipelineState.READY)
        enh = [s for s in statuses
               if s.state == PipelineState.ENHANCING and s.enhanced_file]
        self.assertTrue(enh, "渐进模式必须在 ENHANCING 阶段上报增强文件")
        self.assertTrue(all(s.source_url == "fake://url" for s in enh))
        fronts = [s.enhanced_duration_s for s in enh]
        self.assertEqual(fronts, sorted(fronts), "写入前沿必须单调递增")
        self.assertAlmostEqual(enh[0].enhanced_duration_s, 3000 / 48000, places=5)
        # finalize 截断到实际长度（而非预分配长度）
        data, sr = sf.read(final.enhanced_file, dtype="float32")
        self.assertEqual(sr, 48000)
        self.assertEqual(data.shape[0], 9000)

    def test_cancelled_decode_raises_no_ready(self):
        """取消事件置位后 worker 不产出 READY。"""
        from src.core.audio_pipe import PipelineState

        model = _FakeStreamModel(48000)
        pipe = self._make_pipe([model])
        pipe._cancel.set()
        x = np.ones((2, 5000), dtype=np.float32) * 0.5

        def decode(url, headers):
            def gen():
                if pipe._cancel.is_set():
                    raise InterruptedError
                yield x, False
                yield np.zeros((2, 0), np.float32), True
            return 48000, 0.0, gen()

        pipe._decode_stream = decode
        pipe._worker("fake://url", None, 0.0, gen=1)
        self.assertEqual(pipe.status.state, PipelineState.IDLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
