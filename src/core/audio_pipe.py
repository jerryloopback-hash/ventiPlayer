"""Audio pipeline: decode stream → enhance full track → output.

Runs the enhancer's composable restoration chain (Apollo / FlashSR) on the whole
track offline, preserving stereo, then writes a single WAV that mpv plays once the
SyncManager switches to it. Original audio keeps playing until the result is ready.
"""

import atexit
import logging
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

_TEMP_PREFIX = "ventiplayer_"


def _cleanup_stale_temp_dirs():
    """Remove leftover ventiplayer temp dirs from previous crashed sessions."""
    import shutil
    tmp_root = Path(tempfile.gettempdir())
    for d in tmp_root.glob(f"{_TEMP_PREFIX}*"):
        if d.is_dir():
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


_stale_cleaned = False


class PipelineState(Enum):
    IDLE = "idle"
    DECODING = "decoding"
    ENHANCING = "enhancing"
    READY = "ready"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class PipelineStatus:
    state: PipelineState = PipelineState.IDLE
    progress: float = 0.0
    message: str = ""
    enhanced_file: Optional[str] = None
    enhanced_duration_s: float = 0.0
    output_sr: int = 0  # sample rate of the enhanced output (44100 Apollo / 48000 FlashSR)
    recoverable: bool = False  # True if error is recoverable (can fallback)


class AudioPipeline:
    """Decode → enhance full track → write WAV. Offline (whole-track) only."""

    def __init__(self, enhancer):
        """
        Args:
            enhancer: src.core.enhancer.Enhancer instance
        """
        global _stale_cleaned
        if not _stale_cleaned:
            _stale_cleaned = True
            _cleanup_stale_temp_dirs()

        self._enhancer = enhancer
        self._worker_thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._generation = 0
        self._status = PipelineStatus()
        self._status_lock = threading.Lock()
        self._status_callback: Optional[Callable[[PipelineStatus], None]] = None
        self._temp_dir = tempfile.mkdtemp(prefix=_TEMP_PREFIX)
        atexit.register(self.cleanup)

    def set_status_callback(self, callback: Callable[[PipelineStatus], None]):
        self._status_callback = callback

    @property
    def status(self) -> PipelineStatus:
        with self._status_lock:
            return PipelineStatus(
                state=self._status.state,
                progress=self._status.progress,
                message=self._status.message,
                enhanced_file=self._status.enhanced_file,
                enhanced_duration_s=self._status.enhanced_duration_s,
                output_sr=self._status.output_sr,
                recoverable=self._status.recoverable,
            )

    def _update_status(self, **kwargs):
        with self._status_lock:
            for k, v in kwargs.items():
                setattr(self._status, k, v)
        if self._status_callback:
            self._status_callback(self.status)

    def start_enhance(self, audio_url: str, http_headers: dict = None):
        """Start whole-track enhancement in a background thread."""
        self.cancel()
        self._cancel.clear()
        self._generation += 1
        gen = self._generation
        self._worker_thread = threading.Thread(
            target=self._worker,
            args=(audio_url, http_headers, gen),
            daemon=True,
        )
        self._worker_thread.start()

    def cancel(self):
        """Cancel ongoing enhancement."""
        self._cancel.set()
        if self._worker_thread and self._worker_thread.is_alive():
            # Don't block waiting for worker — it's a daemon thread and
            # may be stuck in a long model inference call
            self._worker_thread.join(timeout=0.5)
        self._worker_thread = None
        self._update_status(state=PipelineState.IDLE, progress=0.0, message="")

    def _decode_full_audio(self, audio_url: str, http_headers: dict = None) -> tuple:
        """Decode full audio stream to numpy array using PyAV, preserving channels.

        Returns:
            (audio_data: np.ndarray float32 shape (channels, samples), sample_rate: int)
        """
        import av
        from av.audio.resampler import AudioResampler

        self._update_status(state=PipelineState.DECODING, progress=0.0,
                           message="正在解码音频流...")

        options = {}
        if http_headers:
            full_headers = dict(http_headers)
            if "User-Agent" not in full_headers:
                full_headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            options["user_agent"] = full_headers["User-Agent"]
            if "Referer" in full_headers:
                options["referer"] = full_headers["Referer"]

        container = av.open(audio_url, options=options)
        audio_stream = container.streams.audio[0]
        sample_rate = audio_stream.rate

        # PyAV >=14 removed the format= kwarg on Frame.to_ndarray(); use a
        # resampler to normalize every frame to planar float32 (channels-first),
        # keeping the source rate and channel layout.
        resampler = AudioResampler(format="fltp")

        frames = []  # each: (channels, samples)
        total_samples = 0

        def _collect(av_frames):
            nonlocal total_samples
            for rf in av_frames:
                arr = rf.to_ndarray()  # (channels, samples) for planar fltp
                if arr.ndim == 1:
                    arr = arr[np.newaxis, :]
                frames.append(arr.astype(np.float32))
                total_samples += arr.shape[-1]

        for packet in container.demux(audio_stream):
            if self._cancel.is_set():
                container.close()
                return None, 0

            for frame in packet.decode():
                _collect(resampler.resample(frame))

        # Flush any frames buffered inside the resampler
        _collect(resampler.resample(None))

        container.close()

        if not frames:
            return None, 0

        # Some frames may have a different channel count on edge files; align to the
        # max channel count by tiling mono up if needed.
        nch = max(f.shape[0] for f in frames)
        aligned = []
        for f in frames:
            if f.shape[0] < nch:
                f = np.repeat(f, nch // f.shape[0], axis=0)[:nch]
            aligned.append(f)
        audio_data = np.concatenate(aligned, axis=1)  # (channels, total_samples)

        self._update_status(progress=0.3,
                            message=f"解码完成 ({total_samples} samples × {nch}ch @ {sample_rate}Hz)")
        return audio_data, sample_rate

    def _worker(self, audio_url: str, http_headers: dict = None, gen: int = 0):
        """Worker thread: decode whole track → enhance chain → write WAV."""
        try:
            audio_data, sample_rate = self._decode_full_audio(audio_url, http_headers)
            if audio_data is None:
                return

            if self._cancel.is_set() or gen != self._generation:
                return

            self._update_status(state=PipelineState.ENHANCING, progress=0.3,
                               message="音频增强中...")

            def progress_cb(p):
                if self._cancel.is_set():
                    raise InterruptedError("Enhancement cancelled")
                overall = 0.3 + p * 0.65
                self._update_status(progress=overall,
                                   message=f"音频增强中... {int(p * 100)}%")

            enhanced, output_sr = self._enhancer.enhance_full(
                audio_data, sample_rate, progress_callback=progress_cb)

            if self._cancel.is_set() or gen != self._generation:
                return

            import soundfile as sf
            # enhanced: (channels, samples) → soundfile wants (samples, channels)
            if enhanced.ndim == 1:
                enhanced = enhanced[np.newaxis, :]
            channels = enhanced.shape[0]
            output_path = Path(self._temp_dir) / "enhanced_quality.wav"
            sf.write(str(output_path), enhanced.T, output_sr, subtype="FLOAT")

            enhanced_duration_s = enhanced.shape[-1] / output_sr
            self._update_status(
                state=PipelineState.READY,
                progress=1.0,
                message="增强完成",
                enhanced_file=str(output_path),
                enhanced_duration_s=enhanced_duration_s,
                output_sr=int(output_sr),
            )

        except InterruptedError:
            logger.info("Enhancement cancelled by user")
            return
        except RuntimeError as e:
            if self._cancel.is_set():
                return
            msg = str(e)
            recoverable = "VRAM" in msg or "out of memory" in msg.lower() or True
            logger.error(f"Enhancement failed: {e}")
            self._update_status(state=PipelineState.ERROR,
                               message=f"增强失败: {e}",
                               recoverable=recoverable)
        except Exception as e:
            if self._cancel.is_set():
                return
            logger.error(f"Enhancement failed: {e}")
            self._update_status(state=PipelineState.ERROR,
                               message=f"增强失败: {e}",
                               recoverable=True)

    def cleanup(self):
        """Clean up temp files."""
        self.cancel()
        import shutil
        try:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        except Exception:
            pass
