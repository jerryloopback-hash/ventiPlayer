"""主窗口组装层：继承 src/gui/mw/ 下 7 个职责 mixin，本模块只保留
__init__ 聚合、生命周期（closeEvent）、剪贴板 URL 自动识别与字幕加载。"""
import re
import logging

from PySide6.QtWidgets import QMainWindow, QApplication, QProgressDialog
from PySide6.QtCore import QTimer, Signal, QEvent

from src.gui.mw.ui_setup import UiSetupMixin
from src.gui.mw.app_init import AppInitMixin
from src.gui.mw.playback import PlaybackMixin
from src.gui.mw.media_info import MediaInfoMixin
from src.gui.mw.enhance import EnhanceIntegrationMixin
from src.gui.mw.export_mixin import ExportMixin
from src.gui.mw.subtitle_mixin import SubtitleMixin
from src.core.stream import StreamResolver, StreamInfo
from src.core.playlist import PlaylistManager, HistoryManager
from src.core.bilibili_api import BilibiliAPI, BiliVideoItem
from src.core.enhancer import Enhancer
from src.core.audio_pipe import AudioPipeline
from src.core.video_export import VideoExporter
from src.core.sync import SyncManager
from src.core.frame_gen import FrameGenManager
from src.core.lossless_scaling import LosslessScalingController
from src.core.subtitle import SubtitlePipeline
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


class MainWindow(
    UiSetupMixin,
    AppInitMixin,
    PlaybackMixin,
    MediaInfoMixin,
    EnhanceIntegrationMixin,
    ExportMixin,
    SubtitleMixin,
    QMainWindow,
):
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

    def _load_subtitle(self, path: str):
        """Load SRT into mpv and auto-start playback if paused at beginning."""
        self._player_widget.load_subtitle(path)
        # Auto-start if video is paused near the beginning
        if not self._player_widget.is_playing and self._player_widget.position < 1.0:
            self._player_widget.resume()
        self._status_label.setText("字幕已加载")
