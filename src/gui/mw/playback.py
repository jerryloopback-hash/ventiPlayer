"""MainWindow 播放控制 mixin：URL 解析与流接入、transport 控制、全屏、
直播重连、播放列表导航、B 站信息/推荐/浏览接线、帧生成(伪插帧/小黄鸭)。

从 main_window.py 拆出；self 即 MainWindow 实例。
"""

import logging
import re
import threading

from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtWidgets import QMessageBox

from src.core.playlist import VideoItem
from src.core.stream import StreamInfo
from src.core.bilibili_api import BiliVideoInfo

logger = logging.getLogger(__name__)


class PlaybackMixin:
    @Slot()
    def _on_play(self):
        url = self._url_input.text().strip()
        if not url:
            return
        self._status_label.setText("解析中...")
        self._play_btn.setEnabled(False)
        self._settings.set("last_url", url)
        self._resolver.resolve_async(url, lambda result: self._stream_resolved.emit(result))

    @Slot(object)
    def _handle_stream_resolved(self, result):
        self._play_btn.setEnabled(True)
        if isinstance(result, Exception):
            self._status_label.setText(f"解析失败: {result}")
            QMessageBox.warning(self, "错误", f"无法解析 URL:\n{result}")
            return

        stream: StreamInfo = result
        self._current_stream = stream
        self.setWindowTitle(f"VentiPlayer — {stream.title}")

        # Track live state
        self._is_live = stream.is_live
        if self._is_live:
            self._live_url = self._url_input.text().strip()

        # Subtitle button visibility: hide for live, show for video
        self._subtitle_btn.setVisible(not self._is_live)
        self._subtitle_lang_combo.setVisible(not self._is_live)
        if not self._is_live:
            self._update_subtitle_btn_state()

        # Playlist and history — skip playlist for live, still record in history
        if not self._is_live:
            if stream.url and not self._playlist.contains_url(stream.url):
                source_type = self._detect_source_type(stream.url)
                item = VideoItem(
                    bvid=stream.url.split("/")[-1] if source_type == "bilibili" else "",
                    title=stream.title,
                    duration=stream.duration,
                    thumbnail_url=stream.thumbnail or "",
                    source_type=source_type,
                    url=stream.url,
                )
                self._playlist.add(item)
                self._playlist.set_current(len(self._playlist) - 1)

        if stream.url:
            source_type = self._detect_source_type(stream.url)
            history_item = VideoItem(
                bvid=stream.url.split("/")[-1] if source_type == "bilibili" else "",
                title=stream.title,
                duration=stream.duration,
                thumbnail_url=stream.thumbnail or "",
                source_type=source_type,
                url=stream.url,
            )
            self._history_mgr.add(history_item)

        self._output_sr = 0
        self._enhanced_duration_s = 0.0
        self._video_out_w = 0
        self._video_out_h = 0
        self._video_out_fps = 0.0
        self._update_media_info()

        if stream.cookie_failed:
            self._status_label.setText("Cookie 读取失败 — 点击\"导入\"或\"自动\"按钮配置")

        if self._is_live:
            # Live stream: use live-optimized playback, start immediately
            stream_url = stream.video_url or stream.audio_url
            self._player_widget.play_live(stream_url, stream.http_headers)
            self._seek_slider.setEnabled(False)
            self._dur_label.setText("LIVE")
            self._enhance_panel.set_enhance_blocked(True)
            if not stream.cookie_failed:
                self._status_label.setText("🔴 直播中")
            # Start periodic stream refresh
            self._live_reconnect_attempts = 0
            self._live_refresh_timer.start()
        else:
            # Normal video playback
            self._seek_slider.setEnabled(True)
            self._live_refresh_timer.stop()

            if stream.video_url and stream.audio_url and stream.video_url != stream.audio_url:
                self._player_widget.play_av(stream.video_url, stream.audio_url, stream.http_headers)
            else:
                self._player_widget.play_url(stream.video_url or stream.audio_url, stream.http_headers)

            # Load in paused state so user can enable enhancement before playback
            self._player_widget.pause()

            if not stream.cookie_failed:
                self._status_label.setText("已解析 — 按播放开始")

            # Store original audio URL for sync fallback
            original_audio = stream.audio_url or stream.video_url
            self._sync.set_original_audio(original_audio)
            self._enhanced_playing = False

            # Disable enhancement if source is lossless >= 48kHz
            _lossless_codecs = {"flac", "alac", "pcm", "wav", "pcm_s16le", "pcm_s24le", "pcm_f32le"}
            sr = stream.audio_sample_rate or 0
            codec = (stream.audio_codec or "").lower()
            is_lossless_hires = sr >= 48000 and codec in _lossless_codecs
            if is_lossless_hires:
                self._enhance_panel.set_enhance_blocked(True)
            else:
                self._enhance_panel.set_enhance_blocked(False)
                self._enhance_panel.set_enhance_enabled(True)

        # Fetch Bilibili video info and recommendations in background
        if stream.url and "bilibili" in stream.url and not self._is_live:
            self._fetch_bili_info(stream.url)

    @Slot()
    def _on_stop(self):
        self._player_widget.stop()
        self._status_label.setText("已停止")
        self._seek_slider.setValue(0)
        self._seek_slider.setEnabled(True)
        self._pos_label.setText("00:00")
        self._dur_label.setText("00:00")
        self._current_stream = None
        self._output_sr = 0
        self._enhanced_duration_s = 0.0
        self._video_out_w = 0
        self._video_out_h = 0
        self._video_out_fps = 0.0
        self._enhanced_playing = False
        self._enhanced_scheme_label = ""
        self._upscale_actually_active = False
        self._media_info_label.setText("")
        self._audio_source_indicator.setText("")
        self._upscale_indicator.setText("")
        self._framegen_indicator.setText("")
        self._exclusive_indicator.setText("")
        self._enhance_panel.set_enhance_enabled(False)
        # Reset live state
        self._is_live = False
        self._live_url = ""
        self._live_refresh_timer.stop()
        self._live_reconnect_attempts = 0

    @Slot(int)
    def _on_audio_output_changed(self, sr: int):
        """Called when mpv's actual output sample rate changes."""
        self._output_sr = sr
        self._update_media_info()

    @Slot(int)
    def _on_audio_source_detected(self, sr: int):
        """Called when mpv detects the source audio sample rate (from decoder)."""
        if self._current_stream and not self._current_stream.audio_sample_rate:
            self._current_stream.audio_sample_rate = sr
            self._update_media_info()

    @Slot(int, int, float)
    def _on_video_output_changed(self, width: int, height: int, fps: float):
        """Called when mpv's actual video output resolution changes (after shaders)."""
        self._video_out_w = width
        self._video_out_h = height
        self._video_out_fps = fps
        self._update_media_info()

    @Slot(int)
    def _on_upscale_factor_changed(self, factor: int):
        """Called when the upscale shader factor changes (1=off, 2=x2).

        This tracks the *intended* factor for the media info resolution display
        (e.g. V-1920x1080 -> 3840x2160). The actual indicator color is driven by
        _check_upscale_active() which verifies shaders are really loaded in mpv.
        """
        self._upscale_factor = factor
        self._update_media_info()

    @Slot(bool, dict)
    def _on_frame_gen_changed(self, enabled: bool, params: dict):
        """帧生成总入口：按 backend 分流到 display-resample / 小黄鸭。

        - display-resample：走 mpv property（零回归旧伪插帧行为）。
        - lossless-scaling：外部小黄鸭程序，懒启动 + 全屏快捷键驱动，不接入 mpv vf 链。
        - 关闭/切换前先复位伪插帧 property，并停掉小黄鸭缩放（进程保持常驻）。
        """
        player = self._player_widget._player
        if not player:
            return
        backend = params.get("backend", "display-resample") if enabled else "off"

        if not enabled or backend == "off":
            self._teardown_frame_gen(player)
            self._stop_ls_if_selected()
            self._framegen_state = {"backend": "off", "multiplier": 1.0,
                                    "target_fps": 0, "applied": False}
            self._update_media_info()
            return

        if backend == "display-resample":
            self._stop_ls_if_selected()
            self._apply_display_resample(player, params)
        elif backend == "lossless-scaling":
            self._enable_lossless_scaling(params)
        self._update_media_info()

    def _stop_ls_if_selected(self):
        """切走/关闭帧生成时：若之前选中小黄鸭，停掉缩放（进程保持常驻）。"""
        if self._ls_backend_selected:
            self._ls_controller.stop_scaling()
            self._ls_backend_selected = False

    def _apply_display_resample(self, player, params: dict):
        """旧伪插帧：display-resample + interpolation=yes + tscale + threshold。"""
        try:
            tscale = params.get("tscale", "oversample")
            threshold = params.get("threshold", -1)
            player["video-sync"] = "display-resample"
            player["interpolation"] = "yes"
            player["tscale"] = tscale
            if threshold == -1:
                player["interpolation-threshold"] = -1
            else:
                player["interpolation-threshold"] = threshold / 10.0
            self._framegen_state = {"backend": "display-resample", "multiplier": 1.0,
                                    "target_fps": 0, "applied": True}
        except (RuntimeError, OSError) as e:
            logger.warning("帧生成: 设置 display-resample 失败: %s", e)
            self._framegen_state = {"backend": "display-resample", "multiplier": 1.0,
                                    "target_fps": 0, "applied": False}

    def _enable_lossless_scaling(self, params: dict):
        """启用小黄鸭后端：懒启动外部程序；进入全屏后由快捷键驱动开启缩放。

        小黄鸭是外部叠加程序，不接入 mpv vf，也不是 mpv interpolation，
        故先复位 video-sync=audio + interpolation=no。
        """
        player = self._player_widget._player
        if not self._ls_controller.is_configured() or not self._ls_controller.exe_exists():
            QMessageBox.information(self, "小黄鸭", "请先在设置中配置 Lossless Scaling 路径")
            # 回退到关闭状态（面板会因 caps 不可用回落到 display-resample）
            self._framegen_state = {"backend": "off", "multiplier": 1.0,
                                    "target_fps": 0, "applied": False}
            return
        # 复位 mpv 伪插帧 property（小黄鸭不是 mpv interpolation）
        if player:
            try:
                player["interpolation"] = "no"
                player["video-sync"] = "audio"
            except (RuntimeError, OSError):
                pass
        self._ls_controller.launch()
        self._ls_backend_selected = True
        # LS 启动后窗口会盖在播放器之上（且若以管理员运行还有 UAC 框）。给它点时间
        # 建好窗口，再尽力最小化 LS（仅 LS 非提权时有效）并把播放器拉回前台。
        QTimer.singleShot(800, self._tame_ls_window)
        self._framegen_state = {"backend": "lossless-scaling", "multiplier": 1.0,
                                "target_fps": 0, "applied": True}
        # 若已处于全屏，立即开启缩放
        if getattr(self, "_is_fullscreen", False):
            self._ls_controller.start_scaling()

    def _tame_ls_window(self):
        """LS 启动后：尽力最小化其窗口，并把 VentiPlayer 拉回前台。

        最小化仅在 LS 非提权时有效（UIPI 限制，见 controller.minimize_window）；
        无论是否成功，都把播放器窗口提到前台，确保用户视线回到播放器。
        """
        try:
            self._ls_controller.minimize_window()
        except Exception:
            pass
        try:
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def _teardown_frame_gen(self, player):
        """关闭帧生成：仅复位伪插帧 property（不再有 vf 路径）。"""
        try:
            player["video-sync"] = "audio"
            player["interpolation"] = "no"
        except (RuntimeError, OSError) as e:
            logger.debug("帧生成: 复位 video-sync 失败(可忽略): %s", e)

    @Slot()
    def _toggle_pause(self):
        self._player_widget.toggle_pause()

    @Slot()
    def _cycle_speed(self):
        self._speed_index = (self._speed_index + 1) % len(self._speed_options)
        speed = self._speed_options[self._speed_index]
        label = f"{speed}x" if speed != int(speed) else f"{int(speed)}x"
        self._speed_btn.setText(label)
        self._player_widget.set_speed(speed)
        self._sync.notify_speed_change(speed)

    @Slot()
    def _cycle_play_mode(self):
        self._play_mode_index = (self._play_mode_index + 1) % len(self._play_modes)
        mode, label, tooltip = self._play_modes[self._play_mode_index]
        self._mode_btn.setText(label)
        self._mode_btn.setToolTip(tooltip)
        self._playlist.set_mode(mode)

    @Slot()
    def _toggle_fullscreen(self):
        if self._is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        self._was_maximized = self.isMaximized()
        self._is_fullscreen = True
        self._url_bar.hide()
        self._right_tabs.hide()
        self.statusBar().hide()
        self.showFullScreen()
        self._fs_start_autohide()
        # 选中小黄鸭后端时，进入全屏后延迟发送快捷键开启缩放（等全屏画面稳定）
        if self._ls_backend_selected and self._ls_controller.is_configured():
            QTimer.singleShot(400, self._ls_controller.start_scaling)

    @Slot()
    def _exit_fullscreen(self):
        if not self._is_fullscreen:
            return
        # 退出全屏前先关闭小黄鸭缩放（小黄鸭需全屏画面，退出即关）
        if self._ls_controller.is_scaling:
            self._ls_controller.stop_scaling()
        self._is_fullscreen = False
        self._fs_stop_autohide()
        self._url_bar.show()
        self._right_tabs.show()
        self.statusBar().show()
        self._transport_bar.show()  # 退出全屏务必恢复控制栏
        self._fs_restore_cursor()
        if self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    # --- 全屏悬浮控制栏：鼠标静止隐藏，移动唤出 ---

    def _fs_start_autohide(self):
        """进入全屏时启动自动隐藏。用轮询光标位置而非事件过滤——mpv 嵌入的原生子窗口
        会吞掉鼠标事件，轮询 QCursor.pos() 才能可靠感知“视频区域上的鼠标移动”。"""
        from PySide6.QtGui import QCursor
        if not hasattr(self, "_fs_cursor_timer"):
            self._fs_cursor_timer = QTimer(self)
            self._fs_cursor_timer.setInterval(400)
            self._fs_cursor_timer.timeout.connect(self._fs_tick)
        self._fs_last_pos = QCursor.pos()
        self._fs_idle_ms = 0
        self._transport_bar.show()
        self._fs_cursor_timer.start()

    def _fs_stop_autohide(self):
        if hasattr(self, "_fs_cursor_timer"):
            self._fs_cursor_timer.stop()

    @Slot()
    def _fs_tick(self):
        if not self._is_fullscreen:
            self._fs_cursor_timer.stop()
            return
        from PySide6.QtGui import QCursor
        pos = QCursor.pos()
        if pos != self._fs_last_pos:
            # 鼠标移动了：唤出控制栏 + 恢复光标，重置静止计时
            self._fs_last_pos = pos
            self._fs_idle_ms = 0
            if not self._transport_bar.isVisible():
                self._transport_bar.show()
            self._fs_restore_cursor()
        else:
            self._fs_idle_ms += self._fs_cursor_timer.interval()
            if self._fs_idle_ms >= 2400 and self._transport_bar.isVisible():
                # 静止 2.4s：隐藏控制栏并隐藏光标
                self._transport_bar.hide()
                self.setCursor(Qt.CursorShape.BlankCursor)
                self._player_widget.setCursor(Qt.CursorShape.BlankCursor)

    def _fs_restore_cursor(self):
        self.unsetCursor()
        self._player_widget.unsetCursor()

    def _seek_relative(self, seconds: float):
        if self._player_widget.duration > 0:
            target = max(0, self._player_widget.position + seconds)
            self._player_widget.seek(target)

    @Slot(int)
    def _on_volume_changed(self, value: int):
        self._player_widget.set_volume(value)
        self._settings.set("volume", value)

    @Slot()
    def _on_seek(self):
        if self._player_widget.duration > 0:
            ratio = self._seek_slider.value() / 1000.0
            target = ratio * self._player_widget.duration
            self._player_widget.seek(target)

    @Slot(int)
    def _on_device_changed(self, index: int):
        device = self._device_combo.itemData(index)
        if device:
            self._player_widget.set_audio_device(device)
            self._settings.set("audio_device", device)

    @Slot(bool)
    def _on_exclusive_changed(self, checked: bool):
        self._settings.set("audio_exclusive", checked)
        self._player_widget.set_audio_exclusive(checked)

    @Slot(float)
    def _update_position(self, pos: float):
        self._pos_label.setText(self._format_time(pos))
        if self._player_widget.duration > 0 and not self._seek_slider.isSliderDown():
            ratio = int(pos / self._player_widget.duration * 1000)
            self._seek_slider.setValue(ratio)
            # Update playback marker on enhance progress bar
            if self._progress_visible():
                self._enhance_panel.update_playback_marker(
                    pos / self._player_widget.duration
                )
        # 注：增强结果是整轨 WAV（覆盖全曲），无需轮询"接近增强边界就回退"。
        # 该机制是早期流式增量设计的遗留，且会在重新增强期间误杀正在播放的
        # 旧增强音频。越界防护由 _on_seek_performed 的 seek 检查承担。

    @Slot(float)
    def _update_duration(self, dur: float):
        if not self._is_live:
            self._dur_label.setText(self._format_time(dur))

    def _progress_visible(self) -> bool:
        return self._enhance_panel._progress.isVisible()

    @Slot(str)
    def _update_state(self, state: str):
        if state == self._last_state:
            return
        self._last_state = state
        state_map = {
            "playing": "播放中",
            "paused": "已暂停",
            "stopped": "已停止",
            "buffering": "缓冲中...",
        }
        self._status_label.setText(state_map.get(state, state))
        if state == "playing":
            self._pause_btn.setText("⏸︎")
            # Notify sync manager that playback resumed — suppress drift checks briefly
            self._sync.notify_resume()
        elif state == "paused":
            self._pause_btn.setText("▶︎")

    @staticmethod
    def _format_time(seconds: float) -> str:
        s = int(seconds)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    @Slot()
    def _on_end_of_file(self):
        """Handle end-of-file: for live streams attempt reconnect, otherwise play next."""
        if self._is_live:
            self._live_reconnect_attempts += 1
            if self._live_reconnect_attempts <= 3:
                self._status_label.setText("直播流中断，正在重连...")
                self._on_live_refresh()
            else:
                self._status_label.setText("直播已结束")
                self._is_live = False
                self._live_refresh_timer.stop()
                self._seek_slider.setEnabled(True)
                self._dur_label.setText("00:00")
        else:
            self._play_next()

    def _on_live_refresh(self):
        """Trigger a background re-resolve of the live stream URL."""
        if not self._is_live or not self._live_url:
            return
        self._resolver.resolve_live_refresh(
            self._live_url,
            lambda result: self._live_refresh_ready.emit(result),
        )

    @Slot(object)
    def _handle_live_refresh(self, result):
        """Handle the result of a live stream URL refresh."""
        if result is None or not isinstance(result, StreamInfo):
            if self._live_reconnect_attempts > 0:
                self._status_label.setText("直播已结束或无法重连")
                self._is_live = False
                self._live_refresh_timer.stop()
                self._seek_slider.setEnabled(True)
                self._dur_label.setText("00:00")
            return

        stream: StreamInfo = result
        self._current_stream = stream
        stream_url = stream.video_url or stream.audio_url
        self._player_widget.replace_live_stream(stream_url, stream.http_headers)
        self._live_reconnect_attempts = 0
        self._status_label.setText("🔴 直播中")

    @Slot()
    def _play_next(self):
        if self._url_input.hasFocus():
            return
        item = self._playlist.next()
        if item:
            self._play_playlist_item(item)
        elif self._current_recommendations:
            # Auto-play from recommendations when queue is exhausted
            rec = self._current_recommendations.pop(0)
            url = f"https://www.bilibili.com/video/{rec.bvid}"
            video_item = VideoItem(
                bvid=rec.bvid,
                title=rec.title,
                duration=rec.duration,
                thumbnail_url=rec.thumbnail,
                source_type="bilibili",
                url=url,
            )
            self._playlist.add(video_item)
            self._playlist.set_current(len(self._playlist) - 1)
            self._play_playlist_item(video_item)
            # Update recommendations display
            self._playlist_panel.set_recommendations(self._current_recommendations)

    @Slot()
    def _play_prev(self):
        if self._url_input.hasFocus():
            return
        item = self._playlist.prev()
        if item:
            self._play_playlist_item(item)

    @Slot(int)
    def _on_playlist_jump(self, index: int):
        self._playlist.set_current(index)
        item = self._playlist.current()
        if item:
            self._play_playlist_item(item)

    def _play_playlist_item(self, item: VideoItem):
        self._url_input.setText(item.url)
        self._status_label.setText("解析中...")
        self._play_btn.setEnabled(False)
        self._resolver.resolve_async(item.url, lambda result: self._stream_resolved.emit(result))

    # --- Bilibili API integration ---

    def _extract_bvid(self, url: str) -> str:
        """Extract BV ID from a Bilibili URL."""
        match = re.search(r'(BV[A-Za-z0-9]+)', url)
        return match.group(1) if match else ""

    def _fetch_bili_info(self, url: str):
        """Fetch video info and related videos in a background thread."""
        bvid = self._extract_bvid(url)
        if not bvid:
            return

        def _worker():
            try:
                info = self._bili_api.get_video_info(bvid)
                if info:
                    self._bili_info_ready.emit(info)
            except Exception as e:
                logger.debug("Failed to fetch bili video info: %s", e)

            try:
                related = self._bili_api.get_related_videos(bvid)
                if related:
                    self._bili_related_ready.emit(related)
            except Exception as e:
                logger.debug("Failed to fetch bili related videos: %s", e)

        threading.Thread(target=_worker, daemon=True).start()

    def _fetch_homepage_recommendations(self):
        """Fetch B站 popular/recommended videos on startup for the playlist panel."""
        def _worker():
            try:
                items = self._bili_api.get_popular()
                if items:
                    self._bili_related_ready.emit(items)
            except Exception as e:
                logger.debug("Failed to fetch homepage recommendations: %s", e)

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(object)
    def _on_bili_info_ready(self, info: BiliVideoInfo):
        """Handle video info arrival — show season prompt if applicable."""
        if info and info.season_id and info.season_title:
            self._playlist_panel.show_season_prompt(
                info.season_title,
                lambda: self._load_season(info.owner_mid, info.season_id),
            )

    @Slot(object)
    def _on_bili_related_ready(self, items):
        """Handle related videos arrival — store and display recommendations."""
        if isinstance(items, tuple) and len(items) == 2:
            msg_type, data = items
            if msg_type == "season":
                # Season videos loaded — set as the source playlist
                video_items = []
                for v in data:
                    url = f"https://www.bilibili.com/video/{v.bvid}"
                    video_items.append(VideoItem(
                        bvid=v.bvid,
                        title=v.title,
                        duration=v.duration,
                        thumbnail_url=v.thumbnail,
                        source_type="bilibili",
                        url=url,
                    ))
                current_url = self._url_input.text().strip()
                self._playlist.set_playlist(video_items, current_url=current_url)
                self._status_label.setText(f"已加载合集 ({len(data)} 个视频)")
                return

        # Regular related videos
        if isinstance(items, list):
            self._current_recommendations = list(items)
            self._playlist_panel.set_recommendations(items)
            self._content_browser.set_recommendations(items)

    def _load_season(self, mid: int, season_id: int):
        """Load all videos from a season into the playlist."""
        def _worker():
            try:
                videos = self._bili_api.get_season_videos(mid, season_id)
                if videos:
                    self._bili_related_ready.emit(("season", videos))
            except Exception as e:
                logger.debug("Failed to load season: %s", e)

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(str)
    def _on_recommendation_clicked(self, bvid: str):
        """Handle double-click on a recommendation — set recommendations as playlist and play."""
        if not bvid:
            return
        url = f"https://www.bilibili.com/video/{bvid}"

        # Build playlist from all current recommendations
        video_items = []
        for r in self._current_recommendations:
            r_url = f"https://www.bilibili.com/video/{r.bvid}"
            video_items.append(VideoItem(
                bvid=r.bvid,
                title=r.title,
                duration=r.duration,
                thumbnail_url=r.thumbnail,
                source_type="bilibili",
                url=r_url,
            ))

        if video_items:
            self._playlist.set_playlist(video_items, current_url=url)
        else:
            # Fallback: just add the single item
            rec_item = None
            for r in self._current_recommendations:
                if r.bvid == bvid:
                    rec_item = r
                    break
            video_item = VideoItem(
                bvid=bvid,
                title=rec_item.title if rec_item else bvid,
                duration=rec_item.duration if rec_item else 0,
                thumbnail_url=rec_item.thumbnail if rec_item else "",
                source_type="bilibili",
                url=url,
            )
            self._playlist.add(video_item)
            self._playlist.set_current(len(self._playlist) - 1)

        # Play the selected item
        item = self._playlist.current()
        if item:
            self._play_playlist_item(item)

    # --- Content browser handlers ---

    @Slot(str)
    def _on_browser_play(self, url: str):
        """Play a video from the content browser (without context — single video playlist)."""
        self._url_input.setText(url)
        self._on_play()

    @Slot(str, list)
    def _on_browser_play_with_context(self, url: str, siblings: list):
        """Play a video from the content browser with source context.

        Sets the playlist to the full list of sibling videos from the source tab.
        """
        if siblings:
            # Convert BiliVideoItem list to VideoItem list for the playlist
            video_items = []
            for v in siblings:
                v_url = f"https://www.bilibili.com/video/{v.bvid}"
                video_items.append(VideoItem(
                    bvid=v.bvid,
                    title=v.title,
                    duration=v.duration,
                    thumbnail_url=v.thumbnail if hasattr(v, 'thumbnail') else "",
                    source_type="bilibili",
                    url=v_url,
                ))
            self._playlist.set_playlist(video_items, current_url=url)
        # The actual play is triggered by play_video signal -> _on_browser_play

    @Slot(str)
    def _on_history_play(self, url: str):
        """Play a video from history — creates a single-item playlist."""
        # Set playlist to just this one video
        # (history play doesn't have source context)
        self._url_input.setText(url)
        self._on_play()

    @Slot(str, str)
    def _on_browser_add_queue(self, url: str, title: str):
        """Add a video to queue without playing."""
        bvid = ""
        if "bilibili" in url:
            parts = url.rstrip("/").split("/")
            bvid = parts[-1] if parts else ""
        item = VideoItem(
            bvid=bvid,
            title=title,
            duration=None,
            thumbnail_url="",
            source_type="bilibili",
            url=url,
        )
        self._playlist.add(item)

    # --- Resource monitoring ---
