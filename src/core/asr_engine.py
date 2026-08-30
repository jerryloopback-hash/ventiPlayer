"""Whisper ASR engine using HuggingFace transformers pipeline.

Leverages PyTorch ROCm for GPU acceleration on AMD GPUs.
Outputs segment-level timestamps for subtitle generation.
"""

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import torch

logger = logging.getLogger(__name__)

MODELS_DIR = Path.home() / ".ventiplayer" / "models" / "whisper"

KNOWN_MODELS = {
    "whisper-large-v3": "openai/whisper-large-v3",
    "whisper-medium": "openai/whisper-medium",
    "whisper-small": "openai/whisper-small",
}


def scan_whisper_models() -> list[dict]:
    """Scan for locally cached Whisper models available for use.

    Checks both the HuggingFace cache and the local models directory.
    Returns list of {name, model_id, available} dicts.
    """
    cache_dirs = []

    # Standard HF cache
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        cache_dirs.append(Path(HF_HUB_CACHE))
    except (ImportError, AttributeError):
        cache_dirs.append(Path.home() / ".cache" / "huggingface" / "hub")

    # Alternate cache (used when C: is full)
    alt_cache = MODELS_DIR.parent / "hf_cache"
    if alt_cache.exists():
        cache_dirs.append(alt_cache)

    results = []

    for name, model_id in KNOWN_MODELS.items():
        available = False
        for cache_dir in cache_dirs:
            model_cache_name = "models--" + model_id.replace("/", "--")
            model_path = cache_dir / model_cache_name
            if model_path.exists() and (model_path / "snapshots").exists():
                snapshots = list((model_path / "snapshots").iterdir())
                if snapshots:
                    available = True
                    break
        results.append({"name": name, "model_id": model_id, "available": available})

    if MODELS_DIR.exists():
        for d in MODELS_DIR.iterdir():
            if d.is_dir() and d.name not in KNOWN_MODELS:
                if (d / "config.json").exists():
                    results.append(
                        {"name": d.name, "model_id": str(d), "available": True}
                    )

    return results


def download_whisper_model(model_id: str, progress_callback=None) -> bool:
    """Download a Whisper model from HuggingFace Hub.

    Only downloads files needed for transformers inference (safetensors, config,
    tokenizer). Skips fp32 bins, onnx, flax, tf weights.

    Args:
        model_id: HuggingFace model ID (e.g. "openai/whisper-large-v3")
        progress_callback: Optional callable(message: str) for progress updates

    Returns:
        True on success, False on failure.
    """
    import os
    import shutil

    try:
        from huggingface_hub import snapshot_download

        # Disable xet protocol (incompatible with mirrors)
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        # huggingface_hub 在 import 时就把 HF_ENDPOINT 冻结进 constants.ENDPOINT，
        # start.bat 设的 hf-mirror.com 此刻已经生效，pop 环境变量为时已晚（且该镜像
        # 会破坏 snapshot_download 的元数据请求）。因此显式把官方 endpoint 作为参数
        # 传入，运行时覆盖那个冻结的常量。用户全程开 VPN，官方源可直连。
        os.environ.pop("HF_ENDPOINT", None)
        endpoint = "https://huggingface.co"

        # Determine cache directory: use alternate location if C: is low
        cache_dir = None
        home_drive = Path.home().drive or "C:"
        try:
            free = shutil.disk_usage(home_drive).free
            if free < 5 * 1024**3:
                cache_dir = str(MODELS_DIR.parent / "hf_cache")
                os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            pass

        if progress_callback:
            progress_callback(f"正在下载 {model_id}...")

        kwargs = {
            "repo_id": model_id,
            "repo_type": "model",
            "endpoint": endpoint,
            "ignore_patterns": [
                "*.fp32*",
                "*.onnx*",
                "flax_*",
                "tf_*",
                "onnx/*",
                "*.msgpack",
            ],
        }
        if cache_dir:
            kwargs["cache_dir"] = cache_dir

        snapshot_download(**kwargs)

        if progress_callback:
            progress_callback(f"{model_id} 下载完成")
        return True
    except Exception as e:
        logger.error(f"Failed to download model {model_id}: {e}")
        if progress_callback:
            progress_callback(f"下载失败: {e}")
        return False


class WhisperASR:
    """Whisper ASR engine with timestamp output for subtitle generation."""

    def __init__(self, model_id: str = "openai/whisper-large-v3", device: Optional[str] = None):
        self._model_id = model_id
        self._device = device or self._detect_device()
        self._pipe = None

    @staticmethod
    def _detect_device() -> str:
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"

    def load(self):
        """Load the Whisper pipeline. Call once before transcribe().

        Raises OSError if model is not downloaded locally.
        """
        import os
        from transformers import (
            pipeline,
            AutoModelForSpeechSeq2Seq,
            AutoProcessor,
        )

        # transformers 5.x 的 ASR preprocess 只要检测到 torchcodec 元数据存在，就会
        # 无条件 `import torchcodec`；本机的 torchcodec 缺少 full-shared FFmpeg DLL，
        # 一 import 就抛 libtorchcodec 加载失败，把整条转写链路炸掉。我们始终传入
        # numpy 字典（{"raw": ..., "sampling_rate": ...}），根本走不到 torchcodec 分支，
        # 因此把该模块命名空间里的可用性探测改成 False，让它跳过那次 import。
        from transformers.pipelines import automatic_speech_recognition as _asr_mod
        _asr_mod.is_torchcodec_available = lambda: False

        torch_dtype = torch.float16 if self._device == "cuda" else torch.float32

        # Check alternate cache if it exists
        alt_cache = MODELS_DIR.parent / "hf_cache"
        cache_dir = None
        if alt_cache.exists():
            model_cache_name = "models--" + self._model_id.replace("/", "--")
            if (alt_cache / model_cache_name / "snapshots").exists():
                cache_dir = str(alt_cache)

        logger.info(f"Loading Whisper model: {self._model_id} on {self._device}")
        # 不能把 local_files_only 直接传给 pipeline()：transformers 5.x 会把这个顶层
        # kwarg 一路透传到 model.generate()，触发 "model_kwargs are not used" 报错。
        # 改为先用 from_pretrained 显式离线加载好 model 与 processor，再把现成对象交给
        # pipeline()，这样 local_files_only 只作用于加载阶段，不会污染推理 kwargs。
        from_pretrained_kwargs = {"local_files_only": True}
        if cache_dir:
            from_pretrained_kwargs["cache_dir"] = cache_dir

        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            self._model_id, torch_dtype=torch_dtype, **from_pretrained_kwargs
        )
        model.to(self._device)
        processor = AutoProcessor.from_pretrained(
            self._model_id, **from_pretrained_kwargs
        )

        self._pipe = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            torch_dtype=torch_dtype,
            device=self._device,
        )
        logger.info("Whisper model loaded")

    def unload(self):
        """Release model from memory."""
        self._pipe = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    @property
    def is_loaded(self) -> bool:
        return self._pipe is not None

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str = "zh",
        chunk_length_s: float = 30.0,
    ) -> list[dict]:
        """Transcribe audio and return segments with timestamps.

        Args:
            audio: float32 numpy array, mono
            sample_rate: input sample rate (will be resampled to 16kHz internally)
            language: "zh" for Chinese, "en" for English
            chunk_length_s: chunk length for long-form transcription

        Returns:
            List of {"start": float, "end": float, "text": str} dicts
        """
        if not self._pipe:
            raise RuntimeError("Model not loaded. Call load() first.")

        generate_kwargs = {"language": language, "task": "transcribe"}

        result = self._pipe(
            {"raw": audio, "sampling_rate": sample_rate},
            return_timestamps=True,
            chunk_length_s=chunk_length_s,
            batch_size=8,
            generate_kwargs=generate_kwargs,
        )

        segments = []
        if "chunks" in result:
            for chunk in result["chunks"]:
                ts = chunk.get("timestamp", (None, None))
                start = ts[0] if ts[0] is not None else 0.0
                end = ts[1] if ts[1] is not None else start + 5.0
                text = chunk.get("text", "").strip()
                if text:
                    segments.append({"start": start, "end": end, "text": text})

        return segments
