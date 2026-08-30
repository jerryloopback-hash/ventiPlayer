"""MainWindow 状态栏 mixin：媒体信息标签、● 指示器（音源/超分/帧生成/独占）、
格式化工具（采样率/频率/截止估算/增强方案）。

从 main_window.py 拆出；self 即 MainWindow 实例。
"""

from src.core.stream import StreamInfo


class MediaInfoMixin:
    def _update_media_info(self, *_args):
        """Rebuild the media info label: V-res-fps → out_res | A-sr-cutoff → output

        独占/增强/超分/帧生成等状态改由各自的 ● 指示器显示，本标签只保留 V/A 段。
        """
        stream = self._current_stream
        if not stream:
            self._media_info_label.setText("")
            self._audio_source_indicator.setText("")
            self._exclusive_indicator.setText("")
            return

        parts = []

        # Video info: V-1080×720-30fps or V-1080×720-30fps → 2160×1440-30fps
        src_w = stream.video_width or 0
        src_h = stream.video_height or 0
        src_fps = stream.video_fps or 0.0

        if src_w and src_h:
            v_src = f"{src_w}×{src_h}"
        elif stream.video_resolution:
            v_src = stream.video_resolution
        else:
            v_src = ""

        fps_str = ""
        if src_fps:
            if src_fps == int(src_fps):
                fps_str = f"{int(src_fps)}fps"
            else:
                fps_str = f"{src_fps:.1f}fps"

        if v_src:
            v_info = f"V-{v_src}"
            if fps_str:
                v_info += f"-{fps_str}"

            # 计算有效输出帧率
            effective_out_fps = self._video_out_fps
            st = self._framegen_state
            fg_applied = st.get("applied", False)
            fg_backend = st.get("backend", "off")
            if fg_backend == "display-resample" and fg_applied:
                # 伪插帧：受显示刷新率约束，取 display-fps
                try:
                    player = self._player_widget._player
                    display_fps = player["display-fps"] if player else None
                    if display_fps and display_fps > 0:
                        effective_out_fps = round(display_fps, 1)
                except Exception:
                    pass
            # 小黄鸭(lossless-scaling)是外部叠加补帧，mpv 仍只输出源帧，无需调整 effective_out_fps

            # Show output resolution: use upscale factor applied to video-out-params
            out_w = self._video_out_w * self._upscale_factor if self._video_out_w else 0
            out_h = self._video_out_h * self._upscale_factor if self._video_out_h else 0
            show_arrow = (self._upscale_factor > 1 and out_w > 0 and out_h > 0) or fg_applied
            if show_arrow:
                if out_w == 0 or out_h == 0:
                    out_w = self._video_out_w or src_w
                    out_h = self._video_out_h or src_h
                out_fps_str = ""
                if effective_out_fps:
                    if effective_out_fps == int(effective_out_fps):
                        out_fps_str = f"{int(effective_out_fps)}fps"
                    else:
                        out_fps_str = f"{effective_out_fps:.1f}fps"
                v_out = f"{out_w}×{out_h}"
                if out_fps_str:
                    v_out += f"-{out_fps_str}"
                v_info += f" → {v_out}"

            parts.append(v_info)

        # Audio info: A-44.1kHz-16kHz → 48kHz-24kHz
        a_str = self._format_audio_info(stream)
        if a_str:
            parts.append(a_str)

        self._media_info_label.setText(" | ".join(parts))
        self._update_audio_source_indicator()

    def _update_audio_source_indicator(self):
        """Update the audio source indicator: green dot + 实际增强方案 or gray dot + '源音频'."""
        if not self._current_stream:
            self._audio_source_indicator.setText("")
            self._upscale_indicator.setText("")
            self._framegen_indicator.setText("")
            self._exclusive_indicator.setText("")
            return
        if self._enhanced_playing:
            scheme = self._enhanced_scheme_label or "增强"
            self._audio_source_indicator.setText(
                f'<span style="color: #4CAF50; font-size: 14px;">●</span> {scheme}'
            )
        else:
            self._audio_source_indicator.setText(
                '<span style="color: #9E9E9E; font-size: 14px;">●</span> 源音频'
            )
        # Upscale indicator — based on whether shaders are actually loaded
        if getattr(self, '_upscale_actually_active', False):
            self._upscale_indicator.setText(
                '<span style="color: #4CAF50; font-size: 14px;">●</span> 超分'
            )
        else:
            self._upscale_indicator.setText(
                '<span style="color: #9E9E9E; font-size: 14px;">●</span> 未超分'
            )
        # Frame-gen indicator — 小黄鸭 / 伪插帧 / 源帧率
        self._render_framegen_indicator()
        # Exclusive indicator — WASAPI Exclusive 开=绿/独占，关=灰/非独占
        if self._exclusive_check.isChecked():
            self._exclusive_indicator.setText(
                '<span style="color: #4CAF50; font-size: 14px;">●</span> 独占'
            )
        else:
            self._exclusive_indicator.setText(
                '<span style="color: #9E9E9E; font-size: 14px;">●</span> 非独占'
            )

    def _render_framegen_indicator(self):
        """状态栏帧生成指示器：按 backend/生效状态渲染。

        - off               灰 源帧率
        - display-resample  绿 伪插帧（注入即生效）
        - lossless-scaling  绿 小黄鸭 生效（已发送快捷键开启缩放）/ 黄 小黄鸭 待全屏
        """
        st = self._framegen_state
        backend = st.get("backend", "off")
        if backend == "off":
            self._framegen_indicator.setText(
                '<span style="color:#9E9E9E;font-size:14px;">●</span> 源帧率')
            return
        if backend == "lossless-scaling":
            if self._ls_controller.is_scaling:
                self._framegen_indicator.setText(
                    '<span style="color:#4CAF50;font-size:14px;">●</span> 小黄鸭 生效')
            else:
                self._framegen_indicator.setText(
                    '<span style="color:#FFEB3B;font-size:14px;">●</span> 小黄鸭 待全屏')
            return
        applied = self._verify_framegen_applied(backend)
        st["applied"] = applied
        if not applied:
            self._framegen_indicator.setText(
                '<span style="color:#FF9800;font-size:14px;">●</span> 帧生成(异常)')
            return
        # display-resample：注入即视为生效（绿）
        self._framegen_indicator.setText(
            '<span style="color:#4CAF50;font-size:14px;">●</span> 伪插帧')

    def _framegen_is_effective(self) -> bool:
        """帧生成是否真正生效。

        - lossless-scaling：以小黄鸭是否已开启缩放为准。
        - display-resample：注入即生效（applied 已校验）。
        """
        if self._framegen_state.get("backend") == "lossless-scaling":
            return self._ls_controller.is_scaling
        return self._framegen_state.get("applied", False)

    def _verify_framegen_applied(self, backend: str) -> bool:
        """校验后端是否真正生效：伪插帧查 video-sync，小黄鸭看是否已选中。"""
        if backend == "lossless-scaling":
            return self._ls_backend_selected
        try:
            player = self._player_widget._player
            if not player:
                return False
            if backend == "display-resample":
                return player["video-sync"] == "display-resample"
            return False
        except Exception:
            return False

    def _format_audio_info(self, stream: StreamInfo) -> str:
        """Format audio section: A-44.1kHz-16kHz → 48kHz-24kHz"""
        src_sr = stream.audio_sample_rate
        if not src_sr:
            return ""

        src_sr_str = self._format_sr(src_sr)
        src_cutoff = self._estimate_cutoff(src_sr, stream.audio_bitrate, stream.audio_codec)
        src_cutoff_str = self._format_freq(src_cutoff) if src_cutoff else ""

        src_part = f"A-{src_sr_str}"
        if src_cutoff_str:
            src_part += f"-{src_cutoff_str}"

        if self._enhanced_playing:
            # Output SR comes from the pipeline (Apollo=44.1k / FlashSR=48k)
            enhanced_sr = self._enhanced_output_sr or src_sr
            out_sr_str = self._format_sr(enhanced_sr)
            out_cutoff = enhanced_sr // 2
            out_cutoff_str = self._format_freq(out_cutoff)
            return f"{src_part} → {out_sr_str}-{out_cutoff_str}"
        elif self._output_sr and self._output_sr != src_sr:
            out_sr_str = self._format_sr(self._output_sr)
            return f"{src_part} → {out_sr_str}"
        else:
            return src_part

    @staticmethod
    def _format_enhance_scheme(settings: dict) -> str:
        """根据增强设置拼出实际方案标签，如 'Apollo(fp32)+FlashSR(fp16)'。"""
        parts = []
        if settings.get("apollo_enabled"):
            parts.append("Apollo(fp16)" if settings.get("apollo_fp16") else "Apollo(fp32)")
        if settings.get("flashsr_enabled"):
            parts.append("FlashSR(fp16)" if settings.get("flashsr_fp16") else "FlashSR(fp32)")
        return "+".join(parts) if parts else "增强"

    @staticmethod
    def _format_sr(sr: int) -> str:
        """Format sample rate: 44100 → '44.1kHz', 48000 → '48kHz'"""
        khz = sr / 1000
        if khz == int(khz):
            return f"{int(khz)}kHz"
        return f"{khz:.1f}kHz"

    @staticmethod
    def _format_freq(freq: int) -> str:
        """Format frequency: 16000 → '16kHz', 22050 → '22kHz'"""
        khz = freq / 1000
        if khz >= 10:
            return f"{int(round(khz))}kHz"
        return f"{khz:.1f}kHz"

    @staticmethod
    def _estimate_cutoff(sample_rate: int, bitrate: int | None, codec: str) -> int | None:
        """Estimate audio bandwidth cutoff from codec info.

        Returns estimated cutoff frequency in Hz, or None if unknown.
        """
        nyquist = sample_rate // 2

        if not bitrate:
            # No bitrate info — assume ~75% of Nyquist for lossy
            if codec and codec.lower() in ("opus", "vorbis", "flac", "alac", "pcm"):
                return nyquist
            return int(nyquist * 0.75)

        codec_lower = (codec or "").lower()

        if codec_lower in ("flac", "alac", "pcm", "pcm_s16le", "pcm_s24le"):
            return nyquist

        if codec_lower == "opus":
            if bitrate >= 128:
                return min(nyquist, 20000)
            elif bitrate >= 64:
                return min(nyquist, 18000)
            else:
                return min(nyquist, 12000)

        # AAC, MP3, Vorbis and other lossy codecs
        if bitrate >= 256:
            return min(nyquist, 20000)
        elif bitrate >= 192:
            return min(nyquist, 18000)
        elif bitrate >= 128:
            return min(nyquist, 16000)
        elif bitrate >= 96:
            return min(nyquist, 14000)
        elif bitrate >= 64:
            return min(nyquist, 12000)
        else:
            return min(nyquist, 8000)
