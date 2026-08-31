"""MainWindow UI 构建与信号接线 mixin：_setup_ui / _setup_shortcuts / _connect_signals。

从 main_window.py 拆出（2026-08 梳理）；MainWindow 组装时继承本 mixin，
self 即 MainWindow 实例，所有属性访问与拆分前一致。
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QSlider, QLabel,
    QComboBox, QCheckBox, QStatusBar, QSplitter, QTabWidget,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut

from src.gui.player_widget import MpvPlayerWidget
from src.gui.enhance_panel import EnhancePanel
from src.gui.video_enhance_panel import VideoEnhancePanel
from src.gui.playlist_panel import PlaylistPanel
from src.gui.content_browser import ContentBrowser
from src.gui.thumbnail_cache import ThumbnailCache
from src.core.playlist import PlayMode


class UiSetupMixin:
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
        # 音频增强任务状态：busy 期间忽略再次点击（防并发加载/推理导致 ROCm 原生崩溃）
        self._enhance_busy: bool = False
        # 当前增强请求的取消事件（每次请求新建，避免旧请求线程读到新请求的状态）
        self._enhance_abort_evt = None
        # 当前实际挂载的增强音频文件路径（渐进切换/READY 去重切换用）
        self._active_enhanced_file: str | None = None
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
