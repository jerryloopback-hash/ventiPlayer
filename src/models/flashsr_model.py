"""FlashSR model wrapper: one-step diffusion audio super-resolution to 48 kHz.

FlashSR (Im & Nam, KAIST) is a distilled one-step version of AudioSR — restores
high-frequency detail and upsamples any input to 48 kHz in a single forward pass.
Versatile across music / speech / SFX, ~22x faster than AudioSR.

Source vendored under src/models/flashsr_src/ (laion redistribution).
License: inference code Apache-2.0; weights inherit AudioSR (MIT).
"""

import logging
import math
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import torch

from src.core.enhancer import Backend, DeviceInfo, MODELS_DIR

logger = logging.getLogger(__name__)

FLASHSR_DIR = MODELS_DIR / "flashsr"
FLASHSR_SR = 48000

# Make the vendored bundle importable (FlashSR.* / TorchJaekwon.*)
_VENDOR = Path(__file__).parent / "flashsr_src"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

# Windowed overlap-add constants (from the bundle's enhance.py)
_WINDOW_LEN = 245_760     # 5.12 s @ 48 kHz — fixed model input length
_OVERLAP = 24_000         # 0.50 s crossfade
_HOP = _WINDOW_LEN - _OVERLAP


class FlashSRModel:
    """High-level FlashSR inference wrapper (per-channel, outputs 48 kHz)."""

    native_sr = FLASHSR_SR  # 流式管线用：模型工作采样率

    def __init__(self, device_info: DeviceInfo, use_fp16: bool = False):
        self._device_info = device_info
        self._model = None
        self._device = self._resolve_device()
        # fp16 autocast: heavy UNet/VAE/vocoder run in half precision (faster +
        # ~half activation VRAM); FFT/STFT ops stay fp32 automatically.
        self._use_fp16 = use_fp16 and self._device.type == "cuda"

    def _resolve_device(self) -> torch.device:
        if self._device_info.backend == Backend.ROCM:
            return torch.device("cuda")
        return torch.device("cpu")

    @staticmethod
    def weights_present() -> bool:
        return all(
            (FLASHSR_DIR / f).exists()
            for f in ("student_ldm.pth", "sr_vocoder.pth", "vae.pth")
        )

    def load(self) -> bool:
        if not self.weights_present():
            logger.error(f"FlashSR weights not found in {FLASHSR_DIR}")
            return False
        try:
            from FlashSR.FlashSR import FlashSR
            self._model = FlashSR(
                student_ldm_ckpt_path=str(FLASHSR_DIR / "student_ldm.pth"),
                sr_vocoder_ckpt_path=str(FLASHSR_DIR / "sr_vocoder.pth"),
                autoencoder_ckpt_path=str(FLASHSR_DIR / "vae.pth"),
            )
            self._model = self._model.to(self._device).eval()
            if self._device.type == "cuda":
                self._warmup()
            logger.info(f"FlashSR loaded on {self._device}")
            return True
        except Exception as e:
            logger.error(f"FlashSR load failed: {e}")
            self._model = None
            return False

    def _warmup(self):
        """One short pass to force MIOpen/HIP kernel JIT compilation."""
        try:
            dummy = torch.zeros(1, _WINDOW_LEN, device=self._device)
            with torch.no_grad(), torch.autocast("cuda", dtype=torch.float16,
                                                 enabled=self._use_fp16):
                self._model(dummy, lowpass_input=False)
            torch.cuda.empty_cache()
            logger.debug("FlashSR warmup complete (fp16=%s)", self._use_fp16)
        except Exception as e:
            logger.warning(f"FlashSR warmup failed (non-fatal): {e}")

    def unload(self):
        if self._model is not None:
            del self._model
            self._model = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    @torch.no_grad()
    def enhance(self, audio: np.ndarray, input_sr: int,
                target_sr: Optional[int] = None,
                progress_callback: Optional[Callable[[float], None]] = None) -> tuple:
        """Super-resolve to 48 kHz. Processes each channel serially (low VRAM).

        Args:
            audio: float32, shape (channels, samples) or (samples,)
            input_sr: source sample rate
            target_sr: ignored (FlashSR always outputs 48 kHz)
            progress_callback: callable(0..1); raise InterruptedError inside to cancel

        Returns:
            (enhanced float32 shape (channels, samples), output_sr=48000)
        """
        if self._model is None:
            raise RuntimeError("FlashSR model not loaded")

        import time as _time
        _t0 = _time.monotonic()

        if audio.ndim == 1:
            audio = audio[np.newaxis, :]

        # Resample each channel to 48 kHz first (FlashSR's working rate)
        if input_sr != FLASHSR_SR:
            audio = self._resample(audio, input_sr, FLASHSR_SR)

        if self._device.type == "cuda":
            self._check_vram()

        # Batch all channels through each window in a single model call
        # (FlashSR forward takes [batch, time]) — stereo costs one call, not two.
        sig = torch.from_numpy(audio).float()  # (nch, T)
        nch, n = sig.shape
        import math as _math
        n_windows = 1 if n <= _WINDOW_LEN else _math.ceil((n - _WINDOW_LEN) / _HOP) + 1
        logger.info("FlashSR inference start: %.1fs audio, %dch, %d window(s), fp16=%s",
                    n / FLASHSR_SR, nch, n_windows, self._use_fp16)

        if n <= _WINDOW_LEN:
            chunk = self._pad_to(sig, _WINDOW_LEN).to(self._device)
            out = self._infer(chunk).cpu()  # (nch, WINDOW_LEN)
            if progress_callback:
                progress_callback(1.0)
            result = out[:, :n].numpy().astype(np.float32)
            logger.info("FlashSR inference done: 1 window in %.1fs",
                        _time.monotonic() - _t0)
            return result, FLASHSR_SR

        fade = self._build_fade(_OVERLAP)
        acc = torch.zeros(nch, n)
        norm = torch.zeros(n)
        offset = 0
        while offset < n:
            if progress_callback:
                progress_callback(min(0.99, offset / n))
            end = min(offset + _WINDOW_LEN, n)
            seg = self._pad_to(sig[:, offset:end], _WINDOW_LEN).to(self._device)
            enhanced = self._infer(seg).cpu()  # (nch, WINDOW_LEN)
            seg_len = min(_WINDOW_LEN, n - offset)
            enhanced = enhanced[:, :seg_len]

            w = torch.ones(seg_len)
            if offset > 0 and seg_len > _OVERLAP:
                w[:_OVERLAP] = fade
            acc[:, offset:offset + seg_len] += enhanced * w
            norm[offset:offset + seg_len] += w
            offset += _HOP

        norm.clamp_(min=1e-8)
        if self._device.type == "cuda":
            torch.cuda.empty_cache()
        if progress_callback:
            progress_callback(1.0)
        result = (acc / norm).numpy().astype(np.float32)
        logger.info("FlashSR inference done: %d windows in %.1fs",
                    n_windows, _time.monotonic() - _t0)
        return result, FLASHSR_SR

    def _infer(self, seg: torch.Tensor) -> torch.Tensor:
        """Run one window (batched over channels) through FlashSR under the
        configured precision. Returns float32 on the model's device."""
        with torch.autocast("cuda", dtype=torch.float16, enabled=self._use_fp16):
            out = self._model(seg, lowpass_input=False)
        return out.float()

    # --- 流式接口（AudioPipeline 边解码边推理用；与批量 enhance() 等价） ---

    def check_vram(self):
        """公开的显存检查入口（CUDA 上可用显存过低时抛 RuntimeError）。"""
        if self._device.type == "cuda":
            self._check_vram()

    def stream_infer(self, win: torch.Tensor) -> torch.Tensor:
        """单窗口推理：(nch, window) CPU 入 → (nch, window) CPU float32 出。"""
        with torch.no_grad():
            return self._infer(win.to(self._device)).cpu()

    def make_stream_ola(self, nch: int):
        """构建流式 overlap-add 处理器（参数与批量 enhance() 的分块一致）。

        FlashSR 批量版接缝只对后窗施加 fade_in（前窗权重 1），故无 fade_out。"""
        from src.models.stream_ola import StreamingOLA
        t = torch.linspace(0.0, math.pi / 2, _OVERLAP)
        return StreamingOLA(
            nch, _WINDOW_LEN, _HOP, _OVERLAP,
            infer_fn=self.stream_infer,
            fade_in_vec=torch.sin(t) ** 2, fade_out_vec=None,
        )

    @staticmethod
    def _build_fade(length: int) -> torch.Tensor:
        t = torch.linspace(0.0, math.pi / 2, length)
        return torch.sin(t) ** 2

    @staticmethod
    def _pad_to(tensor: torch.Tensor, n: int) -> torch.Tensor:
        deficit = n - tensor.shape[-1]
        if deficit <= 0:
            return tensor
        return torch.nn.functional.pad(tensor, (0, deficit))

    @staticmethod
    def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(orig_sr, target_sr)
        up, down = target_sr // g, orig_sr // g
        return np.stack([
            resample_poly(ch, up, down).astype(np.float32) for ch in audio
        ])

    def _check_vram(self):
        free_mem = torch.cuda.mem_get_info(0)[0]
        if free_mem < 400 * 1024 * 1024:
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            free_mem = torch.cuda.mem_get_info(0)[0]
            if free_mem < 250 * 1024 * 1024:
                raise RuntimeError(
                    f"VRAM 不足 ({free_mem // (1024 * 1024)}MB)，无法安全执行 FlashSR 推理"
                )
