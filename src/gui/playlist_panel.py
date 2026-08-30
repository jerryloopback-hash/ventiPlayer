from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QMenu, QApplication, QLabel, QFrame, QTabWidget,
)
from PySide6.QtCore import Qt, Signal, Slot, QSize
from PySide6.QtGui import QFont, QAction, QPixmap, QIcon, QPalette

from src.core.playlist import PlaylistManager, PlayMode, VideoItem, HistoryManager
from src.gui.thumbnail_cache import ThumbnailCache


class PlaylistPanel(QWidget):
    item_double_clicked = Signal(int)
    history_item_double_clicked = Signal(str)  # url
    mode_changed = Signal(object)  # PlayMode
    recommendation_clicked = Signal(str)  # bvid

    def __init__(self, playlist: PlaylistManager, history: HistoryManager,
                 thumbnail_cache: ThumbnailCache = None, parent=None):
        super().__init__(parent)
        self._playlist = playlist
        self._history = history
        self._thumbnail_cache = thumbnail_cache
        self._thumbnail_mode = False
        self._thumbnail_size = 80
        self._season_prompt: QWidget | None = None
        self._setup_ui()
        self._connect_signals()
        if self._thumbnail_cache:
            self._thumbnail_cache.thumbnail_ready.connect(self._on_thumbnail_ready)

    def _setup_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 4, 0, 0)
        self._layout.setSpacing(4)

        # Season prompt placeholder (inserted dynamically above tabs)
        self._season_prompt = None

        # Tab widget for playlist vs history
        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._tabs.setStyleSheet(
            "QTabBar::tab { padding: 3px 8px; font-size: 11px; }"
        )

        # --- Tab 0: Playlist (source-based) ---
        playlist_tab = QWidget()
        pl_layout = QVBoxLayout(playlist_tab)
        pl_layout.setContentsMargins(2, 2, 2, 2)
        pl_layout.setSpacing(4)

        self._list = QListWidget()
        self._list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.setStyleSheet("QListWidget { font-size: 12px; }")
        pl_layout.addWidget(self._list, 1)

        # Clear button bar
        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(2)
        btn_bar.addStretch()

        self._clear_btn = QPushButton("清空")
        self._clear_btn.setFixedHeight(24)
        self._clear_btn.setFixedWidth(40)
        self._clear_btn.setStyleSheet("QPushButton { font-size: 11px; padding: 2px 4px; }")
        btn_bar.addWidget(self._clear_btn)

        pl_layout.addLayout(btn_bar)
        self._tabs.addTab(playlist_tab, "播放列表")

        # --- Tab 1: History ---
        history_tab = QWidget()
        hist_layout = QVBoxLayout(history_tab)
        hist_layout.setContentsMargins(2, 2, 2, 2)
        hist_layout.setSpacing(4)

        self._history_list = QListWidget()
        self._history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._history_list.setStyleSheet("QListWidget { font-size: 12px; }")
        hist_layout.addWidget(self._history_list, 1)

        hist_btn_bar = QHBoxLayout()
        hist_btn_bar.setSpacing(4)
        self._clear_history_btn = QPushButton("清空历史")
        self._clear_history_btn.setFixedHeight(24)
        self._clear_history_btn.setStyleSheet("QPushButton { font-size: 11px; padding: 2px 4px; }")
        hist_btn_bar.addStretch()
        hist_btn_bar.addWidget(self._clear_history_btn)
        hist_layout.addLayout(hist_btn_bar)

        self._tabs.addTab(history_tab, "历史")

        self._layout.addWidget(self._tabs, 1)

        # Recommendations section (below tabs)
        self._rec_separator = QLabel("── 推荐 ──")
        self._rec_separator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._rec_separator.setStyleSheet("color: #aaa; font-size: 11px; margin-top: 4px;")
        self._layout.addWidget(self._rec_separator)

        self._rec_list = QListWidget()
        self._rec_list.setMaximumHeight(200)
        self._rec_list.setStyleSheet("QListWidget { font-size: 11px; color: #eee; }")
        self._layout.addWidget(self._rec_list)

        # Show placeholder until real data arrives
        placeholder = QListWidgetItem("加载中...")
        self._rec_list.addItem(placeholder)

    def _connect_signals(self):
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        self._clear_btn.clicked.connect(self._on_clear)
        self._playlist.queue_changed.connect(self._refresh_list)
        self._playlist.current_changed.connect(self._highlight_current)
        self._rec_list.itemDoubleClicked.connect(self._on_rec_double_clicked)
        # History signals
        self._history_list.itemDoubleClicked.connect(self._on_history_item_double_clicked)
        self._history_list.customContextMenuRequested.connect(self._show_history_context_menu)
        self._clear_history_btn.clicked.connect(self._on_clear_history)
        self._history.history_changed.connect(self._refresh_history_list)
        # Initial population
        self._refresh_history_list()

    def _on_item_double_clicked(self, item: QListWidgetItem):
        row = self._list.row(item)
        self.item_double_clicked.emit(row)

    def _on_history_item_double_clicked(self, item: QListWidgetItem):
        url = item.data(Qt.ItemDataRole.UserRole + 3)
        if url:
            self.history_item_double_clicked.emit(url)

    def _on_rows_moved(self, *_args):
        new_order = []
        for i in range(self._list.count()):
            idx = self._list.item(i).data(Qt.ItemDataRole.UserRole)
            new_order.append(idx)

        self._playlist.reorder(new_order)
        self._refresh_list()

    def _show_context_menu(self, pos):
        item = self._list.itemAt(pos)
        if not item:
            return
        row = self._list.row(item)

        menu = QMenu(self)
        remove_action = QAction("移除", self)
        move_top_action = QAction("移到顶部", self)
        copy_link_action = QAction("复制链接", self)

        remove_action.triggered.connect(lambda: self._playlist.remove(row))
        move_top_action.triggered.connect(lambda: self._playlist.move(row, 0))
        copy_link_action.triggered.connect(lambda: self._copy_link(row))

        menu.addAction(remove_action)
        menu.addAction(move_top_action)
        menu.addAction(copy_link_action)
        menu.exec(self._list.mapToGlobal(pos))

    def _show_history_context_menu(self, pos):
        menu = QMenu(self)
        clear_action = QAction("清空历史", self)
        clear_action.triggered.connect(self._on_clear_history)
        menu.addAction(clear_action)
        menu.exec(self._history_list.mapToGlobal(pos))

    def _copy_link(self, index: int):
        queue = self._playlist.queue
        if 0 <= index < len(queue):
            clipboard = QApplication.clipboard()
            clipboard.setText(queue[index].url)

    def _on_clear(self):
        self._playlist.clear()

    def _on_clear_history(self):
        self._history.clear()

    @Slot()
    def _refresh_list(self):
        self._list.clear()
        for i, video_item in enumerate(self._playlist.queue):
            dur_str = ""
            if video_item.duration:
                m, s = divmod(int(video_item.duration), 60)
                dur_str = f" [{m}:{s:02d}]"
            text = f"{video_item.title}{dur_str}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setData(Qt.ItemDataRole.UserRole + 2, video_item.thumbnail_url)

            if self._thumbnail_mode and self._thumbnail_cache and video_item.thumbnail_url:
                pixmap = self._thumbnail_cache.request(video_item.thumbnail_url)
                if pixmap:
                    item.setIcon(QIcon(pixmap))

            self._list.addItem(item)
        self._highlight_current(self._playlist.current_index)

    @Slot()
    def _refresh_history_list(self):
        self._history_list.clear()
        for video_item in self._history.items:
            dur_str = ""
            if video_item.duration:
                m, s = divmod(int(video_item.duration), 60)
                dur_str = f" [{m}:{s:02d}]"
            text = f"{video_item.title}{dur_str}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole + 2, video_item.thumbnail_url)
            item.setData(Qt.ItemDataRole.UserRole + 3, video_item.url)

            if self._thumbnail_mode and self._thumbnail_cache and video_item.thumbnail_url:
                pixmap = self._thumbnail_cache.request(video_item.thumbnail_url)
                if pixmap:
                    item.setIcon(QIcon(pixmap))

            self._history_list.addItem(item)

    @Slot(int)
    def _highlight_current(self, index: int):
        for i in range(self._list.count()):
            item = self._list.item(i)
            font = item.font()
            if i == index:
                font.setBold(True)
                item.setFont(font)
                item.setBackground(Qt.GlobalColor.darkCyan)
                item.setForeground(Qt.GlobalColor.white)
            else:
                font.setBold(False)
                item.setFont(font)
                item.setBackground(Qt.GlobalColor.transparent)
                item.setData(Qt.ItemDataRole.ForegroundRole, None)

    # --- Thumbnail mode ---

    def set_thumbnail_mode(self, enabled: bool):
        """Enable or disable thumbnail display in the playlist."""
        if self._thumbnail_mode == enabled:
            return
        self._thumbnail_mode = enabled
        if enabled:
            w = self._thumbnail_size
            h = round(w * 9 / 16)
            self._list.setIconSize(QSize(w, h))
            self._history_list.setIconSize(QSize(w, h))
        else:
            self._list.setIconSize(QSize(0, 0))
            self._history_list.setIconSize(QSize(0, 0))
        self._refresh_list()
        self._refresh_history_list()

    def set_thumbnail_size(self, width: int):
        """Update the thumbnail display size (height = width * 9/16)."""
        self._thumbnail_size = width
        if self._thumbnail_mode:
            h = round(width * 9 / 16)
            self._list.setIconSize(QSize(width, h))
            self._history_list.setIconSize(QSize(width, h))

    @Slot(str, QPixmap)
    def _on_thumbnail_ready(self, url: str, pixmap: QPixmap):
        """Update items whose thumbnail URL matches the downloaded one."""
        if not self._thumbnail_mode:
            return
        icon = QIcon(pixmap)
        for lw in (self._list, self._history_list):
            for i in range(lw.count()):
                item = lw.item(i)
                stored_url = item.data(Qt.ItemDataRole.UserRole + 2)
                if stored_url == url:
                    item.setIcon(icon)

    # --- Season prompt ---

    def show_season_prompt(self, season_title: str, callback):
        """Show a prompt bar at the top: '合集《xxx》 [加载全部]'"""
        self.hide_season_prompt()

        prompt = QWidget()
        prompt_layout = QHBoxLayout(prompt)
        prompt_layout.setContentsMargins(4, 2, 4, 2)
        prompt_layout.setSpacing(4)

        label = QLabel(f"合集《{season_title}》")
        label.setStyleSheet("font-size: 11px; color: #333;")
        prompt_layout.addWidget(label, 1)

        load_btn = QPushButton("加载全部")
        load_btn.setFixedHeight(22)
        load_btn.setStyleSheet("font-size: 11px; padding: 2px 6px;")
        load_btn.clicked.connect(lambda: (callback(), self.hide_season_prompt()))
        prompt_layout.addWidget(load_btn)

        dismiss_btn = QPushButton("×")
        dismiss_btn.setFixedSize(20, 20)
        dismiss_btn.setStyleSheet("font-size: 12px; border: none;")
        dismiss_btn.clicked.connect(self.hide_season_prompt)
        prompt_layout.addWidget(dismiss_btn)

        prompt.setStyleSheet(
            "background-color: #FFF3CD; border: 1px solid #FFEEBA; border-radius: 3px;"
        )

        self._season_prompt = prompt
        self._layout.insertWidget(0, prompt)

    def hide_season_prompt(self):
        """Remove the season prompt if visible."""
        if self._season_prompt:
            self._season_prompt.setParent(None)
            self._season_prompt.deleteLater()
            self._season_prompt = None

    # --- Recommendations ---

    def set_recommendations(self, items: list):
        """Update the recommendations section below the queue.

        items: list of BiliVideoItem (or dicts with 'bvid' and 'title')
        """
        self._rec_list.clear()
        if not items:
            self._rec_separator.hide()
            self._rec_list.hide()
            return

        self._rec_separator.show()
        self._rec_list.show()

        # Show at most 5 recommendations
        for item in items[:5]:
            title = item.title if hasattr(item, "title") else str(item)
            bvid = item.bvid if hasattr(item, "bvid") else ""
            dur = item.duration if hasattr(item, "duration") else 0
            dur_str = ""
            if dur:
                m, s = divmod(int(dur), 60)
                dur_str = f" [{m}:{s:02d}]"
            list_item = QListWidgetItem(f"{title}{dur_str}")
            list_item.setData(Qt.ItemDataRole.UserRole, bvid)
            self._rec_list.addItem(list_item)

    def _on_rec_double_clicked(self, item: QListWidgetItem):
        bvid = item.data(Qt.ItemDataRole.UserRole)
        if bvid:
            self.recommendation_clicked.emit(bvid)
