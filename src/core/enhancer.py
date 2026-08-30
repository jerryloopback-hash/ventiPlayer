"""AI audio enhancement engine abstraction layer.

Manages device selection (ROCm/DirectML/CPU) and runs a composable restoration
chain. Two independent backends, each toggleable; when both are on they run in
series (Apollo → FlashSR):

  - Apollo  (RESTORE):  repairs lossy-codec damage at 44.1 kHz, single pass.
  - FlashSR (SUPERRES): one-step super-resolution to 48 kHz.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

MODELS_DIR = Path.home() / ".ventiplayer" / "models"


class Backend(Enum):
    ROCM = "rocm"
    DIRECTML = "directml"
    CPU = "cpu"


@dataclass
class DeviceInfo:
    backend: Backend
    device_name: str
    vram_mb: int = 0


def detect_device() -> DeviceInfo:
    """Detect best available compute device for inference."""
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_memory // (1024 * 1024)
            logger.info(f"ROCm/CUDA device found: {name} ({vram} MB)")
            return DeviceInfo(Backend.ROCM, name, vram)
    except ImportError:
        logger.info("PyTorch not installed, trying DirectML")
    except Exception as e:
        logger.warning(f"PyTorch device detection failed: {e}")

    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        if "DmlExecutionProvider" in providers:
            logger.info("DirectML available via ONNX Runtime")
            return DeviceInfo(Backend.DIRECTML, "DirectML GPU", 0)
    except ImportError:
        logger.info("onnxruntime-directml not installed")
    except Exception as e:
        logger.warning(f"DirectML detection failed: {e}")

    logger.info("Falling back to CPU")
    return DeviceInfo(Backend.CPU, "CPU", 0)


class Enhancer:
    """Composable audio restoration: Apollo (codec repair) + FlashSR (super-res).

    Each backend is an independent toggle. enhance_full() runs the enabled
    backends in series and returns the result plus its output sample rate.
    """

    def __init__(self):
        self._device_info: Optional[DeviceInfo] = None
        self._apollo = None
        self._flashsr = None
        self._apollo_enabled = False
        self._flashsr_enabled = False
        self._apollo_fp16 = False
        self._flashsr_fp16 = False
        self._apollo_loaded_fp16 = None   # precision the loaded model was built with
        self._flashsr_loaded_fp16 = None

    @property
    def device_info(self) -> DeviceInfo:
        if self._device_info is None:
            self._device_info = detect_device()
        return self._device_info

    @property
    def backend(self) -> Backend:
        return self.device_info.backend

    def set_apollo_enabled(self, enabled: bool):
        self._apollo_enabled = enabled

    def set_flashsr_enabled(self, enabled: bool):
        self._flashsr_enabled = enabled

    def set_apollo_fp16(self, fp16: bool):
        self._apollo_fp16 = fp16

    def set_flashsr_fp16(self, fp16: bool):
        self._flashsr_fp16 = fp16

    @property
    def any_enabled(self) -> bool:
        return self._apollo_enabled or self._flashsr_enabled

    def load_models(self) -> bool:
        """Load the enabled backends, unload the disabled ones. Reload a backend
        if its precision (fp16/fp32) changed. True if all enabled models loaded
        successfully (and at least one is enabled)."""
        if not self.any_enabled:
            return False

        ok = True
        # Apollo — reload if precision changed
        if self._apollo_enabled:
            if self._apollo is not None and self._apollo_loaded_fp16 != self._apollo_fp16:
                self._apollo.unload()
                self._apollo = None
            if self._apollo is None:
                ok = self._load_apollo() and ok
        elif self._apollo is not None:
            self._apollo.unload()
            self._apollo = None

        # FlashSR — reload if precision changed
        if self._flashsr_enabled:
            if self._flashsr is not None and self._flashsr_loaded_fp16 != self._flashsr_fp16:
                self._flashsr.unload()
                self._flashsr = None
            if self._flashsr is None:
                ok = self._load_flashsr() and ok
        elif self._flashsr is not None:
            self._flashsr.unload()
            self._flashsr = None

        return ok

    def _load_apollo(self) -> bool:
        from src.models.apollo_model import ApolloModel
        self._apollo = ApolloModel(self.device_info, use_fp16=self._apollo_fp16)
        if not self._apollo.load():
            self._apollo = None
            return False
        self._apollo_loaded_fp16 = self._apollo_fp16
        logger.info("Apollo model loaded")
        return True

    def _load_flashsr(self) -> bool:
        from src.models.flashsr_model import FlashSRModel
        self._flashsr = FlashSRModel(self.device_info, use_fp16=self._flashsr_fp16)
        if not self._flashsr.load():
            self._flashsr = None
            return False
        self._flashsr_loaded_fp16 = self._flashsr_fp16
        logger.info("FlashSR model loaded")
        return True

    def enhance_full(self, audio: np.ndarray, input_sr: int,
                     progress_callback: Optional[Callable[[float], None]] = None) -> tuple:
        """Run the enabled restoration chain on full audio.

        Args:
            audio: float32, shape (channels, samples) or (samples,)
            input_sr: source sample rate
            progress_callback: callable(0..1); raise InterruptedError to cancel

        Returns:
            (enhanced float32 shape (channels, samples), output_sr)
        """
        stages = []
        if self._apollo_enabled and self._apollo is not None:
            stages.append(self._apollo)
        if self._flashsr_enabled and self._flashsr is not None:
            stages.append(self._flashsr)

        if not stages:
            raise RuntimeError("没有可用的增强模型（Apollo/FlashSR 均未加载）")

        n = len(stages)
        cur = audio
        cur_sr = input_sr
        for idx, model in enumerate(stages):
            def stage_progress(p, _i=idx):
                if progress_callback:
                    progress_callback((_i + p) / n)
            cur, cur_sr = model.enhance(cur, cur_sr, progress_callback=stage_progress)

        if progress_callback:
            progress_callback(1.0)
        return cur, cur_sr

    def unload(self):
        if self._apollo is not None:
            self._apollo.unload()
            self._apollo = None
        if self._flashsr is not None:
            self._flashsr.unload()
            self._flashsr = None
        self._apollo_loaded_fp16 = None
        self._flashsr_loaded_fp16 = None

    def available(self) -> dict:
        """Which backends have their weights downloaded. For GUI gating."""
        apollo_ckpt = MODELS_DIR / "apollo" / "apollo_model_uni.ckpt"
        flashsr_ok = all(
            (MODELS_DIR / "flashsr" / f).exists()
            for f in ("student_ldm.pth", "sr_vocoder.pth", "vae.pth")
        )
        return {"apollo": apollo_ckpt.exists(), "flashsr": flashsr_ok}
