"""视频导出引擎：把当前视频连同已配置的音频/画面增强真实烘焙为 mp4。

本模块是编排入口；实现拆分在 src/core/export/ 包：
    common.py    — ExportSettings / ExportResult / 格式化工具
    audio.py     — 音频子管线（解码 → Apollo/FlashSR → WAV）
    bake_gpu.py  — 离屏 GPU 渲染真实烘焙（primary）
    bake_pyav.py — PyAV 近似烘焙（退化回退）+ 共享编码器工具
    mux.py       — 混流

对外 API（VideoExporter / ExportSettings / ExportResult 及其全部私有方法名）
与拆分前完全一致，main_window 与 tests 的 import 路径不变。

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

   ExportResult.video_info_label 形如 "mp4 / 3840×2160 / 23.976fps / 48kHz / 24kHz"。

门控条件：current_stream 存在且 not is_live。

==============================================================================
设计要点
==============================================================================
- 画面烘焙策略 = 离屏 GPU 渲染真实烘焙（primary，见 export/bake_gpu.py）；
  失败退化到 PyAV 近似（export/bake_pyav.py），并在 ExportResult.gpu_baked=False
  且 message 里明确告知用户。
- 插帧不烘焙：display-resample 伪插帧是显示期属性（依赖显示器刷新率），无法写进文件；
  小黄鸭(Lossless Scaling)是外部全屏叠加程序——两者导出时一律忽略，导出文件保持源帧率。
"""

import logging
import threading
from pathlib import Path
from typing import Optional

from src.core.export.common import (
    ensure_libmpv_on_path,
    format_audio_scheme as _format_audio_scheme,
    format_sr as _format_sr,
    format_freq as _format_freq,
    estimate_cutoff as _estimate_cutoff,
    ExportSettings,
    ExportResult,
    ProgressCallback,
    DoneCallback,
)
from src.core.export.audio import AudioPipelineMixin
from src.core.export.bake_gpu import GpuBakeMixin
from src.core.export.bake_pyav import VideoEncoderMixin
from src.core.export.mux import MuxMixin

__all__ = ["VideoExporter", "ExportSettings", "ExportResult",
           "_format_audio_scheme", "_estimate_cutoff", "_format_sr", "_format_freq"]

logger = logging.getLogger(__name__)


class VideoExporter(AudioPipelineMixin, GpuBakeMixin, VideoEncoderMixin, MuxMixin):
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

    # ─── 临时文件管理 ────────────────────────────────────────────────────

    def _tmp_path(self, output_path: str, suffix: str) -> str:
        p = Path(output_path)
        tmp = p.with_name(f".{p.stem}{suffix}")
        self._tmp_files.append(str(tmp))
        return str(tmp)

    def _cleanup_tmp(self):
        import os as _os
        for f in self._tmp_files:
            try:
                if _os.path.exists(f):
                    _os.remove(f)
            except OSError:
                pass
        self._tmp_files.clear()

    def _compute_audio_cutoff(self, es: ExportSettings, out_sr: int) -> Optional[int]:
        """计算输出音频截止频率：增强后按 Nyquist；未增强则按源编码估算。"""
        if es.any_audio_enabled:
            return out_sr // 2 if out_sr else None
        return _estimate_cutoff(out_sr or (es.src_audio_sr or 0),
                                es.src_audio_bitrate, es.src_audio_codec)

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
            ensure_libmpv_on_path()

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
