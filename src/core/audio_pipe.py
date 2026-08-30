"""Audio pipeline: decode stream → enhance → write WAV（流式分块版）。

边解码边把音频块送入增强链（Apollo → FlashSR），逐块写入 WAV，内存占用
与音频时长无关（约常数级）。原音频持续播放，直到结果就绪由 SyncManager
切换。进度条直接反映"已升频音频 / 总音频"（解码/加载阶段用文字提示）。

批量增强路径（整轨内存版）仍保留在 Enhancer.enhance_full，供视频导出使用。
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

from src.models.stream_ola import StreamingResampler

logger = logging.getLogger(__name__)

_TEMP_PREFIX = "ventiplayer_"
# 每块约 10 秒（按源采样率换算），兼顾取消响应速度与推理/IO 效率
_BLOCK_S = 10.0


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
    progress: float = 0.0  # 0..1 = 已升频音频/总音频；-1 = 未知总长，UI 显示忙碌
    message: str = ""
    enhanced_file: Optional[str] = None
    enhanced_duration_s: float = 0.0
    output_sr: int = 0  # sample rate of the enhanced output (44100 Apollo / 48000 FlashSR)
    recoverable: bool = False  # True if error is recoverable (can fallback)
    source_url: str = ""  # 本次增强的音频源 URL，供 UI 校验是否仍属当前流


class _ModelStage:
    """模型阶段：把输入块交给模型自身的流式 OLA 处理器。"""

    def __init__(self, model, nch: int):
        self.model = model
        self.out_sr = model.native_sr
        self._ola = model.make_stream_ola(nch)

    def process(self, x: np.ndarray, last: bool) -> Optional[np.ndarray]:
        return self._ola.process(x, last)


class _ResampleStage:
    """重采样阶段：带 carry 的分块 resample_poly（模型工作采样率 ≠ 输入时）。"""

    def __init__(self, in_sr: int, out_sr: int, nch: int):
        self._rs = StreamingResampler(in_sr, out_sr, nch)
        self.out_sr = out_sr

    def process(self, x: np.ndarray, last: bool) -> np.ndarray:
        return self._rs.process(x, last)


class AudioPipeline:
    """Decode → enhance → write WAV, streamed block by block."""

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
            s = self._status
            return PipelineStatus(
                state=s.state, progress=s.progress, message=s.message,
                enhanced_file=s.enhanced_file,
                enhanced_duration_s=s.enhanced_duration_s,
                output_sr=s.output_sr, recoverable=s.recoverable,
                source_url=s.source_url,
            )

    def _update_status(self, **kwargs):
        with self._status_lock:
            for k, v in kwargs.items():
                setattr(self._status, k, v)
        if self._status_callback:
            self._status_callback(self.status)

    def start_enhance(self, audio_url: str, http_headers: dict = None,
                      duration_hint_s: float = 0.0):
        """Start streamed enhancement in a background thread.

        Args:
            duration_hint_s: 源时长（秒）提示，用于进度估算；容器元数据缺失时兜底。
        """
        self.cancel()
        self._cancel.clear()
        self._generation += 1
        gen = self._generation
        self._worker_thread = threading.Thread(
            target=self._worker,
            args=(audio_url, http_headers, float(duration_hint_s or 0.0), gen),
            daemon=True,
        )
        self._worker_thread.start()

    def cancel(self):
        """Cancel ongoing enhancement. 状态静默复位，不触发回调 —— 取消反馈由 UI 层处理。"""
        self._cancel.set()
        if self._worker_thread and self._worker_thread.is_alive():
            # Don't block waiting for worker — it's a daemon thread and
            # may be stuck in a long model inference call
            self._worker_thread.join(timeout=0.5)
        self._worker_thread = None
        with self._status_lock:
            self._status = PipelineStatus()

    def cleanup_old_files(self, keep: str = None):
        """删除临时目录里的旧增强 WAV（mpv 可能占用旧文件，静默跳过）。"""
        keep_name = Path(keep).name if keep else None
        for f in Path(self._temp_dir).glob("enhanced_*.wav"):
            if keep_name and f.name == keep_name:
                continue
            try:
                f.unlink()
            except OSError:
                pass

    def cleanup(self):
        """Clean up temp files."""
        self.cancel()
        import shutil
        try:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        except Exception:
            pass

    # ─── 解码 ───────────────────────────────────────────────────────────

    def _decode_stream(self, audio_url: str, http_headers: dict = None):
        """打开音频容器，流式产出解码块。

        Returns:
            (src_sr, total_s, block_iter)
            block_iter: 迭代产出 (block (nch, T) float32, is_last bool)；
            耗尽即解码结束（最后一个元素 is_last=True，block 可能为空）。
            用户取消时抛 InterruptedError。
        """
        import av
        from av.audio.resampler import AudioResampler

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
        stream = container.streams.audio[0]
        src_sr = int(stream.rate)

        # 容器时长元数据（秒）。防御：极旧版 PyAV 的 duration 是 time_base 单位
        total_s = 0.0
        try:
            d = container.duration
            if d:
                total_s = float(d)
                if total_s > 86400:  # 超过一天 → 大概率是 µs/time_base 单位
                    total_s /= 1e6
        except Exception:
            total_s = 0.0

        resampler = AudioResampler(format="fltp")
        block_n = int(_BLOCK_S * src_sr)
        state = {"nch": 0, "buf": [], "buf_n": 0}

        def _align(arr: np.ndarray) -> np.ndarray:
            if state["nch"] == 0:
                state["nch"] = arr.shape[0]
            nch = state["nch"]
            if arr.shape[0] < nch:
                arr = np.repeat(arr, nch // arr.shape[0], axis=0)[:nch]
            elif arr.shape[0] > nch:
                arr = arr[:nch]
            return arr

        def _append(arr: np.ndarray):
            state["buf"].append(arr)
            state["buf_n"] += arr.shape[-1]

        def _pop_block() -> np.ndarray:
            data = np.concatenate(state["buf"], axis=1)
            x, rest = data[:, :block_n], data[:, block_n:]
            state["buf"] = [rest] if rest.shape[1] else []
            state["buf_n"] = rest.shape[1]
            return x

        def _gen():
            try:
                for packet in container.demux(stream):
                    if self._cancel.is_set():
                        raise InterruptedError("解码已取消")
                    for frame in packet.decode():
                        for rf in resampler.resample(frame):
                            arr = rf.to_ndarray()
                            if arr.ndim == 1:
                                arr = arr[np.newaxis, :]
                            _append(_align(arr.astype(np.float32)))
                            while state["buf_n"] >= block_n:
                                yield _pop_block(), False
                # Flush any frames buffered inside the resampler
                for rf in resampler.resample(None):
                    arr = rf.to_ndarray()
                    if arr.ndim == 1:
                        arr = arr[np.newaxis, :]
                    _append(_align(arr.astype(np.float32)))
                nch = state["nch"]
                tail = (np.concatenate(state["buf"], axis=1) if state["buf"]
                        else np.zeros((nch, 0), dtype=np.float32))
                yield tail, True
            finally:
                container.close()

        return src_sr, total_s, _gen()

    # ─── 工作线程 ───────────────────────────────────────────────────────

    def _worker(self, audio_url: str, http_headers: dict = None,
                duration_hint_s: float = 0.0, gen: int = 0):
        """Worker thread: streamed decode → enhance chain → incremental WAV."""
        try:
            # GPU 操作串行化：与模型加载/导出/其它增强互斥，防止并发 HIP 崩溃
            with self._enhancer.gpu_lock:
                self._run_stream(audio_url, http_headers, duration_hint_s, gen)
        except InterruptedError:
            logger.info("Enhancement cancelled by user")
            self._delete_partial(gen)
        except RuntimeError as e:
            if self._cancel.is_set():
                return
            logger.error(f"Enhancement failed: {e}")
            self._delete_partial(gen)
            # 任何失败都可回退（UI 层据此决定提示或静默回退原音频）
            self._update_status(state=PipelineState.ERROR,
                                message=f"增强失败: {e}",
                                recoverable=True, source_url=audio_url)
        except Exception as e:
            if self._cancel.is_set():
                return
            logger.error(f"Enhancement failed: {e}")
            self._delete_partial(gen)
            self._update_status(state=PipelineState.ERROR,
                                message=f"增强失败: {e}",
                                recoverable=True, source_url=audio_url)

    def _run_stream(self, audio_url: str, http_headers: dict,
                    duration_hint_s: float, gen: int):
        import soundfile as sf

        if self._cancel.is_set():
            raise InterruptedError("增强已取消")

        self._update_status(state=PipelineState.DECODING, progress=-1.0,
                            message="正在解码音频...", source_url=audio_url)

        src_sr, total_s, blocks = self._decode_stream(audio_url, http_headers)

        # 每代结果用独立文件名：避免覆盖 mpv 正在播放的旧增强文件
        out_path = Path(self._temp_dir) / f"enhanced_{gen}.wav"

        models = self._enhancer.stream_chain()
        for m in models:
            m.check_vram()

        stages = None
        writer = None
        out_sr = src_sr
        written = 0  # 已写出的输出样本数
        nch = 0

        def _ensure_stages(channels: int):
            nonlocal stages, out_sr
            chain = []
            in_sr = src_sr
            for m in models:
                if m.native_sr != in_sr:
                    chain.append(_ResampleStage(in_sr, m.native_sr, channels))
                    in_sr = m.native_sr
                chain.append(_ModelStage(m, channels))
            stages = chain
            out_sr = int(chain[-1].out_sr)

        def _flush(x: np.ndarray, last: bool):
            nonlocal writer, written
            for st in stages:
                y = st.process(x, last)
                x = y if y is not None else np.zeros((nch, 0), dtype=np.float32)
            if x.shape[1] == 0:
                return
            if writer is None:
                writer = sf.SoundFile(str(out_path), "w", samplerate=out_sr,
                                      channels=x.shape[0], subtype="FLOAT")
            writer.write(x.T)
            written += x.shape[1]
            self._report_progress(written, out_sr, total_s)

        got_any = False
        for x, is_last in blocks:
            if not got_any:
                if x.shape[1] == 0:
                    continue
                nch = x.shape[0]
                _ensure_stages(nch)
                got_any = True
            if self._cancel.is_set():
                raise InterruptedError("增强已取消")
            _flush(x, last=is_last)

        if not got_any:
            raise RuntimeError("音频解码为空（无有效音频帧）")

        if self._cancel.is_set():
            # 最后一块处理完才取消：丢弃结果，避免取消后仍触发 READY 切换
            raise InterruptedError("增强已取消")

        if writer is not None:
            writer.close()

        enhanced_duration_s = written / out_sr if out_sr else 0.0
        self._update_status(
            state=PipelineState.READY,
            progress=1.0,
            message="增强完成",
            enhanced_file=str(out_path),
            enhanced_duration_s=enhanced_duration_s,
            output_sr=int(out_sr),
            source_url=audio_url,
        )

    def _report_progress(self, written: int, out_sr: int, total_s: float):
        """进度条语义：已升频音频时长 / 总音频时长（用户指定的显示方式）。"""
        done_s = written / out_sr if out_sr else 0.0
        if total_s > 0:
            frac = min(0.99, done_s / total_s)
            msg = (f"音频增强中 {int(frac * 100)}%"
                   f"（已处理 {self._fmt_t(done_s)} / {self._fmt_t(total_s)}）")
        else:
            frac = -1.0  # 总长未知 → UI 忙碌指示
            msg = f"音频增强中（已处理 {self._fmt_t(done_s)}）"
        self._update_status(state=PipelineState.ENHANCING,
                            progress=frac, message=msg)

    @staticmethod
    def _fmt_t(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m:d}:{s:02d}"

    def _delete_partial(self, gen: int):
        """删除失败/取消留下的半个 WAV。"""
        try:
            (Path(self._temp_dir) / f"enhanced_{gen}.wav").unlink(missing_ok=True)
        except OSError:
            pass
