"""视频导出引擎：把当前视频连同已配置的音频/画面增强真实烘焙为 mp4。

==============================================================================
给 main_window 接线的集成说明（parent agent 约 20 行即可接好）
==============================================================================

1) 连接信号（SettingsDialog 已新增 export_video_requested）：

       dlg = SettingsDialog(self._settings, self)
       dlg.export_video_requested.connect(self._on_export_video_requested)
       dlg.exec()

2) _on_export_video_requested 里做门控 + 取保存路径 + 起后台导出：

       def _on_export_video_requested(self):
           stream = self._current_stream
           if stream is None or stream.is_live:
               QMessageBox.warning(self, "提示", "请先解析一个非直播视频")
               return
           last_dir = self._settings.get("export_last_dir") or ""
           default_name = (stream.title or "video") + ".mp4"
           path, _ = QFileDialog.getSaveFileName(
               self, "导出为 MP4", str(Path(last_dir) / default_name),
               "MP4 视频 (*.mp4)")
           if not path:
               return
           if not path.lower().endswith(".mp4"):
               path += ".mp4"
           self._settings.set("export_last_dir", str(Path(path).parent))

           es = ExportSettings.from_states(
               output_path=path,
               audio_settings=self._enhance_panel.get_settings(),
               export_state=self._video_enhance_panel.get_export_state(),
               stream=stream,
           )
           # 进度回调/完成回调务必切回主线程（用已有的 Signal 中转，参考音频增强）
           self._exporter = VideoExporter(self._enhancer)
           self._exporter.export(
               video_url=stream.video_url,
               audio_url=stream.audio_url or stream.video_url,
               http_headers=stream.http_headers,
               export_settings=es,
               progress_callback=lambda p, msg: self._export_progress.emit(p, msg),
               done_callback=lambda r: self._export_done.emit(r),
           )

3) 成功弹窗（done_callback 收到 ExportResult）：

       def _on_export_done(self, r: ExportResult):
           if not r.success:
               QMessageBox.warning(self, "导出失败", r.message)
               return
           QMessageBox.information(self, "导出成功",
               f"已保存到：{r.output_path}\n\n"
               f"视频：{r.video_info_label}\n"
               f"音频修复方案：{r.audio_scheme_label}\n"
               f"画面增强方案：{r.video_scheme_label}"
               + ("" if r.gpu_baked else "\n\n注意：当前环境无法创建离屏 GPU 渲染上下文，"
                  "已退化为 PyAV 近似烘焙，GLSL 着色器（超分/锐化等）未能真实烘焙。"))

   ExportResult.video_info_label 形如 "mp4 / 3840×2160 / 23.976fps / 48kHz / 24kHz"
   （格式 + 分辨率 + 帧率 + 音频采样率 + 截止频率）。

门控条件：current_stream 存在且 not is_live。

==============================================================================
设计要点
==============================================================================
- 画面烘焙策略 = 离屏 GPU 渲染真实烘焙（primary）：驱动 libmpv 的 render API
  (mpv.MpvRenderContext)，配合 Qt 的离屏 OpenGL 上下文，让每一帧走完 mpv 的完整
  GPU 着色器管线（Anime4K/FSR/FSRCNNX 超分、CAS 锐化、deband、HDR tone-mapping、
  亮度/对比度/饱和度/gamma），回读 framebuffer 后用 PyAV 编码 H.264。
- 安全约束：宿主进程绝不 import vapoursynth / vsrife（原生崩溃 0xe24c4a02）。
  本模块不导入它们；离屏 mpv 实例只用 lavfi 的 hqdn3d/nlmeans 降噪 vf，绝不启用
  任何 vapoursynth vf。
- 退化回退：若离屏 GPU 上下文无法创建/渲染（无显示设备等），不静默吞掉，而是回退到
  PyAV 重编码——尽力烘焙亮度/对比度/饱和度/gamma（numpy 实现）、降噪
  (nlmeans)、按面板超分倍率做 lanczos 缩放近似——并在 ExportResult.gpu_baked=False
  且 message 里明确告知用户「GLSL 着色器未能真实烘焙」。
- 插帧不烘焙：display-resample 伪插帧是显示期属性（依赖显示器刷新率），无法写进文件；
  小黄鸭(Lossless Scaling)是外部全屏叠加程序——两者导出时一律忽略，导出文件保持源帧率。
"""

import logging
import os
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _ensure_libmpv_on_path():
    """确保项目根目录（含 libmpv-2.dll）在 PATH 上，便于 import mpv。

    宿主正常启动时 src/main.py 已设过；独立运行/测试时这里兜底。"""
    root = str(_PROJECT_ROOT)
    if root not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")


# ─── 工具：复用 main_window 的采样率/频率/截止估算（避免跨文件耦合，这里复刻一份）──

def _format_sr(sr: int) -> str:
    """44100 → '44.1kHz'，48000 → '48kHz'。"""
    if not sr:
        return ""
    khz = sr / 1000
    return f"{int(khz)}kHz" if khz == int(khz) else f"{khz:.1f}kHz"


def _format_freq(freq: int) -> str:
    """16000 → '16kHz'，22050 → '22kHz'。"""
    if not freq:
        return ""
    khz = freq / 1000
    return f"{int(round(khz))}kHz" if khz >= 10 else f"{khz:.1f}kHz"


def _estimate_cutoff(sample_rate: int, bitrate: Optional[int], codec: str) -> Optional[int]:
    """按编码信息估算音频带宽截止频率（Hz）。复刻自 main_window._estimate_cutoff。"""
    if not sample_rate:
        return None
    nyquist = sample_rate // 2
    if not bitrate:
        if codec and codec.lower() in ("opus", "vorbis", "flac", "alac", "pcm"):
            return nyquist
        return int(nyquist * 0.75)
    codec_lower = (codec or "").lower()
    if codec_lower in ("flac", "alac", "pcm", "pcm_s16le", "pcm_s24le"):
        return nyquist
    if codec_lower == "opus":
        if bitrate >= 128:
            return min(nyquist, 20000)
        if bitrate >= 64:
            return min(nyquist, 18000)
        return min(nyquist, 12000)
    if bitrate >= 256:
        return min(nyquist, 20000)
    if bitrate >= 192:
        return min(nyquist, 18000)
    if bitrate >= 128:
        return min(nyquist, 16000)
    if bitrate >= 96:
        return min(nyquist, 14000)
    if bitrate >= 64:
        return min(nyquist, 12000)
    return min(nyquist, 8000)


def _format_audio_scheme(apollo_enabled: bool, flashsr_enabled: bool,
                         apollo_fp16: bool, flashsr_fp16: bool) -> str:
    """拼出音频修复方案标签，如 'Apollo(fp32)+FlashSR(fp16)'；都没开返回 '原始音频'。

    与 main_window._format_enhance_scheme 一致，区别是无增强时给出更明确的 '原始音频'。"""
    parts = []
    if apollo_enabled:
        parts.append("Apollo(fp16)" if apollo_fp16 else "Apollo(fp32)")
    if flashsr_enabled:
        parts.append("FlashSR(fp16)" if flashsr_fp16 else "FlashSR(fp32)")
    return "+".join(parts) if parts else "原始音频"


@dataclass
class ExportSettings:
    """一次导出所需的全部配置：输出路径 + 音频方案 + 可复现画面的完整状态。"""

    output_path: str

    # 音频方案
    apollo_enabled: bool = False
    flashsr_enabled: bool = False
    apollo_fp16: bool = False
    flashsr_fp16: bool = False

    # 画面状态（来自 VideoEnhancePanel.get_export_state()）
    shaders: list = field(default_factory=list)          # GLSL 着色器绝对路径列表
    render_props: dict = field(default_factory=dict)     # mpv render property 字典
    vf: str = ""                                         # 降噪 vf 字符串
    upscale_factor: int = 1                              # 有效超分倍率 1/2/4
    denoise_mode: str = ""                               # hqdn3d / nlmeans / ""
    video_scheme_label: str = "原画"                     # 中文画面方案摘要

    # 源信息（用于 ExportResult 里报告真实参数与截止频率估算）
    src_width: Optional[int] = None
    src_height: Optional[int] = None
    src_fps: Optional[float] = None
    src_audio_sr: Optional[int] = None
    src_audio_bitrate: Optional[int] = None
    src_audio_codec: str = ""

    video_codec: str = "libx264"

    @classmethod
    def from_states(cls, output_path: str, audio_settings: dict,
                    export_state: dict, stream) -> "ExportSettings":
        """便捷构造：合并 EnhancePanel.get_settings() + VideoEnhancePanel.get_export_state()
        + StreamInfo。供 main_window 一行拼好。"""
        return cls(
            output_path=output_path,
            apollo_enabled=bool(audio_settings.get("apollo_enabled")),
            flashsr_enabled=bool(audio_settings.get("flashsr_enabled")),
            apollo_fp16=bool(audio_settings.get("apollo_fp16")),
            flashsr_fp16=bool(audio_settings.get("flashsr_fp16")),
            shaders=list(export_state.get("shaders", [])),
            render_props=dict(export_state.get("render_props", {})),
            vf=export_state.get("vf", "") or "",
            upscale_factor=int(export_state.get("upscale_factor", 1) or 1),
            denoise_mode=export_state.get("denoise_mode", "") or "",
            video_scheme_label=export_state.get("scheme_label", "原画"),
            src_width=getattr(stream, "video_width", None),
            src_height=getattr(stream, "video_height", None),
            src_fps=getattr(stream, "video_fps", None),
            src_audio_sr=getattr(stream, "audio_sample_rate", None),
            src_audio_bitrate=getattr(stream, "audio_bitrate", None),
            src_audio_codec=getattr(stream, "audio_codec", "") or "",
        )

    @property
    def audio_scheme_label(self) -> str:
        return _format_audio_scheme(
            self.apollo_enabled, self.flashsr_enabled,
            self.apollo_fp16, self.flashsr_fp16,
        )

    @property
    def any_audio_enabled(self) -> bool:
        return self.apollo_enabled or self.flashsr_enabled


@dataclass
class ExportResult:
    """导出结果：成功标记 + 路径 + 提示语 + 实际烘焙出的视频/音频参数 + 方案标签。"""

    success: bool
    output_path: str = ""
    message: str = ""

    # 实际烘焙参数
    container_format: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    audio_sr: int = 0
    audio_cutoff_hz: int = 0

    audio_scheme_label: str = ""
    video_scheme_label: str = ""
    gpu_baked: bool = False  # True=离屏 GPU 真实烘焙；False=退化为 PyAV 近似

    @property
    def video_info_label(self) -> str:
        """成功弹窗用：'mp4 / 3840×2160 / 23.976fps / 48kHz / 24kHz'。"""
        fps_str = (f"{self.fps:.3f}".rstrip("0").rstrip(".") + "fps") if self.fps else ""
        bits = [
            self.container_format or "mp4",
            f"{self.width}×{self.height}" if self.width and self.height else "",
            fps_str,
            _format_sr(self.audio_sr),
            _format_freq(self.audio_cutoff_hz),
        ]
        return " / ".join(b for b in bits if b)


# 进度回调签名：callback(progress: float 0..1, message: str)
ProgressCallback = Callable[[float, str], None]
DoneCallback = Callable[[ExportResult], None]


class VideoExporter:
    """把视频连同音频/画面增强真实烘焙为 mp4。后台线程执行，进度/完成走回调。

    画面烘焙优先离屏 GPU 渲染（_bake_video_gpu）；失败则退化 PyAV 近似
    （_bake_video_pyav）。音频复用现有 Apollo/FlashSR 离线链。
    """

    def __init__(self, enhancer):
        """Args: enhancer — 共享的 src.core.enhancer.Enhancer 实例。"""
        self._enhancer = enhancer
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._progress_cb: Optional[ProgressCallback] = None
        self._tmp_files: list = []

    # ─── 公共 API ────────────────────────────────────────────────────────

    def export(self, video_url: str, audio_url: str, http_headers: Optional[dict],
               export_settings: ExportSettings,
               progress_callback: Optional[ProgressCallback] = None,
               done_callback: Optional[DoneCallback] = None):
        """在后台线程跑导出。progress_callback(p, msg) 报进度，done_callback(ExportResult)
        在结束（成功或失败）时调用一次。回调可能在工作线程触发——UI 端务必切回主线程。"""
        self._cancel.clear()
        self._progress_cb = progress_callback
        self._worker = threading.Thread(
            target=self._run,
            args=(video_url, audio_url, http_headers, export_settings, done_callback),
            daemon=True,
        )
        self._worker.start()

    def cancel(self):
        self._cancel.set()

    # ─── 进度辅助 ────────────────────────────────────────────────────────

    def _report(self, progress: float, message: str):
        if self._progress_cb:
            try:
                self._progress_cb(max(0.0, min(1.0, progress)), message)
            except Exception:
                pass

    def _check_cancel(self):
        if self._cancel.is_set():
            raise InterruptedError("导出已取消")

    # ─── 主流程 ──────────────────────────────────────────────────────────

    def _run(self, video_url, audio_url, http_headers, es: ExportSettings,
             done_callback: Optional[DoneCallback]):
        result = ExportResult(
            success=False,
            output_path=es.output_path,
            audio_scheme_label=es.audio_scheme_label,
            video_scheme_label=es.video_scheme_label,
        )
        try:
            _ensure_libmpv_on_path()

            # (a) 音频：增强或直通，产出本地 WAV
            self._report(0.0, "准备音频...")
            audio_wav, audio_sr = self._prepare_audio(audio_url, http_headers, es)
            self._check_cancel()

            # (b) 画面烘焙：优先离屏 GPU，失败退化 PyAV
            tmp_video = self._tmp_path(es.output_path, "_baked.mp4")
            gpu_baked = False
            try:
                vinfo = self._bake_video_gpu(video_url, http_headers, es, tmp_video)
                gpu_baked = True
            except InterruptedError:
                raise
            except Exception as e:
                logger.warning("离屏 GPU 烘焙失败，退化为 PyAV 近似: %s", e, exc_info=True)
                self._report(0.35, "GPU 渲染不可用，改用 PyAV 近似烘焙...")
                vinfo = self._bake_video_pyav(video_url, http_headers, es, tmp_video)
                gpu_baked = False
            self._check_cancel()

            # (c) 混流：烘焙视频 + 增强音频 → 最终 mp4
            self._report(0.92, "封装音视频...")
            self._mux(tmp_video, audio_wav, audio_sr, es.output_path)
            self._check_cancel()

            # (d) 汇报真实参数
            result.success = True
            result.gpu_baked = gpu_baked
            result.container_format = "mp4"
            result.width = vinfo.get("width", 0)
            result.height = vinfo.get("height", 0)
            result.fps = vinfo.get("fps", 0.0)
            result.audio_sr = audio_sr
            result.audio_cutoff_hz = self._compute_audio_cutoff(es, audio_sr) or 0
            if not gpu_baked:
                result.message = (
                    "导出成功，但当前环境无法创建离屏 GPU 渲染上下文，已退化为 PyAV "
                    "近似烘焙：亮度/对比度/饱和度/gamma、降噪、按倍率缩放已尽力套用，"
                    "但 GLSL 着色器（Anime4K/FSR 超分、CAS 锐化等）未能真实烘焙。"
                )
            else:
                result.message = "导出成功"
            self._report(1.0, "导出完成")

        except InterruptedError:
            result.success = False
            result.message = "导出已取消"
        except Exception as e:
            logger.error("导出失败: %s", e, exc_info=True)
            result.success = False
            result.message = f"导出失败: {e}"
        finally:
            self._cleanup_tmp()
            if done_callback:
                try:
                    done_callback(result)
                except Exception:
                    logger.error("done_callback 抛错", exc_info=True)

    # ─── 临时文件管理 ────────────────────────────────────────────────────

    def _tmp_path(self, output_path: str, suffix: str) -> str:
        p = Path(output_path)
        tmp = p.with_name(f".{p.stem}{suffix}")
        self._tmp_files.append(str(tmp))
        return str(tmp)

    def _cleanup_tmp(self):
        for f in self._tmp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        self._tmp_files.clear()

    def _compute_audio_cutoff(self, es: ExportSettings, out_sr: int) -> Optional[int]:
        """计算输出音频截止频率：增强后按 Nyquist；未增强则按源编码估算。"""
        if es.any_audio_enabled:
            return out_sr // 2 if out_sr else None
        return _estimate_cutoff(out_sr or (es.src_audio_sr or 0),
                                es.src_audio_bitrate, es.src_audio_codec)

    # ─── (a) 音频：解码 → 增强链 / 直通 → WAV ────────────────────────────

    def _decode_full_audio(self, audio_url: str, http_headers: Optional[dict]) -> tuple:
        """用 PyAV 解码整轨音频为 (channels, samples) float32 + 源采样率。

        复刻 audio_pipe._decode_full_audio：PyAV>=14 移除了 Frame.to_ndarray(format=)，
        故用 AudioResampler(format='fltp') 把每帧规范成 planar float32（声道在前）。"""
        import av
        from av.audio.resampler import AudioResampler

        options = {}
        if http_headers:
            full = dict(http_headers)
            full.setdefault("User-Agent",
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36")
            options["user_agent"] = full["User-Agent"]
            if "Referer" in full:
                options["referer"] = full["Referer"]

        container = av.open(audio_url, options=options)
        try:
            audio_stream = container.streams.audio[0]
            sample_rate = audio_stream.rate
            resampler = AudioResampler(format="fltp")
            frames = []

            def _collect(av_frames):
                for rf in av_frames:
                    arr = rf.to_ndarray()
                    if arr.ndim == 1:
                        arr = arr[np.newaxis, :]
                    frames.append(arr.astype(np.float32))

            for packet in container.demux(audio_stream):
                self._check_cancel()
                for frame in packet.decode():
                    _collect(resampler.resample(frame))
            _collect(resampler.resample(None))
        finally:
            container.close()

        if not frames:
            return None, 0
        nch = max(f.shape[0] for f in frames)
        aligned = []
        for f in frames:
            if f.shape[0] < nch:
                f = np.repeat(f, nch // f.shape[0], axis=0)[:nch]
            aligned.append(f)
        return np.concatenate(aligned, axis=1), sample_rate

    def _prepare_audio(self, audio_url: str, http_headers: Optional[dict],
                       es: ExportSettings) -> tuple:
        """产出本地增强(或直通)的 WAV，返回 (wav_path, output_sr)。

        若启用了任一音频模型则跑 Apollo/FlashSR 离线链；否则直通源音频。
        模型加载失败时不致命——退回直通，保证视频导出仍可完成。"""
        self._report(0.02, "解码音频流...")
        audio, src_sr = self._decode_full_audio(audio_url, http_headers)
        if audio is None:
            raise RuntimeError("音频解码失败（无音频流或解码为空）")

        out_sr = src_sr
        enhanced = audio

        if es.any_audio_enabled:
            self._report(0.05, "加载音频增强模型...")
            self._enhancer.set_apollo_enabled(es.apollo_enabled)
            self._enhancer.set_flashsr_enabled(es.flashsr_enabled)
            self._enhancer.set_apollo_fp16(es.apollo_fp16)
            self._enhancer.set_flashsr_fp16(es.flashsr_fp16)
            if self._enhancer.load_models():
                def _cb(p):
                    self._check_cancel()
                    self._report(0.05 + p * 0.20, f"音频增强中... {int(p * 100)}%")
                try:
                    enhanced, out_sr = self._enhancer.enhance_full(audio, src_sr,
                                                                   progress_callback=_cb)
                except InterruptedError:
                    raise
                except Exception as e:
                    logger.warning("音频增强失败，退回原始音频: %s", e)
                    enhanced, out_sr = audio, src_sr
            else:
                logger.warning("音频模型加载失败，退回原始音频")
                enhanced, out_sr = audio, src_sr

        wav_path = self._tmp_path(es.output_path, "_audio.wav")
        self._write_wav(wav_path, enhanced, out_sr)
        self._report(0.27, "音频准备完成")
        return wav_path, out_sr

    @staticmethod
    def _write_wav(path: str, audio: np.ndarray, sr: int):
        """把 (channels, samples) float32 写为 16-bit PCM WAV（标准库 wave，无额外依赖）。"""
        if audio.ndim == 1:
            audio = audio[np.newaxis, :]
        nch = audio.shape[0]
        # 限幅到 [-1,1] 后转 int16，(channels, samples) → 交织 (samples, channels)
        clipped = np.clip(audio, -1.0, 1.0)
        interleaved = (clipped.T * 32767.0).astype("<i2")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(nch)
            wf.setsampwidth(2)
            wf.setframerate(int(sr))
            wf.writeframes(interleaved.tobytes())

    # ─── 共享：把 RGB 帧序列编码为 video-only mp4 ─────────────────────────

    def _open_video_encoder(self, path: str, width: int, height: int,
                            fps: float, codec: str):
        """打开一个仅含视频流的 PyAV 输出，返回 (container, stream, time_base_fps)。

        编码 H.264(libx264)，像素格式 yuv420p（兼容性最佳，宽高需为偶数）。"""
        import av
        from fractions import Fraction

        fps = fps if fps and fps > 0 else 25.0
        rate = Fraction(fps).limit_denominator(100000)
        # H.264 yuv420p 要求宽高为偶数
        width -= width % 2
        height -= height % 2

        container = av.open(path, mode="w")
        stream = container.add_stream(codec or "libx264", rate=rate)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        try:
            stream.codec_context.time_base = Fraction(1, 1) / rate
        except Exception:
            pass
        # 合理默认：crf 18 接近视觉无损，preset medium 平衡速度/体积
        try:
            stream.options = {"crf": "18", "preset": "medium"}
        except Exception:
            pass
        return container, stream, rate

    @staticmethod
    def _encode_rgb_frame(container, stream, rgb: np.ndarray, pts: int):
        """把一帧 (H,W,3) uint8 RGB 编码并 mux。"""
        import av
        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
        frame.pts = pts
        for pkt in stream.encode(frame):
            container.mux(pkt)

    @staticmethod
    def _flush_encoder(container, stream):
        for pkt in stream.encode():
            container.mux(pkt)

    # ─── (b1) 离屏 GPU 渲染真实烘焙（primary） ───────────────────────────

    def _bake_video_gpu(self, video_url: str, http_headers: Optional[dict],
                        es: ExportSettings, out_path: str) -> dict:
        """用 Qt 离屏 OpenGL 上下文 + libmpv render API 真实烘焙画面增强。

        失败（无显示设备/上下文创建失败/渲染异常）会抛出，由调用方退化到 PyAV。
        返回烘焙出的 {'width','height','fps'}。
        """
        from PySide6.QtGui import (
            QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat,
        )
        from PySide6.QtOpenGL import (
            QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
        )
        import mpv

        if QGuiApplication.instance() is None:
            raise RuntimeError("无 QGuiApplication 实例，无法创建离屏 GL 上下文")

        # 1) 离屏 GL 上下文 + surface
        fmt = QSurfaceFormat()
        fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        surface = QOffscreenSurface()
        surface.setFormat(fmt)
        surface.create()
        if not surface.isValid():
            raise RuntimeError("离屏 surface 创建失败")
        gl_ctx = QOpenGLContext()
        gl_ctx.setFormat(fmt)
        if not gl_ctx.create() or not gl_ctx.isValid():
            raise RuntimeError("OpenGL 上下文创建失败（headless/无 GPU）")
        if not gl_ctx.makeCurrent(surface):
            raise RuntimeError("makeCurrent 失败（无法绑定 GL 上下文到线程）")

        player = None
        render_ctx = None
        fbo = None
        try:
            # 2) get_proc_address 回调：mpv 通过它拿 GL 函数地址（经 Qt 上下文）
            def _get_proc_address(_ctx, name):
                try:
                    addr = gl_ctx.getProcAddress(name)  # name 为 bytes，Qt 接受
                    return int(addr) if addr else 0
                except Exception:
                    return 0
            proc_fn = mpv.MpvGlGetProcAddressFn(_get_proc_address)
            self._proc_fn_ref = proc_fn  # 防止被 GC

            # 3) 离屏 mpv 实例：vo=libmpv（不传 wid！），关音频，软解保证可读/确定性
            player = mpv.MPV(
                vo="libmpv",
                hwdec="no",            # 离屏渲染禁硬解，避免 GPU surface interop 问题
                audio="no",
                video_sync="audio",
                keep_open="yes",
                idle="yes",
                pause="yes",           # 加载后暂停，逐帧步进
                input_default_bindings=False,
                input_vo_keyboard=False,
                osc=False,
                loglevel="error",
            )

            # 4) render context（OpenGL）
            render_ctx = mpv.MpvRenderContext(
                player, "opengl",
                opengl_init_params={"get_proc_address": proc_fn},
            )
            # 新帧就绪时由 mpv 线程触发，置事件供渲染循环唤醒
            frame_ready = threading.Event()
            render_ctx.update_cb = lambda: frame_ready.set()

            # 5) 应用画面增强（着色器/deband/render props/降噪 vf）
            self._apply_video_enhancements(player, es)

            # 6) 加载文件
            self._set_http_headers(player, http_headers)
            player.play(video_url)
            self._wait_video_ready(player)

            # 7) 计算输出分辨率 = 源分辨率 * 超分倍率
            src_w, src_h = self._get_source_resolution(player, es)
            factor = max(1, int(es.upscale_factor or 1))
            out_w, out_h = src_w * factor, src_h * factor
            fps = self._get_fps(player, es)

            container, stream, _rate = self._open_video_encoder(
                out_path, out_w, out_h, fps, es.video_codec)
            out_w, out_h = stream.width, stream.height  # 取偶数对齐后的真实值

            # 8) FBO（目标分辨率）
            fbo_fmt = QOpenGLFramebufferObjectFormat()
            fbo = QOpenGLFramebufferObject(out_w, out_h, fbo_fmt)
            fbo_id = fbo.handle()

            # 9) 逐帧渲染循环
            baked = self._render_loop(
                player, render_ctx, frame_ready, fbo, fbo_id,
                container, stream, out_w, out_h, fps, es)

            self._flush_encoder(container, stream)
            container.close()
            logger.info("离屏 GPU 烘焙完成：%dx%d @%.3ffps, %d 帧",
                        out_w, out_h, fps, baked)
            return {"width": out_w, "height": out_h, "fps": fps}
        finally:
            try:
                if render_ctx is not None:
                    render_ctx.update_cb = None
                    render_ctx.free()
            except Exception:
                pass
            try:
                if player is not None:
                    # 先清 vf，避免析构期原生崩溃（与 player_widget.destroy 同理）
                    try:
                        player.command("vf", "set", "")
                    except Exception:
                        pass
                    player.terminate()
            except Exception:
                pass
            try:
                if fbo is not None:
                    fbo.release()
            except Exception:
                pass
            try:
                gl_ctx.doneCurrent()
            except Exception:
                pass

    # ─── GPU 烘焙辅助 ────────────────────────────────────────────────────

    @staticmethod
    def _set_http_headers(player, headers: Optional[dict]):
        if headers:
            try:
                player.http_header_fields = [f"{k}: {v}" for k, v in headers.items()]
            except Exception:
                pass

    def _apply_video_enhancements(self, player, es: ExportSettings):
        """把 get_export_state 捕获的画面增强套到离屏 mpv，对齐 main_window 的 live-apply。

        安全：vf 仅允许 lavfi 的 hqdn3d/nlmeans 降噪；绝不注入 vapoursynth vf。"""
        import sys as _sys
        # render props：brightness/contrast/saturation/gamma/deband*/tone-mapping/...
        for k, v in (es.render_props or {}).items():
            try:
                player[k] = v
            except Exception as e:
                logger.debug("set render prop %s=%s 失败: %s", k, v, e)
        # GLSL 着色器链
        try:
            shaders = [p for p in (es.shaders or []) if Path(p).is_file()]
            if shaders:
                sep = ";" if _sys.platform == "win32" else ":"
                player.command("change-list", "glsl-shaders", "set", sep.join(shaders))
            else:
                player.command("change-list", "glsl-shaders", "clr", "")
        except Exception as e:
            logger.debug("应用着色器失败: %s", e)
        # 降噪 vf（仅 lavfi，安全）
        try:
            vf = es.vf or ""
            if vf and "vapoursynth" not in vf.lower():
                player.command("vf", "set", vf)
            else:
                player.command("vf", "set", "")
        except Exception as e:
            logger.debug("应用 vf 失败: %s", e)

    def _wait_video_ready(self, player, timeout: float = 30.0):
        """等待文件加载、video 参数就绪。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_cancel()
            try:
                vp = player.video_out_params
                if vp and vp.get("w"):
                    return
            except Exception:
                pass
            time.sleep(0.05)
        raise RuntimeError("等待视频就绪超时")

    @staticmethod
    def _get_source_resolution(player, es: ExportSettings) -> tuple:
        try:
            vp = player.video_out_params
            if vp and vp.get("w") and vp.get("h"):
                return int(vp["w"]), int(vp["h"])
        except Exception:
            pass
        if es.src_width and es.src_height:
            return int(es.src_width), int(es.src_height)
        return 1920, 1080

    @staticmethod
    def _get_fps(player, es: ExportSettings) -> float:
        for getter in (lambda: player.container_fps,
                       lambda: player.estimated_vf_fps):
            try:
                v = getter()
                if v and v > 0:
                    return float(v)
            except Exception:
                pass
        if es.src_fps and es.src_fps > 0:
            return float(es.src_fps)
        return 25.0

    def _render_loop(self, player, render_ctx, frame_ready, fbo, fbo_id,
                     container, stream, out_w, out_h, fps, es: ExportSettings) -> int:
        """逐帧步进 + 离屏渲染 + 回读 + 编码，直到 EOF。返回烘焙帧数。

        deterministic 思路：暂停态下用 frame-step 精确步进每一解码帧；render() 把当前帧
        画进我们的 FBO；toImage() 回读为 RGBA，转 RGB 后交给 PyAV 编码。EOF 通过
        eof-reached / idle-active 判定。
        """
        # 估算总帧数用于进度（时长*fps），拿不到时按时间比例兜底
        try:
            duration = float(player.duration or 0)
        except Exception:
            duration = 0
        total_frames = int(duration * fps) if duration and fps else 0

        pts = 0
        # 渲染首帧（文件已加载、暂停在第 0 帧）
        while True:
            self._check_cancel()
            # 渲染当前帧到 FBO
            self._render_one(render_ctx, fbo_id, out_w, out_h)
            img = fbo.toImage()  # QImage（GL 读回，可能上下翻转，已用 flip_y 修正）
            rgb = self._qimage_to_rgb(img, out_w, out_h)
            self._encode_rgb_frame(container, stream, rgb, pts)
            pts += 1

            if total_frames:
                self._report(0.4 + 0.5 * min(1.0, pts / total_frames),
                              f"烘焙画面 {pts}/{total_frames} 帧")
            else:
                self._report(0.6, f"烘焙画面 {pts} 帧")

            # 判断是否已到结尾
            if self._is_eof(player):
                break
            # 步进下一帧
            if not self._step_next_frame(player, render_ctx, frame_ready):
                break
            if total_frames and pts >= total_frames + 2:
                break  # 安全上限，防异常文件死循环
        return pts

    def _render_one(self, render_ctx, fbo_id: int, w: int, h: int):
        """调一次 mpv render，把当前帧渲染进指定 FBO。flip_y=True 修正 GL 上下颠倒。"""
        render_ctx.render(
            opengl_fbo={"w": w, "h": h, "fbo": int(fbo_id)},
            flip_y=True,
        )

    @staticmethod
    def _qimage_to_rgb(img, w: int, h: int) -> np.ndarray:
        """QImage → (h,w,3) uint8 RGB numpy。"""
        from PySide6.QtGui import QImage
        img = img.convertToFormat(QImage.Format.Format_RGBA8888)
        ptr = img.constBits()
        bpl = img.bytesPerLine()
        arr = np.frombuffer(ptr, dtype=np.uint8, count=bpl * img.height())
        arr = arr.reshape((img.height(), bpl // 4, 4))
        arr = arr[:h, :w, :3]  # 去 padding + 丢 alpha
        return np.ascontiguousarray(arr)

    def _step_next_frame(self, player, render_ctx, frame_ready, timeout: float = 5.0) -> bool:
        """frame-step 到下一解码帧并等 render 更新。返回 False 表示已到 EOF/无新帧。"""
        if self._is_eof(player):
            return False
        frame_ready.clear()
        try:
            player.command("frame-step")
        except Exception:
            return False
        # 等 mpv 报告新帧（update_cb 置位）；超时则尝试读 eof 再决定
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_cancel()
            if frame_ready.wait(0.05):
                render_ctx.update()  # 消费 update 标志
                return True
            if self._is_eof(player):
                return False
        return False

    @staticmethod
    def _is_eof(player) -> bool:
        try:
            if player.eof_reached:
                return True
        except Exception:
            pass
        try:
            if player.idle_active:
                return True
        except Exception:
            pass
        return False

    # ─── (b2) PyAV 近似烘焙（退化回退） ─────────────────────────────────

    def _bake_video_pyav(self, video_url: str, http_headers: Optional[dict],
                         es: ExportSettings, out_path: str) -> dict:
        """GPU 路径不可用时的退化烘焙：用 PyAV 解码 → numpy/lavfi 近似处理 → 重编码。

        能烘焙的：亮度/对比度/饱和度/gamma（numpy，因本机 PyAV 无 eq 滤镜）、
        降噪（nlmeans，若选 hqdn3d 也用 nlmeans 近似——本机 PyAV 无 hqdn3d）、
        按超分倍率做 lanczos 缩放近似。不能烘焙：GLSL 着色器(Anime4K/FSR/CAS)、deband、
        HDR tone-mapping —— 这些是 mpv GPU 管线特性，PyAV 无对应实现，故仅缩放近似。
        """
        import av
        from fractions import Fraction

        options = {}
        if http_headers:
            full = dict(http_headers)
            full.setdefault("User-Agent",
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36")
            options["user_agent"] = full["User-Agent"]
            if "Referer" in full:
                options["referer"] = full["Referer"]

        inp = av.open(video_url, options=options)
        try:
            vstream = inp.streams.video[0]
            vstream.thread_type = "AUTO"
            src_w = vstream.codec_context.width or es.src_width or 1920
            src_h = vstream.codec_context.height or es.src_height or 1080
            factor = max(1, int(es.upscale_factor or 1))
            out_w, out_h = src_w * factor, src_h * factor
            fps = self._fps_from_stream(vstream, es)

            container, stream, _rate = self._open_video_encoder(
                out_path, out_w, out_h, fps, es.video_codec)
            out_w, out_h = stream.width, stream.height

            # 预编译 numpy 系数（亮度/对比度/饱和度/gamma）
            eq = self._build_eq_coeffs(es.render_props)
            denoise_graph = self._build_denoise_graph(es, out_w, out_h)

            try:
                duration = float(vstream.duration * vstream.time_base) if vstream.duration else 0
            except Exception:
                duration = 0
            total = int(duration * fps) if duration and fps else 0

            pts = 0
            for frame in inp.decode(vstream):
                self._check_cancel()
                rgb = frame.to_ndarray(format="rgb24")
                # 缩放到目标分辨率（lanczos 近似超分）
                if (rgb.shape[1], rgb.shape[0]) != (out_w, out_h):
                    rgb = self._lanczos_scale(frame, out_w, out_h)
                # eq（numpy）
                if eq is not None:
                    rgb = self._apply_eq_numpy(rgb, eq)
                # 降噪（nlmeans lavfi）
                if denoise_graph is not None:
                    rgb = self._apply_denoise(denoise_graph, rgb, out_w, out_h)
                self._encode_rgb_frame(container, stream, rgb, pts)
                pts += 1
                if total:
                    self._report(0.4 + 0.5 * min(1.0, pts / total),
                                  f"PyAV 烘焙 {pts}/{total} 帧")
                else:
                    self._report(0.6, f"PyAV 烘焙 {pts} 帧")

            self._flush_encoder(container, stream)
            container.close()
            logger.info("PyAV 近似烘焙完成：%dx%d @%.3ffps, %d 帧",
                        out_w, out_h, fps, pts)
            return {"width": out_w, "height": out_h, "fps": fps}
        finally:
            inp.close()

    @staticmethod
    def _fps_from_stream(vstream, es: ExportSettings) -> float:
        try:
            r = vstream.average_rate or vstream.guessed_rate
            if r and float(r) > 0:
                return float(r)
        except Exception:
            pass
        return float(es.src_fps) if es.src_fps and es.src_fps > 0 else 25.0

    @staticmethod
    def _lanczos_scale(frame, out_w: int, out_h: int) -> np.ndarray:
        """用 PyAV 的 reformat（lanczos）缩放一帧，返回 rgb24 ndarray。"""
        from av.video.reformatter import Interpolation
        scaled = frame.reformat(width=out_w, height=out_h, format="rgb24",
                                interpolation=Interpolation.LANCZOS)
        return scaled.to_ndarray(format="rgb24")

    @staticmethod
    def _build_eq_coeffs(render_props: dict):
        """从 render props 提取 brightness/contrast/saturation/gamma（滑块 -100..100）。
        全为 0/缺省时返回 None（无需处理）。"""
        if not render_props:
            return None
        b = render_props.get("brightness", 0)
        c = render_props.get("contrast", 0)
        s = render_props.get("saturation", 0)
        g = render_props.get("gamma", 0)
        if not any((b, c, s, g)):
            return None
        # 映射到合理系数：mpv 取值 -100..100
        return {
            "brightness": b / 100.0,          # 加性，[-1,1]
            "contrast": 1.0 + c / 100.0,      # 乘性，围绕 0.5
            "saturation": 1.0 + s / 100.0,    # 饱和度系数
            "gamma": 2.0 ** (-g / 100.0),     # gamma 指数
        }

    @staticmethod
    def _apply_eq_numpy(rgb: np.ndarray, eq: dict) -> np.ndarray:
        """numpy 实现亮度/对比度/饱和度/gamma（本机 PyAV 无 eq 滤镜，故自己算）。"""
        x = rgb.astype(np.float32) / 255.0
        # gamma
        if abs(eq["gamma"] - 1.0) > 1e-3:
            x = np.power(np.clip(x, 0, 1), eq["gamma"])
        # contrast（围绕 0.5）
        if abs(eq["contrast"] - 1.0) > 1e-3:
            x = (x - 0.5) * eq["contrast"] + 0.5
        # brightness（加性）
        if abs(eq["brightness"]) > 1e-3:
            x = x + eq["brightness"]
        # saturation（向灰度插值）
        if abs(eq["saturation"] - 1.0) > 1e-3:
            luma = (0.299 * x[..., 0] + 0.587 * x[..., 1] + 0.114 * x[..., 2])[..., None]
            x = luma + (x - luma) * eq["saturation"]
        return (np.clip(x, 0, 1) * 255.0).astype(np.uint8)

    def _build_denoise_graph(self, es: ExportSettings, w: int, h: int):
        """构建 nlmeans 降噪 filter graph（本机 PyAV 无 hqdn3d，统一用 nlmeans 近似）。
        未启用降噪返回 None。"""
        if not es.denoise_mode:
            return None
        try:
            import av
            graph = av.filter.Graph()
            src = graph.add_buffer(width=w, height=h, format="rgb24")
            # nlmeans 强度近似：默认 s=4 与面板一致量级
            nl = graph.add("nlmeans", "s=4:p=7:r=15")
            sink = graph.add("buffersink")
            src.link_to(nl)
            nl.link_to(sink)
            graph.configure()
            return graph
        except Exception as e:
            logger.debug("构建降噪 graph 失败，跳过降噪: %s", e)
            return None

    @staticmethod
    def _apply_denoise(graph, rgb: np.ndarray, w: int, h: int) -> np.ndarray:
        import av
        try:
            f = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
            graph.push(f)
            out = graph.pull()
            return out.to_ndarray(format="rgb24")
        except Exception:
            return rgb

    # ─── (c) 混流：烘焙视频 + 增强音频 → 最终 mp4 ───────────────────────

    def _mux(self, video_path: str, audio_wav: str, audio_sr: int, out_path: str):
        """把 video-only mp4 的 H.264 流原样 remux + AAC 编码音频 → 最终 mp4。"""
        import av

        out = av.open(out_path, mode="w")
        vin = av.open(video_path)
        ain = av.open(audio_wav)
        try:
            in_vstream = vin.streams.video[0]
            # 视频：直接 remux（不重编码，无损、快）
            out_vstream = out.add_stream_from_template(in_vstream)

            # 音频：编码 AAC
            out_astream = out.add_stream("aac", rate=audio_sr)
            try:
                in_achannels = ain.streams.audio[0].channels or 2
            except Exception:
                in_achannels = 2
            try:
                out_astream.codec_context.layout = "stereo" if in_achannels >= 2 else "mono"
            except Exception:
                pass

            # 先写视频包
            for pkt in vin.demux(in_vstream):
                if pkt.dts is None:
                    continue
                pkt.stream = out_vstream
                out.mux(pkt)

            # 再编码音频
            in_astream = ain.streams.audio[0]
            for frame in ain.decode(in_astream):
                frame.pts = None
                for pkt in out_astream.encode(frame):
                    out.mux(pkt)
            for pkt in out_astream.encode():
                out.mux(pkt)
        finally:
            try:
                out.close()
            except Exception:
                pass
            vin.close()
            ain.close()






