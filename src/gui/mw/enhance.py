"""MainWindow 增强集成 mixin：音频增强(Apollo/FlashSR)面板接线、视频增强面板
（属性/着色器/deband/降噪 vf/HDR/超分倍率/帧生成入口）、资源监视器。

从 main_window.py 拆出；self 即 MainWindow 实例。
"""

import logging
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Slot, QTimer
from PySide6.QtWidgets import QMessageBox

from src.core.enhancer import Backend
from src.core.audio_pipe import PipelineState, PipelineStatus
from src.core.resource_monitor import ResourceMonitor

logger = logging.getLogger(__name__)


class EnhanceIntegrationMixin:
    def _start_resource_monitor(self, backend: Backend):
        """Initialize the resource monitor and start periodic updates."""
        self._resource_monitor = ResourceMonitor(backend)
        self._resource_timer = QTimer(self)
        self._resource_timer.timeout.connect(self._update_resource_stats)
        self._resource_timer.start(1500)  # update every 1.5s
        # Do an immediate first update
        self._update_resource_stats()

    @Slot()
    def _update_resource_stats(self):
        """Update the resource usage label in the status bar."""
        text = self._resource_monitor.format_stats()
        self._resource_label.setText(text)
        # 小黄鸭启用时周期性刷新帧生成指示器，使其在进入/退出全屏后
        # 自动在“待全屏(黄)”与“生效(绿)”之间切换，无需用户操作。
        if self._framegen_state.get("backend", "off") == "lossless-scaling":
            self._render_framegen_indicator()

    # --- Enhancement integration ---

    @Slot(dict)
    def _on_enhance_settings_changed(self, settings: dict):
        """Handle enhance panel toggle change — turn enhanced audio off when both
        models are deselected. Turning a model on does NOT auto-process; the user
        must click '修复当前音频'. Re-enabling can resume already-rendered audio."""
        any_enabled = settings["apollo_enabled"] or settings["flashsr_enabled"]
        if not any_enabled and self._enhanced_playing:
            self._sync.deactivate_enhanced()
            self._enhanced_playing = False
            self._update_media_info()
            self._status_label.setText("已切换回原始音频")
        elif any_enabled and not self._enhanced_playing:
            status = self._pipeline.status
            if status.enhanced_file and status.state == PipelineState.READY:
                # 只接回仍属于当前流的增强结果
                cur_url = (self._current_stream.audio_url or self._current_stream.video_url
                           ) if self._current_stream else None
                if cur_url and status.source_url == cur_url:
                    current_pos = self._player_widget.position
                    self._sync.activate_enhanced(status.enhanced_file, current_pos)
                    self._enhanced_playing = True
                    self._update_media_info()
                    self._status_label.setText("已切换回增强音频")

    def _init_enhance_backend(self):
        """Detect GPU backend and model availability in background thread.

        All heavy imports (torch) happen here off the main thread.
        Emits backend_ready when the panel has been updated.
        """
        def _worker():
            info = self._enhancer.device_info
            avail = self._enhancer.available()
            self._enhance_status_update.emit(
                ("backend_ready", info, avail["apollo"], avail["flashsr"])
            )

        threading.Thread(target=_worker, daemon=True).start()

    @Slot()
    def _on_enhance_requested(self):
        """User clicked '修复当前音频'. Loads enabled models and runs the chain in
        the background; original audio keeps playing until the result is ready."""
        if self._enhance_busy:
            # 忙时忽略：并发加载/推理是 ROCm 原生闪退的主要来源
            self._status_label.setText("已有增强任务进行中 — 请等待完成或先取消")
            return
        if self._current_stream is None:
            QMessageBox.warning(self, "提示", "请先播放一个视频/音频")
            return

        settings = self._enhance_panel.get_settings()
        self._enhanced_scheme_label = self._format_enhance_scheme(settings)
        self._enhancer.set_apollo_enabled(settings["apollo_enabled"])
        self._enhancer.set_flashsr_enabled(settings["flashsr_enabled"])
        self._enhancer.set_apollo_fp16(settings.get("apollo_fp16", False))
        self._enhancer.set_flashsr_fp16(settings.get("flashsr_fp16", False))
        if not self._enhancer.any_enabled:
            QMessageBox.warning(self, "提示", "请至少勾选一个增强模型 (Apollo / FlashSR)")
            return

        # 在主线程一次性捕获流信息，避免 worker 运行期间切视频导致错配
        audio_url = self._current_stream.audio_url or self._current_stream.video_url
        headers = self._current_stream.http_headers
        duration_hint = float(getattr(self._current_stream, "duration", 0) or 0.0)

        self._enhance_busy = True
        abort_evt = threading.Event()
        self._enhance_abort_evt = abort_evt
        self._enhance_panel.show_progress(True)
        self._enhance_panel.update_progress(0.0, "正在加载模型...")

        def _load_and_enhance():
            ok = self._enhancer.load_models()

            if abort_evt.is_set():
                # 加载期间用户点了取消：这里持锁的只有本线程，可安全释放
                self._enhancer.unload()
                self._enhance_status_update.emit(("enhance_aborted",))
                return

            if not ok:
                failed = "、".join(self._enhancer.last_load_errors) or "未知原因"
                self._enhance_status_update.emit(
                    PipelineStatus(state=PipelineState.ERROR,
                                   message=f"模型加载失败: {failed}（请检查模型文件是否存在）")
                )
                return

            self._enhance_status_update.emit(("model_loaded", None))
            self._pipeline.start_enhance(audio_url, headers, duration_hint)

        threading.Thread(target=_load_and_enhance, daemon=True).start()

    @Slot()
    def _on_enhance_cancel(self):
        self._pipeline.cancel()
        if self._enhance_busy:
            # 模型加载线程在加载完成后检查自己的取消事件并自行释放
            if self._enhance_abort_evt is not None:
                self._enhance_abort_evt.set()
        self._enhance_busy = False
        self._enhance_panel.show_progress(False)
        self._enhanced_duration_s = 0.0
        self._status_label.setText("增强已取消")
        # 释放模型：若 worker 仍占 GPU 锁（正在推理一个块），稍后重试
        if not self._enhancer.unload_if_idle():
            QTimer.singleShot(2500, self._enhancer.unload_if_idle)

    @Slot(object)
    def _handle_enhance_status(self, status):
        """Handle enhancement status updates on the main thread."""
        # Handle tuple messages from backend init
        if isinstance(status, tuple):
            msg_type = status[0]
            if msg_type == "backend_ready":
                _, info, apollo_avail, flashsr_avail = status
                backend_text = {
                    Backend.ROCM: f"ROCm ({info.device_name})",
                    Backend.DIRECTML: "DirectML",
                    Backend.CPU: "CPU (慢)",
                }[info.backend]
                self._enhance_panel.set_backend_info(
                    backend_text, info.backend != Backend.CPU
                )
                self._enhance_panel.set_models_available(apollo_avail, flashsr_avail)
                if apollo_avail or flashsr_avail:
                    parts = []
                    if apollo_avail:
                        parts.append("Apollo")
                    if flashsr_avail:
                        parts.append("FlashSR")
                    self._enhance_panel.set_model_status(
                        f"可用: {', '.join(parts)}", True
                    )
                else:
                    self._enhance_panel.set_model_status("未找到模型文件", False)
                self.backend_ready.emit()
                self._start_resource_monitor(info.backend)
                return
            elif msg_type == "model_loaded":
                self._enhance_panel.set_model_status("已加载", True)
                return
            elif msg_type == "enhance_aborted":
                # 加载期间被取消：模型已由加载线程释放
                self._enhance_busy = False
                self._enhance_panel.show_progress(False)
                self._status_label.setText("增强已取消")
                return

        # Handle PipelineStatus
        if not isinstance(status, PipelineStatus):
            return

        self._enhance_panel.update_progress(status.progress, status.message)
        if status.state in (PipelineState.DECODING, PipelineState.ENHANCING):
            self._enhance_busy = True

        # Track how much enhanced audio is available
        if status.enhanced_duration_s > 0:
            self._enhanced_duration_s = status.enhanced_duration_s
            # Keep sync manager aware of the write frontier
            self._sync.update_enhanced_duration(status.enhanced_duration_s)

        if status.state == PipelineState.READY and status.enhanced_file:
            self._enhance_busy = False
            self._enhance_panel.show_progress(False)
            self._enhanced_duration_s = status.enhanced_duration_s
            self._enhanced_output_sr = status.output_sr

            # 过期流防护：增强期间用户切换了视频/停止播放 → 丢弃旧流结果，
            # 严禁把旧视频的增强音频挂到新视频上
            cur_url = (self._current_stream.audio_url or self._current_stream.video_url
                       ) if self._current_stream else None
            if not cur_url or (status.source_url and status.source_url != cur_url):
                self._status_label.setText("增强完成，但音频源已切换 — 结果已忽略")
                QTimer.singleShot(3000, lambda: self._pipeline.cleanup_old_files(
                    keep=status.enhanced_file))
                return

            was_playing_same = (self._enhanced_playing
                                and getattr(self, "_active_enhanced_file", None)
                                == status.enhanced_file)
            if was_playing_same:
                # 已在渐进播放本文件：前沿即完整时长，无需重挂音轨（避免中断）
                self._status_label.setText("增强完成 — 已在增强音频上继续播放")
                QTimer.singleShot(3000, lambda: self._pipeline.cleanup_old_files(
                    keep=status.enhanced_file))
                return

            # 总是切换到新结果（每个结果独立文件名，不会覆盖正在播的旧文件）
            was_playing_other = self._enhanced_playing
            current_pos = self._player_widget.position
            self._sync.activate_enhanced(status.enhanced_file, current_pos)
            self._enhanced_playing = True
            self._update_media_info()
            self._status_label.setText(
                "重新增强完成 — 已切换到新结果" if was_playing_other
                else "增强完成 — 切换到增强音频")
            # mpv 释放旧文件句柄后清理旧代临时文件（失败会静默跳过）
            QTimer.singleShot(3000, lambda: self._pipeline.cleanup_old_files(
                keep=status.enhanced_file))

        elif status.state == PipelineState.ERROR:
            self._enhance_busy = False
            self._enhance_panel.show_progress(False)
            if status.recoverable and self._enhanced_playing:
                self._sync.fallback_to_original(status.message)
                self._enhanced_playing = False
                self._update_media_info()
                self._status_label.setText(f"增强失败，已回退到原始音频: {status.message}")
            else:
                self._status_label.setText(f"增强失败: {status.message}")
                QMessageBox.warning(self, "增强失败", status.message)

        elif status.state == PipelineState.IDLE:
            self._enhance_busy = False

    # --- Video enhancement integration ---

    @Slot(str, object)
    def _on_video_property_changed(self, prop: str, value):
        """Apply mpv video property change (brightness/contrast/saturation/gamma)."""
        player = self._player_widget._player
        if player:
            try:
                player[prop] = value
            except (RuntimeError, OSError):
                pass

    # Keywords that identify upscale shaders (as opposed to CAS/sharpening-only shaders)
    _UPSCALE_SHADER_KEYWORDS = (
        "Anime4K_Upscale", "Anime4K_Restore", "Anime4K_Upscale_Denoise",
        "FSR", "FSRCNNX",
    )

    @Slot(list)
    def _on_video_shader_changed(self, shader_paths: list):
        """Apply GLSL shader list to mpv and verify upscale shaders are actually loaded."""
        player = self._player_widget._player
        if not player:
            self._upscale_actually_active = False
            self._update_audio_source_indicator()
            return
        try:
            if shader_paths:
                # Verify all shader files exist on disk before applying
                missing = [p for p in shader_paths if not Path(p).is_file()]
                if missing:
                    logger.warning("Shader files not found: %s", missing)
                    # Only apply the ones that exist
                    shader_paths = [p for p in shader_paths if Path(p).is_file()]

                if shader_paths:
                    sep = ";" if sys.platform == "win32" else ":"
                    shader_str = sep.join(shader_paths)
                    # 必须先 clr 再 set：mpv 对"同路径 set"是无操作（不重读文件，
                    # 已在真实 libmpv 上实测确认），而 CAS/FSR 的生成文件内容会随
                    # 参数实时改写，不先清掉就无法把新强度应用到已挂载的着色器。
                    player.command("change-list", "glsl-shaders", "clr", "")
                    player.command("change-list", "glsl-shaders", "set", shader_str)
                else:
                    player.command("change-list", "glsl-shaders", "clr", "")
            else:
                player.command("change-list", "glsl-shaders", "clr", "")
        except (RuntimeError, OSError) as e:
            logger.warning("Failed to apply shaders: %s", e)
            self._upscale_actually_active = False
            self._update_audio_source_indicator()
            return

        # Verify: check if upscale-related shaders are actually present
        self._upscale_actually_active = self._check_upscale_active(shader_paths)
        self._update_audio_source_indicator()

    def _check_upscale_active(self, applied_paths: list | None = None) -> bool:
        """Check whether upscale shaders are actually active.

        First checks the provided path list for upscale keywords. If no list is
        provided, reads mpv's glsl-shaders property to determine what's loaded.
        Returns True if at least one upscale shader is present and its file exists.
        """
        paths_to_check = applied_paths

        if paths_to_check is None:
            # Read back from mpv to see what's actually loaded
            player = self._player_widget._player
            if not player:
                return False
            try:
                shader_prop = player["glsl-shaders"]
                if not shader_prop:
                    return False
                sep = ";" if sys.platform == "win32" else ":"
                paths_to_check = [p.strip() for p in shader_prop.split(sep) if p.strip()]
            except (RuntimeError, OSError, TypeError):
                return False

        if not paths_to_check:
            return False

        for path_str in paths_to_check:
            filename = Path(path_str).name
            if any(kw in filename for kw in self._UPSCALE_SHADER_KEYWORDS):
                # Confirm the file actually exists on disk
                if Path(path_str).is_file():
                    return True
        return False

    @Slot(bool, dict)
    def _on_video_deband_changed(self, enabled: bool, params: dict):
        """Apply deband settings to mpv."""
        player = self._player_widget._player
        if not player:
            return
        try:
            player["deband"] = "yes" if enabled else "no"
            if enabled and params:
                if "iterations" in params:
                    player["deband-iterations"] = params["iterations"]
                if "threshold" in params:
                    player["deband-threshold"] = params["threshold"]
                if "range" in params:
                    player["deband-range"] = params["range"]
        except (RuntimeError, OSError):
            pass

    @Slot(str)
    def _on_video_vf_changed(self, vf_str: str):
        """应用降噪 vf（hqdn3d/nlmeans）。小黄鸭是外部叠加程序、不接入 mpv vf 链，
        故降噪独占 mpv vf 链（二者可同时开启）。

        hqdn3d/nlmeans 是 lavfi(CPU) 滤镜，需要 CPU 可读帧；硬解 d3d11 帧喂不进去会被
        mpv 禁用（日志 'Impossible to convert ... d3d11'）。故启用降噪时切 auto-copy，
        关闭时恢复 auto-safe。
        """
        player = self._player_widget._player
        if not player:
            return
        try:
            if vf_str:
                self._player_widget.set_hwdec_for_vf(True)  # 降噪需 CPU 可读帧
                player.command("vf", "set", vf_str)
            else:
                player.command("vf", "set", "")
                self._player_widget.set_hwdec_for_vf(False)  # 无 CPU 滤镜，恢复零拷贝
        except (RuntimeError, OSError):
            pass

    @Slot(bool, dict)
    def _on_video_hdr_changed(self, enabled: bool, params: dict):
        """Apply HDR tone mapping settings to mpv."""
        player = self._player_widget._player
        if not player:
            return
        try:
            if enabled:
                player["tone-mapping"] = params.get("tone-mapping", "bt.2390")
                player["hdr-compute-peak"] = "yes" if params.get("hdr-compute-peak", True) else "no"
            else:
                player["tone-mapping"] = "auto"
                player["hdr-compute-peak"] = "auto"
        except (RuntimeError, OSError):
            pass
