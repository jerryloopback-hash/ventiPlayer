import sys
import re
import logging
import threading
from pathlib import Path
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QSlider, QLabel, QComboBox,
    QCheckBox, QStatusBar, QSplitter, QMessageBox, QFileDialog,
    QApplication, QTabWidget, QProgressDialog,
)
from PySide6.QtCore import Qt, Slot, QTimer, Signal, QEvent
from PySide6.QtGui import QKeySequence, QShortcut

from src.gui.player_widget import MpvPlayerWidget
from src.gui.enhance_panel import EnhancePanel
from src.gui.video_enhance_panel import VideoEnhancePanel
from src.gui.playlist_panel import PlaylistPanel
from src.gui.content_browser import ContentBrowser
from src.gui.settings_dialog import SettingsDialog
from src.gui.thumbnail_cache import ThumbnailCache
from src.core.stream import (
    StreamResolver, StreamInfo, CookieStatus,
    check_cookie_status,
)
from src.core.playlist import PlaylistManager, VideoItem, PlayMode, HistoryManager
from src.core.bilibili_api import BilibiliAPI, BiliVideoInfo, BiliVideoItem
from src.core.enhancer import Enhancer, Backend
from src.core.audio_pipe import AudioPipeline, PipelineState, PipelineStatus
from src.core.video_export import VideoExporter, ExportSettings, ExportResult
from src.core.sync import SyncManager, SyncState, SyncStatus
from src.core.frame_gen import FrameGenManager
from src.core.lossless_scaling import LosslessScalingController
from src.core.resource_monitor import ResourceMonitor
from src.core.subtitle import SubtitlePipeline, SubtitleStatus, extract_video_id
from src.core.llm import LLMProvider, provider_from_dict
from src.config.settings import Settings

logger = logging.getLogger(__name__)

# Regex for matching YouTube / Bilibili URLs in clipboard
_CLIPBOARD_URL_RE = re.compile(
    r'https?://(?:'
    r'(?:www\.|m\.)?youtube\.com/watch\?'
    r'|youtu\.be/'
    r'|(?:www\.|m\.)?youtube\.com/(?:shorts|live|embed|v)/'
    r'|(?:www\.)?bilibili\.com/video/[ABab]'
    r'|live\.bilibili\.com/\d'
    r'|b23\.tv/'
    r'|(?:www\.)?twitch\.tv/videos/\d'
    r'|(?:www\.)?twitch\.tv/\w'
    r')',
    re.IGNORECASE,
)


class MainWindow(QMainWindow):
    _stream_resolved = Signal(object)
    _cookie_status_ready = Signal(object)
    _enhance_status_update = Signal(object)
    _subtitle_status_update = Signal(object)
    _bili_info_ready = Signal(object)
    _bili_related_ready = Signal(object)
    _live_refresh_ready = Signal(object)
    backend_ready = Signal()  # emitted when enhance panel backend info is set
    _export_progress = Signal(float, str)   # 视频导出进度（后台线程→主线程）
    _export_done = Signal(object)            # 视频导出完成，携带 ExportResult

    def __init__(self, predetected_device=None):
        super().__init__()
        self.setWindowTitle("VentiPlayer")
        self.setMinimumSize(960, 640)

        self._settings = Settings()
        self._resolver = self._create_resolver()
        self._current_stream: StreamInfo | None = None
        self._last_state: str = ""
        self._is_fullscreen = False
        self._was_maximized = False

        # Playlist
        self._playlist = PlaylistManager()
        self._history_mgr = HistoryManager()

        # Bilibili API client
        self._bili_api = BilibiliAPI()
        cookie_file = self._settings.get("cookie_file")
        if cookie_file:
            self._bili_api.set_cookies_from_file(cookie_file)
        self._current_recommendations: list[BiliVideoItem] = []

        # Enhancement engine
        self._enhancer = Enhancer()
        if predetected_device is not None:
            self._enhancer._device_info = predetected_device
        self._pipeline = AudioPipeline(self._enhancer)
        self._pipeline.set_status_callback(
            lambda s: self._enhance_status_update.emit(s)
        )

        # Sync manager
        self._sync = SyncManager()
        self._enhanced_playing = False  # True when enhanced audio is active

        # 视频导出引擎（复用共享 Enhancer，按需懒创建）
        self._exporter: VideoExporter | None = None
        self._export_dialog: QProgressDialog | None = None

        # Frame generation manager（仅做后端依赖检测：display-resample + 小黄鸭）
        self._frame_gen_mgr = FrameGenManager()
        # 小黄鸭 (Lossless Scaling) 外部全屏补帧控制器（懒启动，常驻）
        self._ls_controller = LosslessScalingController(
            self._settings.get("lossless_scaling_path") or "",
            self._settings.get("lossless_scaling_hotkey") or "ctrl+alt+s",
        )
        self._ls_backend_selected = False

        # Live stream state
        self._is_live = False
        self._live_url = ""  # original live URL for refresh
        self._live_refresh_timer = QTimer(self)
        self._live_refresh_timer.setInterval(25 * 60 * 1000)  # 25 minutes
        self._live_refresh_timer.timeout.connect(self._on_live_refresh)
        self._live_reconnect_attempts = 0

        # Subtitle pipeline
        self._subtitle_pipeline: SubtitlePipeline | None = None

        self._setup_ui()
        self._setup_shortcuts()
        self._connect_signals()

        # Apply initial thumbnail mode and size from settings
        thumb_size = self._settings.get("thumbnail_size")
        if thumb_size and thumb_size != 80:
            self._playlist_panel.set_thumbnail_size(thumb_size)
            self._content_browser.set_thumbnail_size(thumb_size)
        if self._settings.get("thumbnail_mode"):
            self._playlist_panel.set_thumbnail_mode(True)
            self._content_browser.set_thumbnail_mode(True)

        QTimer.singleShot(0, self._init_player)
        QTimer.singleShot(100, self._refresh_cookie_status)
        QTimer.singleShot(200, self._init_enhance_backend)
        QTimer.singleShot(300, self._check_clipboard_url)
        QTimer.singleShot(500, self._fetch_homepage_recommendations)

    def _create_resolver(self) -> StreamResolver:
        return StreamResolver(
            cookie_file=self._settings.get("cookie_file"),
            cookie_browser=self._settings.get("cookie_browser"),
        )

    @staticmethod
    def _detect_source_type(url: str) -> str:
        if "bilibili" in url or "b23.tv" in url:
            return "bilibili"
        if "twitch.tv" in url:
            return "twitch"
        return "youtube"

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        self._main_layout = QVBoxLayout(central)
        self._main_layout.setContentsMargins(8, 8, 8, 8)
        self._main_layout.setSpacing(6)

        # URL bar
        self._url_bar = QWidget()
        url_layout = QHBoxLayout(self._url_bar)
        url_layout.setContentsMargins(0, 0, 0, 0)
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("输入 YouTube / B站 URL...")
        self._url_input.setText(self._settings.get("last_url"))
        self._play_btn = QPushButton("解析")
        self._stop_btn = QPushButton("停止")
        self._settings_btn = QPushButton("设置")
        url_layout.addWidget(self._url_input, 1)
        url_layout.addWidget(self._play_btn)
        url_layout.addWidget(self._stop_btn)
        url_layout.addWidget(self._settings_btn)
        self._main_layout.addWidget(self._url_bar)

        # Splitter: video + panel
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Video area
        self._player_widget = MpvPlayerWidget()
        self._player_widget.setMinimumSize(480, 270)
        self._player_widget.mouseDoubleClickEvent = lambda e: self._toggle_fullscreen()
        self._splitter.addWidget(self._player_widget)

        # Right panel — Tab widget
        self._right_tabs = QTabWidget()
        self._right_tabs.setTabPosition(QTabWidget.TabPosition.North)

        # Thumbnail cache (shared between playlist and content browser)
        self._thumbnail_cache = ThumbnailCache(self)

        # Tab 0: Playlist
        playlist_tab = QWidget()
        playlist_layout = QVBoxLayout(playlist_tab)
        playlist_layout.setContentsMargins(4, 4, 4, 4)
        self._playlist_panel = PlaylistPanel(self._playlist, self._history_mgr, self._thumbnail_cache)
        playlist_layout.addWidget(self._playlist_panel)
        self._right_tabs.addTab(playlist_tab, "播放列表")

        # Tab 1: Audio
        audio_tab = QWidget()
        audio_layout = QVBoxLayout(audio_tab)
        audio_layout.setContentsMargins(4, 4, 4, 4)

        # Audio device
        dev_layout = QHBoxLayout()
        dev_layout.addWidget(QLabel("输出设备:"))
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(180)
        dev_layout.addWidget(self._device_combo, 1)
        audio_layout.addLayout(dev_layout)

        # WASAPI exclusive
        self._exclusive_check = QCheckBox("WASAPI Exclusive")
        self._exclusive_check.setChecked(self._settings.get("audio_exclusive"))
        audio_layout.addWidget(self._exclusive_check)

        # Enhance panel
        self._enhance_panel = EnhancePanel()
        audio_layout.addWidget(self._enhance_panel)

        audio_layout.addStretch()
        self._right_tabs.addTab(audio_tab, "音频")

        # Tab 2: Video
        video_tab = QWidget()
        video_layout = QVBoxLayout(video_tab)
        video_layout.setContentsMargins(4, 4, 4, 4)
        self._video_enhance_panel = VideoEnhancePanel(
            frame_gen_caps=self._frame_gen_mgr.check_dependencies(
                ls_exe_path=self._settings.get("lossless_scaling_path") or ""
            )
        )
        video_layout.addWidget(self._video_enhance_panel)
        video_layout.addStretch()
        self._right_tabs.addTab(video_tab, "视频")

        # Tab 3: Browse (Content Browser)
        self._content_browser = ContentBrowser(self._bili_api, self._thumbnail_cache)
        self._right_tabs.addTab(self._content_browser, "浏览")

        self._splitter.addWidget(self._right_tabs)
        self._splitter.setSizes([700, 260])
        self._main_layout.addWidget(self._splitter, 1)

        # Transport controls
        self._transport_bar = QWidget()
        transport_layout = QHBoxLayout(self._transport_bar)
        transport_layout.setContentsMargins(0, 0, 0, 0)

        _sym_style = "QPushButton { font-family: 'Segoe UI Symbol'; font-size: 14px; }"
        _vs15 = "︎"

        self._pause_btn = QPushButton(f"⏸{_vs15}")
        self._pause_btn.setFixedWidth(36)
        self._pause_btn.setToolTip("暂停/继续 (Space)")
        self._pause_btn.setStyleSheet(_sym_style)

        self._prev_btn = QPushButton(f"⏮{_vs15}")
        self._prev_btn.setFixedWidth(36)
        self._prev_btn.setToolTip("上一首 (P)")
        self._prev_btn.setStyleSheet(_sym_style)

        self._next_btn = QPushButton(f"⏭{_vs15}")
        self._next_btn.setFixedWidth(36)
        self._next_btn.setToolTip("下一首 (N)")
        self._next_btn.setStyleSheet(_sym_style)

        self._speed_btn = QPushButton("1x")
        self._speed_btn.setFixedWidth(40)
        self._speed_btn.setToolTip("播放倍速")
        self._speed_btn.clicked.connect(self._cycle_speed)
        self._speed_options = [0.5, 1.0, 1.25, 1.5, 2.0, 3.0]
        self._speed_index = 1  # default 1x

        # Play mode cycle button
        self._mode_btn = QPushButton("顺序")
        self._mode_btn.setFixedWidth(44)
        self._mode_btn.setToolTip("播放模式: 顺序播放")
        self._mode_btn.setStyleSheet("QPushButton { font-size: 11px; }")
        self._mode_btn.clicked.connect(self._cycle_play_mode)
        self._play_mode_index = 0
        self._play_modes = [
            (PlayMode.SEQUENTIAL, "顺序", "播放模式: 顺序播放"),
            (PlayMode.SINGLE_LOOP, "单曲", "播放模式: 单曲循环"),
            (PlayMode.LIST_LOOP, "列表", "播放模式: 列表循环"),
            (PlayMode.SHUFFLE, "随机", "播放模式: 随机播放"),
        ]

        self._fullscreen_btn = QPushButton(f"⛶{_vs15}")
        self._fullscreen_btn.setFixedWidth(36)
        self._fullscreen_btn.setToolTip("全屏 (F)")
        self._fullscreen_btn.setStyleSheet(_sym_style)

        # Subtitle controls
        self._subtitle_lang_combo = QComboBox()
        self._subtitle_lang_combo.addItems(["中文", "英文"])
        self._subtitle_lang_combo.setFixedWidth(56)
        self._subtitle_lang_combo.setToolTip("字幕语言")
        self._subtitle_btn = QPushButton("字幕")
        self._subtitle_btn.setFixedWidth(44)
        self._subtitle_btn.setToolTip("生成 AI 字幕")
        self._subtitle_btn.setStyleSheet("QPushButton { font-size: 11px; }")
        self._subtitle_btn.clicked.connect(self._on_subtitle_requested)

        self._pos_label = QLabel("00:00")
        self._pos_label.setFixedWidth(52)
        self._seek_slider = QSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 1000)
        self._dur_label = QLabel("00:00")
        self._dur_label.setFixedWidth(52)
        self._vol_label = QLabel("🔊︎")
        self._vol_label.setStyleSheet("font-family: 'Segoe UI Symbol'; font-size: 14px;")
        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 150)
        self._vol_slider.setValue(self._settings.get("volume"))
        self._vol_slider.setFixedWidth(100)

        transport_layout.addWidget(self._pause_btn)
        transport_layout.addWidget(self._prev_btn)
        transport_layout.addWidget(self._next_btn)
        transport_layout.addWidget(self._speed_btn)
        transport_layout.addWidget(self._mode_btn)
        transport_layout.addWidget(self._pos_label)
        transport_layout.addWidget(self._seek_slider, 1)
        transport_layout.addWidget(self._dur_label)
        transport_layout.addSpacing(12)
        transport_layout.addWidget(self._vol_label)
        transport_layout.addWidget(self._vol_slider)
        transport_layout.addSpacing(8)
        transport_layout.addWidget(self._fullscreen_btn)
        transport_layout.addSpacing(4)
        transport_layout.addWidget(self._subtitle_lang_combo)
        transport_layout.addWidget(self._subtitle_btn)
        self._main_layout.addWidget(self._transport_bar)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("就绪")
        self._status_bar.addWidget(self._status_label, 1)
        self._media_info_label = QLabel("")
        self._media_info_label.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        self._status_bar.addPermanentWidget(self._media_info_label)
        self._audio_source_indicator = QLabel("")
        self._audio_source_indicator.setTextFormat(Qt.TextFormat.RichText)
        self._audio_source_indicator.setStyleSheet("font-size: 11px; margin-left: 6px;")
        self._status_bar.addPermanentWidget(self._audio_source_indicator)
        self._upscale_indicator = QLabel("")
        self._upscale_indicator.setTextFormat(Qt.TextFormat.RichText)
        self._upscale_indicator.setStyleSheet("font-size: 11px; margin-left: 6px;")
        self._status_bar.addPermanentWidget(self._upscale_indicator)
        self._framegen_indicator = QLabel("")
        self._framegen_indicator.setTextFormat(Qt.TextFormat.RichText)
        self._framegen_indicator.setStyleSheet("font-size: 11px; margin-left: 6px;")
        self._status_bar.addPermanentWidget(self._framegen_indicator)
        # 独占(WASAPI Exclusive)指示器：绿点=独占 / 灰点=非独占，与其余 ● 指示器保持一致
        self._exclusive_indicator = QLabel("")
        self._exclusive_indicator.setTextFormat(Qt.TextFormat.RichText)
        self._exclusive_indicator.setStyleSheet("font-size: 11px; margin-left: 6px;")
        self._status_bar.addPermanentWidget(self._exclusive_indicator)
        self._resource_label = QLabel("")
        self._resource_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px; margin-left: 8px;"
        )
        self._status_bar.addPermanentWidget(self._resource_label)
        self._cookie_info_label = QLabel("")
        self._cookie_info_label.setStyleSheet("color: gray; margin-left: 8px;")
        self._status_bar.addPermanentWidget(self._cookie_info_label)

        # Media info state
        self._output_sr: int = 0  # actual output sample rate from mpv
        self._enhanced_duration_s: float = 0.0  # seconds of enhanced audio available
        self._enhanced_output_sr: int = 0  # SR of enhanced output (Apollo 44.1k / FlashSR 48k)
        # 当前正在播放的增强音频实际采用的方案标签，如 "Apollo(fp32)+FlashSR(fp16)"
        # 在 _on_enhance_requested 发起增强时捕获，避免被事后改动的勾选状态影响
        self._enhanced_scheme_label: str = ""
        self._video_out_w: int = 0  # actual video output width (from video-out-params)
        self._video_out_h: int = 0  # actual video output height (from video-out-params)
        self._video_out_fps: float = 0.0  # actual video output fps
        self._upscale_factor: int = 1  # 1 = no upscale, 2 = x2 shader active
        self._upscale_actually_active: bool = False  # True only when upscale shaders verified loaded
        # 帧生成状态：backend(off|display-resample|lossless-scaling) / multiplier /
        # target_fps(0=倍率模式) / applied(是否真正生效)
        self._framegen_state: dict = {
            "backend": "off",
            "multiplier": 1.0,
            "target_fps": 0,
            "applied": False,
        }

    def _setup_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self._toggle_pause)
        QShortcut(QKeySequence("Ctrl+Return"), self, self._on_play)
        QShortcut(QKeySequence(Qt.Key.Key_F), self, self._toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._exit_fullscreen)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, lambda: self._seek_relative(-5))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, lambda: self._seek_relative(5))
        QShortcut(QKeySequence(Qt.Key.Key_N), self, self._play_next)
        QShortcut(QKeySequence(Qt.Key.Key_P), self, self._play_prev)

    def _connect_signals(self):
        self._play_btn.clicked.connect(self._on_play)
        self._stop_btn.clicked.connect(self._on_stop)
        self._settings_btn.clicked.connect(self._on_open_settings)
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        self._url_input.returnPressed.connect(self._on_play)
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        self._seek_slider.sliderReleased.connect(self._on_seek)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._exclusive_check.toggled.connect(self._on_exclusive_changed)
        self._stream_resolved.connect(self._handle_stream_resolved)
        self._cookie_status_ready.connect(self._handle_cookie_status)
        # Playlist signals
        self._prev_btn.clicked.connect(self._play_prev)
        self._next_btn.clicked.connect(self._play_next)
        self._playlist_panel.item_double_clicked.connect(self._on_playlist_jump)
        self._playlist_panel.history_item_double_clicked.connect(self._on_history_play)
        self._playlist_panel.recommendation_clicked.connect(self._on_recommendation_clicked)
        self._player_widget.end_of_file.connect(self._on_end_of_file)
        # Bilibili API signals
        self._bili_info_ready.connect(self._on_bili_info_ready)
        self._bili_related_ready.connect(self._on_bili_related_ready)
        # Live stream refresh signal
        self._live_refresh_ready.connect(self._handle_live_refresh)
        # Subtitle signal
        self._subtitle_status_update.connect(self._handle_subtitle_status)
        # Enhancement signals
        self._enhance_panel.enhance_requested.connect(self._on_enhance_requested)
        self._enhance_panel.cancel_requested.connect(self._on_enhance_cancel)
        self._enhance_panel.settings_changed.connect(self._on_enhance_settings_changed)
        self._enhance_status_update.connect(self._handle_enhance_status)
        self._exclusive_check.toggled.connect(self._update_media_info)
        # Video enhance panel signals
        self._video_enhance_panel.property_changed.connect(self._on_video_property_changed)
        self._video_enhance_panel.shader_changed.connect(self._on_video_shader_changed)
        self._video_enhance_panel.deband_changed.connect(self._on_video_deband_changed)
        self._video_enhance_panel.vf_changed.connect(self._on_video_vf_changed)
        self._video_enhance_panel.hdr_changed.connect(self._on_video_hdr_changed)
        self._video_enhance_panel.upscale_factor_changed.connect(self._on_upscale_factor_changed)
        self._video_enhance_panel.frame_gen_changed.connect(self._on_frame_gen_changed)
        # Content browser signals
        self._content_browser.play_video.connect(self._on_browser_play)
        self._content_browser.play_video_with_context.connect(self._on_browser_play_with_context)
        self._content_browser.add_to_queue.connect(self._on_browser_add_queue)
        # 视频导出信号（后台线程→主线程）
        self._export_progress.connect(self._on_export_progress)
        self._export_done.connect(self._on_export_done)

    @Slot()
    def _init_player(self):
        self._player_widget.init_mpv(
            audio_exclusive=self._settings.get("audio_exclusive"),
        )
        self._player_widget.position_changed.connect(self._update_position)
        self._player_widget.duration_changed.connect(self._update_duration)
        self._player_widget.state_changed.connect(self._update_state)
        self._player_widget.seek_performed.connect(self._on_seek_performed)
        self._player_widget.audio_output_changed.connect(self._on_audio_output_changed)
        self._player_widget.audio_source_detected.connect(self._on_audio_source_detected)
        self._player_widget.video_output_changed.connect(self._on_video_output_changed)
        self._refresh_devices()
        self._configure_sync()

    def _refresh_devices(self):
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        self._device_combo.addItem("自动", "auto")
        devices = self._player_widget.get_audio_device_list()
        saved_device = self._settings.get("audio_device")
        target_index = 0
        for dev in devices:
            if dev["name"] != "auto":
                self._device_combo.addItem(dev["description"], dev["name"])
                if dev["name"] == saved_device:
                    target_index = self._device_combo.count() - 1
        self._device_combo.setCurrentIndex(target_index)
        self._device_combo.blockSignals(False)
        if target_index > 0:
            self._player_widget.set_audio_device(saved_device)

    def _refresh_cookie_status(self):
        """Check cookie status in background thread."""
        cookie_file = self._settings.get("cookie_file")
        if not cookie_file:
            self._cookie_info_label.setText("Cookie: 未配置")
            return

        def _worker():
            status = check_cookie_status(cookie_file)
            self._cookie_status_ready.emit(status)

        threading.Thread(target=_worker, daemon=True).start()

    def _configure_sync(self):
        """Wire up the sync manager to player functions."""
        self._sync.configure(
            video_position_fn=lambda: self._player_widget.position,
            audio_position_fn=lambda: self._player_widget.get_audio_position(),
            seek_fn=lambda pos: self._player_widget.seek(pos),
            switch_audio_fn=self._sync_switch_audio,
            set_speed_fn=lambda s: self._player_widget.set_speed(s),
            get_speed_fn=lambda: self._speed_options[self._speed_index],
        )

    def _sync_switch_audio(self, path_or_url: str):
        """Called by SyncManager to switch audio source."""
        if path_or_url.startswith(("http://", "https://")):
            headers = self._current_stream.http_headers if self._current_stream else None
            self._player_widget.switch_audio_url(path_or_url, headers)
            self._enhanced_playing = False
        else:
            self._player_widget.switch_audio_file(path_or_url)
            self._enhanced_playing = True
        self._update_media_info()

    @Slot(float)
    def _on_seek_performed(self, position: float):
        """Notify sync manager when user seeks."""
        self._sync.notify_seek(position)
        # If enhanced audio is active but seek goes past coverage, fall back
        if self._enhanced_playing and self._enhanced_duration_s > 0:
            if position > self._enhanced_duration_s - 2.0:
                self._sync.deactivate_enhanced()
                self._enhanced_playing = False
                self._update_media_info()
                self._status_label.setText("播放位置超出已增强范围 — 回退到源音频")

    @Slot(object)
    def _handle_cookie_status(self, status: CookieStatus):
        if status.platform == "bilibili":
            if status.is_vip:
                text = f"B站: {status.username} (大会员)"
                self._cookie_info_label.setStyleSheet("color: #fb7299;")
            elif status.logged_in:
                text = f"B站: {status.username} (普通)"
                self._cookie_info_label.setStyleSheet("color: orange;")
            else:
                text = "B站: 未登录"
                self._cookie_info_label.setStyleSheet("color: gray;")
        elif status.platform == "youtube":
            text = "YouTube: 已登录"
            self._cookie_info_label.setStyleSheet("color: #ff0000;")
        else:
            text = "Cookie: 未识别"
            self._cookie_info_label.setStyleSheet("color: gray;")
        self._cookie_info_label.setText(text)

    @Slot()
    def _on_open_settings(self):
        """Open the settings dialog."""
        dlg = SettingsDialog(self._settings, self)
        dlg.cookie_imported.connect(self._on_cookie_imported_from_settings)
        dlg.thumbnail_mode_changed.connect(self._on_thumbnail_mode_changed)
        dlg.thumbnail_size_changed.connect(self._on_thumbnail_size_changed)
        dlg.llm_config_changed.connect(self._on_llm_config_changed)
        dlg.lossless_scaling_changed.connect(self._on_ls_settings_changed)
        dlg.export_video_requested.connect(self._on_export_video_requested)
        dlg.exec()

    @Slot()
    def _on_ls_settings_changed(self):
        """设置中小黄鸭路径/快捷键变更：更新控制器配置并刷新面板后端可选项。"""
        path = self._settings.get("lossless_scaling_path") or ""
        hotkey = self._settings.get("lossless_scaling_hotkey") or "ctrl+alt+s"
        self._ls_controller.update_config(path, hotkey)
        caps = self._frame_gen_mgr.check_dependencies(ls_exe_path=path)
        self._video_enhance_panel.refresh_caps(caps)

    # ─── 视频导出 ────────────────────────────────────────────────────────

    @Slot()
    def _on_export_video_requested(self):
        """设置面板「导出当前视频为 MP4」：门控→选保存路径→后台烘焙导出。

        导出文件会真实套用当前面板上的音频(Apollo/FlashSR)与画面(超分/锐化/去色带/
        降噪/HDR 等)增强；插帧不烘焙(伪插帧是显示期特性，小黄鸭是外部叠加)。
        """
        stream = self._current_stream
        if stream is None or stream.is_live:
            QMessageBox.warning(self, "提示", "请先解析一个非直播视频")
            return
        if self._exporter is not None:
            QMessageBox.information(self, "提示", "已有导出任务在进行中")
            return

        last_dir = self._settings.get("export_last_dir") or ""
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", stream.title or "video")
        default_path = str(Path(last_dir) / f"{safe_title}.mp4") if last_dir else f"{safe_title}.mp4"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出为 MP4", default_path, "MP4 视频 (*.mp4)")
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

        # 进度对话框（可取消）
        self._export_dialog = QProgressDialog("准备导出...", "取消", 0, 100, self)
        self._export_dialog.setWindowTitle("导出视频")
        self._export_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._export_dialog.setMinimumDuration(0)
        self._export_dialog.setAutoClose(False)
        self._export_dialog.setAutoReset(False)
        self._export_dialog.canceled.connect(self._on_export_cancel)
        self._export_dialog.setValue(0)

        self._exporter = VideoExporter(self._enhancer)
        self._exporter.export(
            video_url=stream.video_url,
            audio_url=stream.audio_url or stream.video_url,
            http_headers=stream.http_headers,
            export_settings=es,
            progress_callback=lambda p, msg: self._export_progress.emit(p, msg),
            done_callback=lambda r: self._export_done.emit(r),
        )
        self._status_label.setText("视频导出中...")

    @Slot(float, str)
    def _on_export_progress(self, progress: float, message: str):
        if self._export_dialog is not None:
            self._export_dialog.setValue(int(progress * 100))
            self._export_dialog.setLabelText(message)

    @Slot()
    def _on_export_cancel(self):
        if self._exporter is not None:
            self._exporter.cancel()
        self._status_label.setText("正在取消导出...")

    @Slot(object)
    def _on_export_done(self, result: ExportResult):
        """导出结束（成功/失败/取消）：关进度框、弹结果、清理 exporter。"""
        if self._export_dialog is not None:
            self._export_dialog.close()
            self._export_dialog = None
        self._exporter = None

        if not result.success:
            self._status_label.setText(f"导出结束: {result.message}")
            if "取消" not in result.message:
                QMessageBox.warning(self, "导出失败", result.message)
            return

        self._status_label.setText("导出成功")
        body = (
            f"已保存到：\n{result.output_path}\n\n"
            f"视频信息：{result.video_info_label}\n"
            f"音频修复方案：{result.audio_scheme_label}\n"
            f"画面增强方案：{result.video_scheme_label}"
        )
        if not result.gpu_baked:
            body += (
                "\n\n注意：当前环境无法创建离屏 GPU 渲染上下文，已退化为 PyAV 近似烘焙；"
                "GLSL 着色器（Anime4K/FSR 超分、CAS 锐化等）未能真实烘焙到文件。"
            )
        QMessageBox.information(self, "导出成功", body)

    @Slot(str)
    def _on_cookie_imported_from_settings(self, path: str):
        """Handle cookie import from the settings dialog."""
        self._resolver = self._create_resolver()
        self._bili_api.set_cookies_from_file(path)
        self._status_label.setText("Cookie 已导入")
        self._refresh_cookie_status()

    @Slot(bool)
    def _on_thumbnail_mode_changed(self, enabled: bool):
        """Toggle thumbnail mode on playlist panel and content browser."""
        self._playlist_panel.set_thumbnail_mode(enabled)
        self._content_browser.set_thumbnail_mode(enabled)

    @Slot(int)
    def _on_thumbnail_size_changed(self, width: int):
        """Update thumbnail display size on both panels."""
        self._playlist_panel.set_thumbnail_size(width)
        self._content_browser.set_thumbnail_size(width)

    @Slot()
    def _on_import_cookie(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 Cookie 文件", "",
            "Cookie 文件 (*.txt);;所有文件 (*)",
        )
        if path:
            self._settings.set("cookie_file", path)
            self._settings.set("cookie_browser", "")
            self._resolver = self._create_resolver()
            self._bili_api.set_cookies_from_file(path)
            self._status_label.setText("Cookie 已导入")
            self._refresh_cookie_status()

    @Slot()
    def _on_auto_cookie(self):
        QMessageBox.information(
            self,
            "Cookie 导出教程",
            "获取普通用户或大会员画质需要导入 Cookie 文件：\n\n"
            "1. 在 Edge 中安装扩展 \"et cookies txt\"\n"
            "   (Edge 扩展商店搜索即可)\n\n"
            "2. 打开 bilibili.com 任意页面（确保已登录）\n\n"
            "3. 点击扩展图标 → 导出为 Netscape 格式\n"
            "   重要：选择 \"All Cookies\" 而非 \"Current Site\"\n"
            "   或者确保导出域名包含 .bilibili.com\n\n"
            "4. 保存 .txt 文件后，点击左侧\"导入...\"按钮选择该文件\n\n"
            "提示：导出的文件必须包含 SESSDATA 等认证 Cookie，\n"
            "仅导出 www.bilibili.com 的 Cookie 不包含登录信息。",
            QMessageBox.StandardButton.Ok,
        )

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
            self._sync.set_frame_gen_active(False, 1.0)
            self._update_media_info()
            return

        if backend == "display-resample":
            self._stop_ls_if_selected()
            self._apply_display_resample(player, params)
            self._sync.set_frame_gen_active(False, 1.0)
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
            self._sync.set_frame_gen_active(False, 1.0)
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
        self._sync.set_frame_gen_active(False, 1.0)
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
        # Auto-fallback if playback approaches end of enhanced coverage
        if self._enhanced_playing and self._enhanced_duration_s > 0:
            pipeline_state = self._pipeline.status.state
            if pipeline_state != PipelineState.READY and pos > self._enhanced_duration_s - 3.0:
                self._sync.deactivate_enhanced()
                self._enhanced_playing = False
                self._update_media_info()
                self._status_label.setText("播放位置接近增强边界 — 回退到源音频")

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

    def changeEvent(self, event):
        """Auto-check clipboard when window regains focus."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._check_clipboard_url()

    def _check_clipboard_url(self):
        """Check clipboard for a YouTube/Bilibili URL and auto-fill + resolve."""
        clipboard = QApplication.clipboard()
        text = (clipboard.text() or "").strip()
        if not text:
            return
        # Only proceed if it matches known URL patterns
        match = _CLIPBOARD_URL_RE.search(text)
        if not match:
            return
        # Extract the full URL (non-whitespace run containing the match)
        start = match.start()
        end = match.end()
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        while end < len(text) and not text[end].isspace():
            end += 1
        url = text[start:end]
        # Don't re-trigger on the same URL already in the input
        if url == self._url_input.text().strip():
            return
        self._url_input.setText(url)

    def closeEvent(self, event):
        self._live_refresh_timer.stop()
        self._settings.flush()
        # 退出前取消进行中的视频导出（后台线程会自行收尾）
        if self._exporter is not None:
            try:
                self._exporter.cancel()
            except Exception:
                pass
        self._sync.cleanup()
        self._pipeline.cleanup()
        self._enhancer.unload()
        self._thumbnail_cache.shutdown()
        # 退出时务必终止小黄鸭进程（已确认接受可能误杀用户自开实例）
        try:
            self._ls_controller.terminate()
        except Exception:
            pass
        self._player_widget.destroy()
        event.accept()

    # --- Playlist navigation ---

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
            if status.enhanced_file and status.state in (PipelineState.READY, PipelineState.ENHANCING):
                current_pos = self._player_widget.position
                if status.enhanced_duration_s >= current_pos:
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

        # Load model in background, then start enhancement
        self._enhance_panel.show_progress(True)
        self._enhance_panel.update_progress(0.0, "正在加载模型...")

        def _load_and_enhance():
            if not self._enhancer.load_models():
                self._enhance_status_update.emit(
                    PipelineStatus(state=PipelineState.ERROR,
                                   message="模型加载失败，请检查模型文件是否存在")
                )
                return

            self._enhance_status_update.emit(("model_loaded", None))

            audio_url = self._current_stream.audio_url or self._current_stream.video_url
            headers = self._current_stream.http_headers
            self._pipeline.start_enhance(audio_url, headers)

        threading.Thread(target=_load_and_enhance, daemon=True).start()

    @Slot()
    def _on_enhance_cancel(self):
        self._pipeline.cancel()
        self._enhancer.unload()
        self._enhance_panel.show_progress(False)
        self._enhanced_duration_s = 0.0
        if self._enhanced_playing:
            self._sync.deactivate_enhanced()
            self._enhanced_playing = False
            self._update_media_info()
        self._status_label.setText("增强已取消")

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

        # Handle PipelineStatus
        if not isinstance(status, PipelineStatus):
            return

        self._enhance_panel.update_progress(status.progress, status.message)

        # Track how much enhanced audio is available
        if status.enhanced_duration_s > 0:
            self._enhanced_duration_s = status.enhanced_duration_s
            # Keep sync manager aware of the write frontier
            self._sync.update_enhanced_duration(status.enhanced_duration_s)

        if status.state == PipelineState.READY and status.enhanced_file:
            self._enhance_panel.show_progress(False)
            self._enhanced_duration_s = status.enhanced_duration_s
            self._enhanced_output_sr = status.output_sr
            if not self._enhanced_playing:
                self._status_label.setText("增强完成 — 切换到增强音频")
                current_pos = self._player_widget.position
                self._sync.activate_enhanced(status.enhanced_file, current_pos)
                self._enhanced_playing = True
                self._update_media_info()

        elif status.state == PipelineState.ERROR:
            self._enhance_panel.show_progress(False)
            if status.recoverable and self._enhanced_playing:
                self._sync.fallback_to_original(status.message)
                self._enhanced_playing = False
                self._update_media_info()
                self._status_label.setText(f"增强失败，已回退到原始音频: {status.message}")
            else:
                self._status_label.setText(f"增强失败: {status.message}")
                QMessageBox.warning(self, "增强失败", status.message)

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

    # ─── Subtitle ───────────────────────────────────────────────────────

    def _update_subtitle_btn_state(self):
        """Enable/disable subtitle button based on LLM configuration."""
        providers = self._settings.get("llm_providers") or []
        has_llm = bool(providers)
        self._subtitle_btn.setEnabled(has_llm)
        if not has_llm:
            self._subtitle_btn.setToolTip("请先在设置中配置 LLM 服务商")
        else:
            self._subtitle_btn.setToolTip("生成 AI 字幕")

    @Slot()
    def _on_llm_config_changed(self):
        """Update subtitle button state when LLM config changes."""
        self._update_subtitle_btn_state()

    @Slot()
    def _on_subtitle_requested(self):
        """Handle subtitle button click."""
        if not self._current_stream or self._is_live:
            return

        providers = self._settings.get("llm_providers") or []
        default_name = self._settings.get("llm_default_provider") or ""
        provider_data = None
        for p in providers:
            if p.get("name") == default_name:
                provider_data = p
                break
        if not provider_data and providers:
            provider_data = providers[0]

        if not provider_data:
            QMessageBox.information(self, "提示", "请先在设置中配置 LLM 服务商")
            return

        model_id = self._settings.get("subtitle_model") or "openai/whisper-large-v3"
        lang_idx = self._subtitle_lang_combo.currentIndex()
        language = "zh" if lang_idx == 0 else "en"

        # Check cache first
        video_id = extract_video_id(self._current_stream.url or self._url_input.text())
        from src.core.subtitle import SUBTITLE_CACHE_DIR
        cache_path = SUBTITLE_CACHE_DIR / f"{video_id}_{language}.srt"
        if cache_path.exists():
            self._load_subtitle(str(cache_path))
            return

        # Start pipeline
        self._subtitle_btn.setEnabled(False)
        self._subtitle_btn.setText("...")
        self._status_label.setText("字幕生成中...")

        llm_provider = provider_from_dict(provider_data)
        self._subtitle_pipeline = SubtitlePipeline(
            model_id=model_id,
            llm_provider=llm_provider,
            progress_callback=lambda s: self._subtitle_status_update.emit(s),
        )
        self._subtitle_pipeline.generate(
            audio_url=self._current_stream.audio_url,
            video_url=self._current_stream.url or self._url_input.text(),
            language=language,
            http_headers=self._current_stream.http_headers,
        )

    @Slot(object)
    def _handle_subtitle_status(self, status: SubtitleStatus):
        """Handle subtitle pipeline progress updates."""
        if status.state == "done":
            self._subtitle_btn.setEnabled(True)
            self._subtitle_btn.setText("字幕")
            self._status_label.setText("字幕已加载")
            if status.srt_path:
                self._load_subtitle(status.srt_path)
        elif status.state == "error":
            self._subtitle_btn.setEnabled(True)
            self._subtitle_btn.setText("字幕")
            self._status_label.setText(status.message)
        else:
            pct = int(status.progress * 100)
            self._status_label.setText(f"字幕生成中 ({pct}%) — {status.message}")

    def _load_subtitle(self, path: str):
        """Load SRT into mpv and auto-start playback if paused at beginning."""
        self._player_widget.load_subtitle(path)
        # Auto-start if video is paused near the beginning
        if not self._player_widget.is_playing and self._player_widget.position < 1.0:
            self._player_widget.resume()
        self._status_label.setText("字幕已加载")
