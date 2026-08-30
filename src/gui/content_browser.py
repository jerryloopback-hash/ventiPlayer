"""Content Browser panel — discover videos via recommendations, popular, favorites, search."""

import threading
import time
import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QListWidget, QListWidgetItem, QPushButton, QLineEdit,
    QLabel, QMenu,
)
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QSize
from PySide6.QtGui import QPixmap, QIcon

from src.core.bilibili_api import BilibiliAPI, BiliVideoItem
from src.gui.thumbnail_cache import ThumbnailCache

logger = logging.getLogger(__name__)


def _format_duration(seconds: int) -> str:
    """Format seconds as mm:ss or h:mm:ss."""
    if seconds <= 0:
        return ""
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class _VideoListWidget(QListWidget):
    """QListWidget with double-click, right-click, and infinite scroll support."""

    play_requested = Signal(str)  # bvid
    add_queue_requested = Signal(str, str)  # url, title
    load_more = Signal()  # emitted when scrolled near bottom

    _SCROLL_THRESHOLD = 10  # pixels from bottom to trigger
    _COOLDOWN_MS = 1000  # minimum ms between load_more emissions

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.itemDoubleClicked.connect(self._on_double_click)
        self.setStyleSheet("QListWidget { font-size: 12px; }")
        self.setWordWrap(False)

        # Infinite scroll state
        self._loading = False
        self._last_load_time = 0.0
        self._scroll_enabled = True  # can be disabled for tabs that don't need it

        # Connect scrollbar
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def set_infinite_scroll(self, enabled: bool):
        """Enable or disable infinite scroll for this list."""
        self._scroll_enabled = enabled

    def set_loading(self, loading: bool):
        """Set the loading state to prevent concurrent loads."""
        self._loading = loading

    def is_loading(self) -> bool:
        return self._loading

    def _on_scroll(self, value: int):
        if not self._scroll_enabled or self._loading:
            return
        scrollbar = self.verticalScrollBar()
        if scrollbar.maximum() <= 0:
            return  # not enough content to scroll
        if value >= scrollbar.maximum() - self._SCROLL_THRESHOLD:
            now = time.time() * 1000
            if now - self._last_load_time >= self._COOLDOWN_MS:
                self._last_load_time = now
                self._loading = True
                self.load_more.emit()

    def _on_double_click(self, item: QListWidgetItem):
        bvid = item.data(Qt.ItemDataRole.UserRole)
        if bvid:
            # Don't emit play for folder items — handled by favorites tab
            if isinstance(bvid, str) and bvid.startswith("folder:"):
                return
            self.play_requested.emit(bvid)

    def _show_context_menu(self, pos):
        item = self.itemAt(pos)
        if not item:
            return
        bvid = item.data(Qt.ItemDataRole.UserRole)
        title = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        if not bvid:
            return

        menu = QMenu(self)
        play_action = menu.addAction("播放")
        queue_action = menu.addAction("加入播放列表")
        action = menu.exec(self.mapToGlobal(pos))

        url = f"https://www.bilibili.com/video/{bvid}"
        if action == play_action:
            self.play_requested.emit(bvid)
        elif action == queue_action:
            self.add_queue_requested.emit(url, title)


class ContentBrowser(QWidget):
    """Content discovery panel with tabs: recommend, popular, favorites, search."""

    play_video = Signal(str)  # URL
    play_video_with_context = Signal(str, list)  # URL, list of BiliVideoItem siblings
    add_to_queue = Signal(str, str)  # url, title
    _results_ready = Signal(str, object)  # target, data — thread-safe delivery

    def __init__(self, bili_api: BilibiliAPI, thumbnail_cache: ThumbnailCache = None, parent=None):
        super().__init__(parent)
        self._api = bili_api
        self._thumbnail_cache = thumbnail_cache
        self._thumbnail_mode = False
        self._thumbnail_size = 80
        self._user_mid = 0
        self._fav_folders: list[dict] = []
        self._current_fav_id: int = 0
        self._recommendations: list[BiliVideoItem] = []

        # Pagination state
        self._pop_page = 1
        self._pop_bvids: set[str] = set()
        self._rec_page = 1
        self._rec_bvids: set[str] = set()
        self._search_page = 1
        self._search_keyword = ""
        self._search_bvids: set[str] = set()

        self._results_ready.connect(self._handle_results)
        self._setup_ui()
        if self._thumbnail_cache:
            self._thumbnail_cache.thumbnail_ready.connect(self._on_thumbnail_ready)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._tabs.setStyleSheet(
            "QTabBar::tab { padding: 3px 8px; font-size: 11px; }"
        )
        layout.addWidget(self._tabs)

        # Tab 0: Recommend
        self._rec_tab = self._build_recommend_tab()
        self._tabs.addTab(self._rec_tab, "推荐")

        # Tab 1: Popular
        self._pop_tab = self._build_popular_tab()
        self._tabs.addTab(self._pop_tab, "热门")

        # Tab 2: Favorites
        self._fav_tab = self._build_favorites_tab()
        self._tabs.addTab(self._fav_tab, "收藏夹")

        # Tab 3: Search
        self._search_tab = self._build_search_tab()
        self._tabs.addTab(self._search_tab, "搜索")

        # Load popular on first show
        self._tabs.currentChanged.connect(self._on_tab_changed)

    # ─── Tab builders ───────────────────────────────────────────────

    def _build_recommend_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        self._rec_list = _VideoListWidget()
        self._rec_list.play_requested.connect(self._on_play_bvid)
        self._rec_list.add_queue_requested.connect(self.add_to_queue.emit)
        self._rec_list.load_more.connect(self._load_more_recommendations)
        lay.addWidget(self._rec_list, 1)

        self._rec_status = QLabel("")
        self._rec_status.setStyleSheet("color: gray; font-size: 11px;")
        self._rec_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._rec_status)

        return w

    def _build_popular_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        self._pop_list = _VideoListWidget()
        self._pop_list.play_requested.connect(self._on_play_bvid)
        self._pop_list.add_queue_requested.connect(self.add_to_queue.emit)
        self._pop_list.load_more.connect(self._load_more_popular)
        lay.addWidget(self._pop_list, 1)

        self._pop_status = QLabel("")
        self._pop_status.setStyleSheet("color: gray; font-size: 11px;")
        self._pop_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._pop_status)

        return w

    def _build_favorites_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        # Back button (hidden initially)
        self._fav_back_btn = QPushButton("< 返回收藏夹列表")
        self._fav_back_btn.setFixedHeight(24)
        self._fav_back_btn.setStyleSheet("font-size: 11px;")
        self._fav_back_btn.hide()
        self._fav_back_btn.clicked.connect(self._show_fav_folders)
        lay.addWidget(self._fav_back_btn)

        self._fav_list = _VideoListWidget()
        self._fav_list.play_requested.connect(self._on_play_bvid)
        self._fav_list.add_queue_requested.connect(self.add_to_queue.emit)
        self._fav_list.itemDoubleClicked.connect(self._on_fav_item_clicked)
        # Disable infinite scroll for favorites (folder navigation)
        self._fav_list.set_infinite_scroll(False)
        lay.addWidget(self._fav_list, 1)

        self._fav_status = QLabel("")
        self._fav_status.setStyleSheet("color: gray; font-size: 11px;")
        self._fav_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._fav_status)

        self._fav_refresh_btn = QPushButton("刷新")
        self._fav_refresh_btn.setFixedHeight(26)
        self._fav_refresh_btn.clicked.connect(self._load_favorites)
        lay.addWidget(self._fav_refresh_btn)

        return w

    def _build_search_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        # Search bar
        search_bar = QHBoxLayout()
        search_bar.setSpacing(4)
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索B站视频...")
        self._search_input.returnPressed.connect(self._do_search)
        self._search_btn = QPushButton("搜索")
        self._search_btn.setFixedWidth(50)
        self._search_btn.setFixedHeight(26)
        self._search_btn.clicked.connect(self._do_search)
        search_bar.addWidget(self._search_input, 1)
        search_bar.addWidget(self._search_btn)
        lay.addLayout(search_bar)

        self._search_list = _VideoListWidget()
        self._search_list.play_requested.connect(self._on_play_bvid)
        self._search_list.add_queue_requested.connect(self.add_to_queue.emit)
        self._search_list.load_more.connect(self._load_more_search)
        lay.addWidget(self._search_list, 1)

        self._search_status = QLabel("")
        self._search_status.setStyleSheet("color: gray; font-size: 11px;")
        self._search_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._search_status)

        return w

    # ─── Public API ─────────────────────────────────────────────────

    def set_recommendations(self, items: list[BiliVideoItem]):
        """Called externally when related videos are fetched for current playback."""
        self._recommendations = list(items)
        self._rec_bvids.clear()
        self._populate_list(self._rec_list, items)
        for v in items:
            self._rec_bvids.add(v.bvid)
        if items:
            self._rec_status.setText(f"{len(items)} 个推荐")
        else:
            self._rec_status.setText("")

    # ─── Signal handlers ────────────────────────────────────────────

    def _on_play_bvid(self, bvid: str):
        url = f"https://www.bilibili.com/video/{bvid}"
        # Determine which tab/list the play came from and gather siblings
        siblings = self._get_current_tab_items()
        self.play_video.emit(url)
        self.play_video_with_context.emit(url, siblings)

    def _get_current_tab_items(self) -> list:
        """Get all BiliVideoItem-like data from the currently active tab's list."""
        tab_index = self._tabs.currentIndex()
        if tab_index == 0:
            return list(self._recommendations) if self._recommendations else []
        elif tab_index == 1:
            return self._extract_items_from_list(self._pop_list)
        elif tab_index == 2:
            # Only return items if we're inside a folder (not showing folder list)
            if self._current_fav_id:
                return self._extract_items_from_list(self._fav_list)
            return []
        elif tab_index == 3:
            return self._extract_items_from_list(self._search_list)
        return []

    def _extract_items_from_list(self, list_widget: QListWidget) -> list:
        """Extract BiliVideoItem-compatible data from a QListWidget."""
        items = []
        for i in range(list_widget.count()):
            w_item = list_widget.item(i)
            bvid = w_item.data(Qt.ItemDataRole.UserRole)
            if not bvid or (isinstance(bvid, str) and bvid.startswith("folder:")):
                continue
            title = w_item.data(Qt.ItemDataRole.UserRole + 1) or ""
            thumbnail = w_item.data(Qt.ItemDataRole.UserRole + 2) or ""
            items.append(BiliVideoItem(
                bvid=bvid,
                title=title,
                duration=0,
                thumbnail=thumbnail,
                owner_name="",
            ))
        return items

    def _on_tab_changed(self, index: int):
        if index == 0 and self._rec_list.count() == 0:
            self._load_recommendations()
        elif index == 1 and self._pop_list.count() == 0:
            self._load_popular()
        elif index == 2 and self._fav_list.count() == 0:
            self._load_favorites()

    # ─── Recommend tab ──────────────────────────────────────────────

    def _load_recommendations(self):
        """Initial load for recommendations tab."""
        if self._recommendations:
            self._rec_bvids.clear()
            self._populate_list(self._rec_list, self._recommendations)
            for v in self._recommendations:
                self._rec_bvids.add(v.bvid)
            self._rec_status.setText(f"{len(self._recommendations)} 个推荐")
        else:
            # Fall back to popular
            self._rec_status.setText("加载中...")
            self._rec_list.set_loading(True)

            def _worker():
                try:
                    items = self._api.get_popular()
                except Exception:
                    items = []
                self._deliver_results("rec", items)

            threading.Thread(target=_worker, daemon=True).start()

    def _load_more_recommendations(self):
        """Load more recommendations (infinite scroll)."""
        self._rec_status.setText("加载中...")
        self._rec_page += 1
        page = self._rec_page

        def _worker():
            try:
                items = self._api.get_popular(page=page)
            except Exception:
                items = []
            self._deliver_results("rec_more", items)

        threading.Thread(target=_worker, daemon=True).start()

    # ─── Popular tab ────────────────────────────────────────────────

    def _load_popular(self):
        """Initial load for popular tab."""
        self._pop_page = 1
        self._pop_bvids.clear()
        self._pop_list.clear()
        self._pop_status.setText("加载中...")
        self._pop_list.set_loading(True)

        def _worker():
            try:
                items = self._api.get_popular(page=1)
            except Exception:
                items = []
            self._deliver_results("pop", items)

        threading.Thread(target=_worker, daemon=True).start()

    def _load_more_popular(self):
        """Load next page of popular videos (infinite scroll)."""
        self._pop_status.setText("加载中...")
        next_page = self._pop_page + 1

        def _worker():
            try:
                items = self._api.get_popular(page=next_page)
            except Exception:
                items = []
            self._deliver_results("pop_more", items)

        threading.Thread(target=_worker, daemon=True).start()

    # ─── Favorites tab ──────────────────────────────────────────────

    def _load_favorites(self):
        self._fav_status.setText("加载中...")
        self._fav_refresh_btn.setEnabled(False)

        def _worker():
            try:
                mid = self._api.get_user_mid()
                self._user_mid = mid
                if mid == 0:
                    self._deliver_results("fav_error", "请先导入 Cookie")
                    return
                folders = self._api.get_user_favorites(mid)
                self._fav_folders = folders
                self._deliver_results("fav_folders", folders)
            except Exception as e:
                logger.debug("Failed to load favorites: %s", e)
                self._deliver_results("fav_error", "加载失败，点击刷新重试")

        threading.Thread(target=_worker, daemon=True).start()

    def _show_fav_folders(self):
        """Show the folder list view."""
        self._fav_back_btn.hide()
        self._current_fav_id = 0
        self._fav_list.clear()
        for folder in self._fav_folders:
            item = QListWidgetItem()
            title = folder["title"]
            count = folder["media_count"]
            item.setText(f"{title} ({count})")
            item.setData(Qt.ItemDataRole.UserRole, f"folder:{folder['id']}")
            item.setData(Qt.ItemDataRole.UserRole + 1, title)
            item.setToolTip(title)
            self._fav_list.addItem(item)
        self._fav_status.setText(f"{len(self._fav_folders)} 个收藏夹")
        self._fav_refresh_btn.setEnabled(True)

    def _on_fav_item_clicked(self, item: QListWidgetItem):
        """Handle double-click in favorites — either open folder or play video."""
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        if isinstance(data, str) and data.startswith("folder:"):
            media_id = int(data.split(":")[1])
            self._current_fav_id = media_id
            QTimer.singleShot(0, lambda: self._load_fav_content(media_id))

    def _load_fav_content(self, media_id: int):
        self._fav_status.setText("加载中...")
        self._fav_list.clear()

        def _worker():
            try:
                items = self._api.get_favorite_content(media_id)
            except Exception:
                items = []
            self._deliver_results("fav_content", items)

        threading.Thread(target=_worker, daemon=True).start()

    # ─── Search tab ─────────────────────────────────────────────────

    def _do_search(self):
        keyword = self._search_input.text().strip()
        if not keyword:
            return
        # Reset pagination for new search
        self._search_keyword = keyword
        self._search_page = 1
        self._search_bvids.clear()
        self._search_status.setText("搜索中...")
        self._search_btn.setEnabled(False)
        self._search_list.clear()
        self._search_list.set_loading(True)

        def _worker():
            try:
                items = self._api.search_videos(keyword, page=1)
            except Exception:
                items = []
            self._deliver_results("search", items)

        threading.Thread(target=_worker, daemon=True).start()

    def _load_more_search(self):
        """Load next page of search results (infinite scroll)."""
        if not self._search_keyword:
            self._search_list.set_loading(False)
            return
        self._search_status.setText("加载中...")
        next_page = self._search_page + 1

        def _worker():
            try:
                items = self._api.search_videos(self._search_keyword, page=next_page)
            except Exception:
                items = []
            self._deliver_results("search_more", items)

        threading.Thread(target=_worker, daemon=True).start()

    # ─── Thread-safe result delivery ────────────────────────────────

    def _deliver_results(self, target: str, data):
        """Emit signal to deliver results to the main thread safely."""
        self._results_ready.emit(target, data)

    def _handle_results(self, target: str, data):
        """Process results on the main thread."""
        if target == "rec":
            self._rec_list.set_loading(False)
            if data:
                self._rec_bvids.clear()
                self._populate_list(self._rec_list, data)
                for v in data:
                    self._rec_bvids.add(v.bvid)
                self._rec_status.setText(f"{len(data)} 个热门视频")
            else:
                self._rec_status.setText("加载失败")

        elif target == "rec_more":
            self._rec_list.set_loading(False)
            if data:
                new_items = [v for v in data if v.bvid not in self._rec_bvids]
                if new_items:
                    self._append_to_list(self._rec_list, new_items)
                    for v in new_items:
                        self._rec_bvids.add(v.bvid)
                    total = self._rec_list.count()
                    self._rec_status.setText(f"{total} 个推荐")
                else:
                    self._rec_status.setText(f"{self._rec_list.count()} 个推荐")
            else:
                self._rec_status.setText(f"{self._rec_list.count()} 个推荐")

        elif target == "pop":
            self._pop_list.set_loading(False)
            if data:
                self._populate_list(self._pop_list, data)
                for v in data:
                    self._pop_bvids.add(v.bvid)
                self._pop_page = 1
                self._pop_status.setText(f"{len(data)} 个热门视频")
            else:
                self._pop_status.setText("加载失败")

        elif target == "pop_more":
            self._pop_list.set_loading(False)
            if data:
                new_items = [v for v in data if v.bvid not in self._pop_bvids]
                if new_items:
                    self._pop_page += 1
                    self._append_to_list(self._pop_list, new_items)
                    for v in new_items:
                        self._pop_bvids.add(v.bvid)
                    total = self._pop_list.count()
                    self._pop_status.setText(f"{total} 个热门视频")
                else:
                    self._pop_status.setText(f"{self._pop_list.count()} 个热门视频 · 没有更多了")
            else:
                self._pop_status.setText(f"{self._pop_list.count()} 个热门视频")

        elif target == "fav_folders":
            self._fav_refresh_btn.setEnabled(True)
            if data:
                self._show_fav_folders()
            else:
                self._fav_status.setText("没有收藏夹")

        elif target == "fav_error":
            self._fav_refresh_btn.setEnabled(True)
            self._fav_status.setText(str(data))

        elif target == "fav_content":
            self._fav_refresh_btn.setEnabled(True)
            self._fav_back_btn.show()
            if data:
                self._populate_list(self._fav_list, data)
                self._fav_status.setText(f"{len(data)} 个视频")
            else:
                self._fav_status.setText("加载失败，点击刷新重试")

        elif target == "search":
            self._search_list.set_loading(False)
            self._search_btn.setEnabled(True)
            if data:
                self._populate_list(self._search_list, data)
                for v in data:
                    self._search_bvids.add(v.bvid)
                self._search_status.setText(f"{len(data)} 个结果")
            else:
                self._search_status.setText("无结果或加载失败")

        elif target == "search_more":
            self._search_list.set_loading(False)
            if data:
                new_items = [v for v in data if v.bvid not in self._search_bvids]
                if new_items:
                    self._search_page += 1
                    self._append_to_list(self._search_list, new_items)
                    for v in new_items:
                        self._search_bvids.add(v.bvid)
                    total = self._search_list.count()
                    self._search_status.setText(f"{total} 个结果")
                else:
                    self._search_status.setText(f"{self._search_list.count()} 个结果 · 没有更多了")
            else:
                self._search_status.setText(f"{self._search_list.count()} 个结果")

    # ─── Thumbnail mode ──────────────────────────────────────────────

    def set_thumbnail_mode(self, enabled: bool):
        """Enable or disable thumbnail display in all video lists."""
        if self._thumbnail_mode == enabled:
            return
        self._thumbnail_mode = enabled
        if enabled:
            w = self._thumbnail_size
            h = round(w * 9 / 16)
            icon_size = QSize(w, h)
        else:
            icon_size = QSize(0, 0)
        for lw in (self._rec_list, self._pop_list, self._fav_list, self._search_list):
            lw.setIconSize(icon_size)
        # Refresh visible lists
        if self._recommendations:
            self._populate_list(self._rec_list, self._recommendations)

    def set_thumbnail_size(self, width: int):
        """Update the thumbnail display size (height = width * 9/16)."""
        self._thumbnail_size = width
        if self._thumbnail_mode:
            h = round(width * 9 / 16)
            icon_size = QSize(width, h)
            for lw in (self._rec_list, self._pop_list, self._fav_list, self._search_list):
                lw.setIconSize(icon_size)

    @Slot(str, QPixmap)
    def _on_thumbnail_ready(self, url: str, pixmap: QPixmap):
        """Update items in all lists whose thumbnail URL matches."""
        if not self._thumbnail_mode:
            return
        icon = QIcon(pixmap)
        for lw in (self._rec_list, self._pop_list, self._fav_list, self._search_list):
            for i in range(lw.count()):
                item = lw.item(i)
                stored_url = item.data(Qt.ItemDataRole.UserRole + 2)
                if stored_url == url:
                    item.setIcon(icon)

    # ─── Helpers ────────────────────────────────────────────────────

    def _populate_list(self, list_widget: QListWidget, items: list[BiliVideoItem]):
        """Fill a QListWidget with video items (clears existing content)."""
        list_widget.clear()
        self._append_to_list(list_widget, items)

    def _append_to_list(self, list_widget: QListWidget, items: list[BiliVideoItem]):
        """Append video items to a QListWidget without clearing."""
        for v in items:
            item = QListWidgetItem()
            # Truncate title for display
            title = v.title
            display_title = title if len(title) <= 28 else title[:26] + "..."
            dur_str = _format_duration(v.duration)
            line2_parts = []
            if v.owner_name:
                line2_parts.append(v.owner_name)
            if dur_str:
                line2_parts.append(dur_str)
            line2 = " · ".join(line2_parts)
            item.setText(f"{display_title}\n{line2}")
            item.setToolTip(title)
            item.setData(Qt.ItemDataRole.UserRole, v.bvid)
            item.setData(Qt.ItemDataRole.UserRole + 1, title)
            item.setData(Qt.ItemDataRole.UserRole + 2, v.thumbnail)

            # Set thumbnail icon if in thumbnail mode
            if self._thumbnail_mode and self._thumbnail_cache and v.thumbnail:
                pixmap = self._thumbnail_cache.request(v.thumbnail)
                if pixmap:
                    item.setIcon(QIcon(pixmap))

            list_widget.addItem(item)
