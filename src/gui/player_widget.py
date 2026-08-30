import sys
import locale
import logging
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QMetaObject, Q_ARG
import mpv

logger = logging.getLogger(__name__)


class MpvPlayerWidget(QWidget):
    """Widget that embeds an mpv player instance for video/audio playback."""

    position_changed = Signal(float)
    duration_changed = Signal(float)
    state_changed = Signal(str)
    file_loaded = Signal()
    end_of_file = Signal()
    seek_performed = Signal(float)  # emitted after user seeks, carries target pos
    audio_output_changed = Signal(int)  # emitted when output sample rate changes
    audio_source_detected = Signal(int)  # emitted when source sample rate is first detected
    video_output_changed = Signal(int, int, float)  # width, height, fps of actual output

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)

        self._player: mpv.MPV | None = None
        self._duration = 0.0
        self._position = 0.0
        self._file_has_played = False
        self._last_out_sr = 0
        self._last_in_sr = 0
        self._last_video_out = (0, 0, 0.0)  # (w, h, fps)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(250)
        self._poll_timer.timeout.connect(self._poll_state)

    def init_mpv(self, audio_exclusive: bool = True, audio_device: str = "auto"):
        """Initialize mpv with the given audio settings. Must be called after widget is shown."""
        wid = int(self.winId())

        self._player = mpv.MPV(
            wid=str(wid),
            vo="gpu",
            hwdec="auto-safe",
            ao="wasapi",
            audio_exclusive=("yes" if audio_exclusive else "no"),
            audio_device=audio_device if audio_device != "auto" else "auto",
            keep_open="yes",
            idle="yes",
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            log_handler=self._mpv_log,
            loglevel="warn",
        )

        self._poll_timer.start()

    def _mpv_log(self, loglevel, component, message):
        # 防御性：VapourSynth 的 VSScript 会往 root logger 挂一个有 bug 的日志桥
        # (PythonVSScriptLoggingBridge, 缺 .parent)，启用 RIFE 后任何 logging 调用
        # 经它转发都会抛异常并污染 mpv 事件循环。这里吞掉下游 handler 的异常，
        # 保证日志失败绝不反噬 mpv 事件循环。
        try:
            if loglevel in ("error", "fatal"):
                logger.error(f"[mpv/{component}] {message}")
            elif loglevel == "warn":
                logger.warning(f"[mpv/{component}] {message}")
        except Exception:
            pass

    @Slot()
    def _poll_state(self):
        """Poll mpv state from the Qt main thread — avoids cross-thread signal issues."""
        if not self._player:
            return

        try:
            idle = self._player.idle_active
            if idle:
                if self._file_has_played and self._position != 0.0:
                    self._position = 0.0
                    self._file_has_played = False
                    self.state_changed.emit("stopped")
                    self.end_of_file.emit()
                return

            duration = self._player.duration
            if duration is not None and duration != self._duration:
                self._duration = duration
                self._file_has_played = True
                self.duration_changed.emit(duration)
                self.file_loaded.emit()

            pos = self._player.time_pos
            if pos is not None:
                self._position = pos
                self.position_changed.emit(pos)

            paused = self._player.pause
            if paused:
                self.state_changed.emit("paused")
            else:
                self.state_changed.emit("playing")

            out_params = self._player.audio_out_params
            if out_params:
                sr = out_params.get("samplerate", 0)
                if sr and sr != self._last_out_sr:
                    self._last_out_sr = sr
                    self.audio_output_changed.emit(sr)

            in_params = self._player.audio_params
            if in_params:
                in_sr = in_params.get("samplerate", 0)
                if in_sr and in_sr != self._last_in_sr:
                    self._last_in_sr = in_sr
                    self.audio_source_detected.emit(in_sr)

            # Video source resolution from video-out-params
            # (GLSL shaders run on GPU and don't change this property;
            # upscale factor is tracked separately via the enhance panel)
            try:
                vo_params = self._player.video_out_params
                if vo_params:
                    vw = vo_params.get("w", 0)
                    vh = vo_params.get("h", 0)
                    vfps = self._player.container_fps or 0.0
                    current = (vw, vh, vfps)
                    if current != self._last_video_out and vw > 0:
                        self._last_video_out = current
                        self.video_output_changed.emit(vw, vh, vfps)
            except (RuntimeError, OSError, AttributeError):
                pass
        except (RuntimeError, OSError):
            pass

    def play_url(self, url: str, http_headers: dict | None = None):
        if self._player:
            self._last_in_sr = 0
            self._last_out_sr = 0
            self._player.audio_files = []
            self._set_http_headers(http_headers)
            self._player.play(url)

    def play_av(self, video_url: str, audio_url: str, http_headers: dict | None = None):
        """Play video with separate audio stream (for split format streams)."""
        if self._player:
            self._last_in_sr = 0
            self._last_out_sr = 0
            self._set_http_headers(http_headers)
            self._player.audio_files = [audio_url]
            self._player.play(video_url)

    def play_live(self, url: str, http_headers: dict | None = None):
        """Play a live stream with cache settings optimized for continuous streaming."""
        if self._player:
            self._last_in_sr = 0
            self._last_out_sr = 0
            self._player.audio_files = []
            self._set_http_headers(http_headers)
            self._player.cache = "yes"
            self._player["demuxer-max-bytes"] = "150MiB"
            self._player["demuxer-readahead-secs"] = 30
            self._player.play(url)

    def replace_live_stream(self, url: str, http_headers: dict | None = None):
        """Replace the current live stream URL without interrupting playback."""
        if self._player:
            self._set_http_headers(http_headers)
            self._player.play(url)

    def _set_http_headers(self, headers: dict | None):
        if not self._player:
            return
        if headers:
            header_list = [f"{k}: {v}" for k, v in headers.items()]
            self._player.http_header_fields = header_list
        else:
            self._player.http_header_fields = []

    def pause(self):
        if self._player:
            self._player.pause = True

    def resume(self):
        if self._player:
            self._player.pause = False

    def toggle_pause(self):
        if self._player:
            self._player.pause = not self._player.pause

    def stop(self):
        if self._player:
            self._player.stop()

    def seek(self, position: float):
        if self._player:
            self._player.seek(position, "absolute")
            self.seek_performed.emit(position)

    def set_volume(self, volume: int):
        if self._player:
            self._player.volume = volume

    def set_speed(self, speed: float):
        if self._player:
            self._player.speed = speed

    def set_hwdec_for_vf(self, need_copy: bool):
        """切换 hwdec 以适配 CPU 侧 vf 滤镜（如 vapoursynth RIFE）。

        VapourSynth 这类 vf 滤镜需要 CPU 可读帧；auto-safe 会把帧留在 GPU surface 上，
        滤镜拿不到帧。need_copy=True 切到 auto-copy（解码仍走硬件，但 copy-back 回系统内存），
        need_copy=False 恢复 auto-safe（零拷贝、低功耗）。

        注意：SVP 的 GPU 渲染（OpenCL）不需要 CPU 可读帧，所以 SVP 后端不调用本方法。
        仅 RIFE 后端需要调用。
        """
        if not self._player:
            return
        target = "auto-copy" if need_copy else "auto-safe"
        try:
            cur = self._player["hwdec"]
        except Exception:
            cur = None
        if cur == target:
            return
        try:
            # 只切换 property，不重载文件（重载会中断播放）。
            # hwdec 切换对已在播放的文件可能不会即时生效，但新加载的文件会用新设置。
            self._player["hwdec"] = target
            print(f"[hwdec] 切换为 {target} (vf need_copy={need_copy})")
        except Exception as e:
            print(f"[hwdec] 切换失败: {e}")

    def clear_video_filters(self):
        """清空 vf 滤镜链。终止 mpv 前必须先清，避免 vapoursynth+torch 在析构时原生崩溃。"""
        if self._player:
            try:
                self._player.command("vf", "set", "")
            except Exception as e:
                # debug 级别日志，可用 print
                print(f"[vf] 清 vf 失败(可忽略): {e}")

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def position(self) -> float:
        return self._position

    @property
    def is_playing(self) -> bool:
        if self._player:
            return not self._player.pause and not self._player.idle_active
        return False

    def set_audio_exclusive(self, exclusive: bool):
        if self._player:
            self._player.audio_exclusive = "yes" if exclusive else "no"

    def set_audio_device(self, device: str):
        if self._player:
            self._player.audio_device = device

    def get_audio_device_list(self) -> list[dict]:
        """Return list of available audio devices from mpv."""
        if self._player:
            devices = self._player.audio_device_list
            if devices:
                return [{"name": d["name"], "description": d["description"]} for d in devices]
        return []

    def switch_audio_file(self, audio_path: str):
        """Switch to a local audio file while keeping current video playback.

        Uses audio-add with 'select' to immediately activate the new track.
        mpv will re-open the audio output device if the sample rate differs
        (important for WASAPI exclusive mode to output at 48kHz).
        """
        if self._player:
            pos = self._position
            self._player.command("audio-add", audio_path, "select")
            self._last_in_sr = 0
            self._last_out_sr = 0
            if pos > 0.5:
                self._player.seek(pos, "absolute")

    def switch_audio_url(self, audio_url: str, http_headers: dict | None = None):
        """Switch back to a remote audio URL (for fallback to original)."""
        if self._player:
            pos = self._position
            self._set_http_headers(http_headers)
            self._player.command("audio-add", audio_url, "select")
            self._last_in_sr = 0
            self._last_out_sr = 0
            if pos > 0.5:
                self._player.seek(pos, "absolute")

    def get_audio_position(self) -> float | None:
        """Get current audio playback position (may differ from video when using external audio)."""
        if self._player:
            try:
                return self._player.audio_pts
            except (RuntimeError, OSError, AttributeError):
                return self._position
        return None

    def get_audio_output_params(self) -> dict | None:
        """Get actual audio output parameters from mpv (what's being sent to the device).

        Returns dict with 'samplerate', 'channel-count', 'format' or None if unavailable.
        Uses audio-out-params which reflects the real device output after any resampling.
        """
        if self._player:
            try:
                params = self._player.audio_out_params
                if params:
                    return {
                        "samplerate": params.get("samplerate"),
                        "channel-count": params.get("channel-count"),
                        "format": params.get("format"),
                    }
            except (RuntimeError, OSError, AttributeError):
                pass
        return None

    def get_audio_input_params(self) -> dict | None:
        """Get audio decoder output parameters (source audio format before resampling).

        Returns dict with 'samplerate', 'channel-count', 'format' or None if unavailable.
        """
        if self._player:
            try:
                params = self._player.audio_params
                if params:
                    return {
                        "samplerate": params.get("samplerate"),
                        "channel-count": params.get("channel-count"),
                        "format": params.get("format"),
                    }
            except (RuntimeError, OSError, AttributeError):
                pass
        return None

    def get_estimated_vf_fps(self) -> float | None:
        """读取 mpv 的 estimated-vf-fps（vf 链输出端的实测帧率）。

        SVP/RIFE 真补帧生效后，该值会升到源帧率的倍数（如 30→~60）；vf 仅注入但
        尚未真正产出补帧时仍约等于源帧率。供状态栏判定“黄(启动中)→绿(生效)”。
        用属性式访问，避免 p["..."] 命中 options/ 前缀报错（见帧生成踩坑笔记）。
        """
        if not self._player:
            return None
        try:
            return self._player.estimated_vf_fps
        except (RuntimeError, OSError, AttributeError):
            return None

    def get_container_fps(self) -> float | None:
        """读取源容器帧率 container-fps（属性式）。"""
        if not self._player:
            return None
        try:
            return self._player.container_fps
        except (RuntimeError, OSError, AttributeError):
            return None

    def destroy(self):
        self._poll_timer.stop()
        if self._player:
            # 先清 vf：vapoursynth+torch 滤镜若残留，terminate 时会原生崩溃 (0xe24c4a02)
            self.clear_video_filters()
            self._player.terminate()
            self._player = None

    def load_subtitle(self, path: str):
        """Load an external SRT subtitle file and activate it."""
        if self._player:
            self._player.command("sub-add", path, "select")

    def remove_subtitle(self):
        """Remove all external subtitle tracks."""
        if self._player:
            try:
                self._player.command("sub-remove")
            except (RuntimeError, OSError):
                pass
