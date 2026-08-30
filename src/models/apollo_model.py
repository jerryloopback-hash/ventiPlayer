"""Apollo model wrapper: single-pass band-split GAN for lossy-codec music restoration.

Apollo (Look2Hear, ICASSP 2025) repairs music degraded by lossy codecs (MP3/AAC),
reconstructing high-frequency content cut by the codec. Operates at 44.1 kHz,
processes stereo natively, single forward pass (no diffusion) — ~19x realtime.

Source vendored under src/models/apollo_src/. License: CC-BY-SA 4.0.
"""

import logging
from typing import Callable, Optional

import numpy as np
import torch

from src.core.enhancer import Backend, DeviceInfo, MODELS_DIR

logger = logging.getLogger(__name__)

APOLLO_DIR = MODELS_DIR / "apollo"
APOLLO_SR = 44100

# Model hyperparams from config_apollo_uni.yaml (universal lossy enhancer)
_MODEL_ARGS = dict(sr=APOLLO_SR, win=20, feature_dim=384, layer=6)

# Chunked overlap-add. Apollo was trained on ~5.4 s segments and its Roformer
# attention is O(T²) over STFT frames, so large chunks explode VRAM (spilling to
# host RAM on ROCm = ~100x slower). Keep chunks small with a short crossfade and
# near-zero redundant overlap.
_CHUNK_S = 8.0           # seconds per model call (was 25 → caused VRAM spill)
_FADE_S = 0.5            # crossfade / overlap length in seconds


class ApolloModel:
    """High-level Apollo inference wrapper (stereo, 44.1 kHz, single pass)."""

    native_sr = APOLLO_SR  # 流式管线用：模型工作采样率

    def __init__(self, device_info: DeviceInfo, use_fp16: bool = False):
        self._device_info = device_info
        self._model = None
        self._device = self._resolve_device()
        # fp16 autocast: runs conv/attention/matmul in half precision (faster +
        # ~half the activation VRAM) while STFT/iSTFT stay fp32 automatically.
        self._use_fp16 = use_fp16 and self._device.type == "cuda"

    def _resolve_device(self) -> torch.device:
        if self._device_info.backend == Backend.ROCM:
            return torch.device("cuda")
        return torch.device("cpu")

    def load(self) -> bool:
        ckpt_path = APOLLO_DIR / "apollo_model_uni.ckpt"
        if not ckpt_path.exists():
            logger.error(f"Apollo checkpoint not found at {ckpt_path}")
            return False
        try:
            from src.models.apollo_src import Apollo
            # Build on CPU first, then move to GPU (avoids ROCm LLVM JIT crash)
            self._model = Apollo.from_pretrain(str(ckpt_path), **_MODEL_ARGS)
            self._model = self._model.to(self._device)
            self._model.eval()
            if self._device.type == "cuda":
                self._warmup()
            logger.info(f"Apollo loaded on {self._device}")
            return True
        except Exception as e:
            logger.error(f"Apollo load failed: {e}")
            self._model = None
            return False

    def _warmup(self):
        """Tiny inference pass to force MIOpen/HIP kernel JIT compilation.

        Prevents 'LLVM ERROR: Can't get available size' on first real call.
        """
        try:
            dummy = torch.zeros(1, 2, APOLLO_SR // 2, device=self._device)
            with torch.no_grad(), torch.autocast("cuda", dtype=torch.float16,
                                                 enabled=self._use_fp16):
                self._model(dummy)
            torch.cuda.empty_cache()
            logger.debug("Apollo warmup complete (fp16=%s)", self._use_fp16)
        except Exception as e:
            logger.warning(f"Apollo warmup failed (non-fatal): {e}")

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
        """Restore lossy-codec damage. Apollo does NOT change sample rate.

        Args:
            audio: float32, shape (channels, samples) or (samples,)
            input_sr: source sample rate
            target_sr: ignored (Apollo works at its native 44.1 kHz)
            progress_callback: callable(0..1); raise InterruptedError inside to cancel

        Returns:
            (enhanced float32 shape (channels, samples), output_sr=44100)
        """
        if self._model is None:
            raise RuntimeError("Apollo model not loaded")

        import time as _time
        _t0 = _time.monotonic()

        # Normalize to (channels, samples)
        if audio.ndim == 1:
            audio = audio[np.newaxis, :]

        # Resample to Apollo's native 44.1 kHz if needed
        if input_sr != APOLLO_SR:
            audio = self._resample(audio, input_sr, APOLLO_SR)

        if self._device.type == "cuda":
            self._check_vram()

        data = torch.from_numpy(audio).float()  # (nch, T)
        nch, n_samples = data.shape
        logger.info("Apollo inference start: %.1fs audio, %dch, fp16=%s",
                    n_samples / APOLLO_SR, nch, self._use_fp16)

        chunk = int(_CHUNK_S * APOLLO_SR)
        fade = int(_FADE_S * APOLLO_SR)
        step = chunk - fade  # advance leaves only `fade` samples of overlap

        result = torch.zeros((nch, n_samples), dtype=torch.float32)
        counter = torch.zeros((nch, n_samples), dtype=torch.float32)
        fade_in = torch.linspace(0.0, 1.0, fade)
        fade_out = torch.linspace(1.0, 0.0, fade)

        i = 0
        idx = 0
        while i < n_samples:
            if progress_callback:
                progress_callback(min(0.99, i / n_samples))

            part = data[:, i:i + chunk]
            length = part.shape[-1]
            if length < chunk:
                part = torch.nn.functional.pad(part, (0, chunk - length))

            out = self._process_chunk(part)[:, :length]  # (nch, length)

            # Crossfade only the leading `fade` region against the previous chunk
            w = torch.ones(length)
            if i > 0 and length > fade:
                w[:fade] = fade_in
            if i + step < n_samples and length > fade:
                w[length - fade:] = fade_out

            result[:, i:i + length] += out * w
            counter[:, i:i + length] += w
            i += step
            idx += 1

        counter.clamp_(min=1e-8)
        final = (result / counter).numpy()
        np.nan_to_num(final, copy=False, nan=0.0)

        if self._device.type == "cuda":
            torch.cuda.empty_cache()
        if progress_callback:
            progress_callback(1.0)
        logger.info("Apollo inference done: %d chunks in %.1fs",
                    idx, _time.monotonic() - _t0)
        return final.astype(np.float32), APOLLO_SR

    def _process_chunk(self, chunk: torch.Tensor) -> torch.Tensor:
        """Run one (nch, samples) chunk through Apollo. Returns (nch, samples).

        Processes all channels in a single batched forward (nch as batch dim)
        rather than per-channel, so stereo costs one GPU call, not two.
        """
        x = chunk.unsqueeze(0).to(self._device)  # (1, nch, T)
        with torch.autocast("cuda", dtype=torch.float16, enabled=self._use_fp16):
            out = self._model(x)                  # (1, nch, T)
        return out.float().squeeze(0).cpu()

    # --- 流式接口（AudioPipeline 边解码边推理用；与批量 enhance() 等价） ---

    def check_vram(self):
        """公开的显存检查入口（CUDA 上可用显存过低时抛 RuntimeError）。"""
        if self._device.type == "cuda":
            self._check_vram()

    def stream_infer(self, win: torch.Tensor) -> torch.Tensor:
        """单窗口推理：(nch, window) CPU 入 → (nch, window) CPU float32 出。"""
        with torch.no_grad():
            return self._process_chunk(win)

    def make_stream_ola(self, nch: int):
        """构建流式 overlap-add 处理器（参数与批量 enhance() 的分块一致）。"""
        from src.models.stream_ola import StreamingOLA
        window, fade = int(_CHUNK_S * APOLLO_SR), int(_FADE_S * APOLLO_SR)
        t = torch.linspace(0.0, 1.0, fade)
        return StreamingOLA(
            nch, window, window - fade, fade,
            infer_fn=self.stream_infer,
            fade_in_vec=t, fade_out_vec=1.0 - t,
        )

    @staticmethod
    def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(orig_sr, target_sr)
        up, down = target_sr // g, orig_sr // g
        # resample each channel
        return np.stack([
            resample_poly(ch, up, down).astype(np.float32) for ch in audio
        ])

    def _check_vram(self):
        free_mem = torch.cuda.mem_get_info(0)[0]
        if free_mem < 300 * 1024 * 1024:
            import gc
            gc.collect()
            torch.cuda.empty_cache()
            free_mem = torch.cuda.mem_get_info(0)[0]
            if free_mem < 200 * 1024 * 1024:
                raise RuntimeError(
                    f"VRAM 不足 ({free_mem // (1024 * 1024)}MB)，无法安全执行 Apollo 推理"
                )
