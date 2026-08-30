import json
import random
from dataclasses import dataclass, asdict
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal


@dataclass
class VideoItem:
    bvid: str
    title: str
    duration: Optional[float]
    thumbnail_url: str
    source_type: str  # "bilibili", "youtube", "twitch"
    url: str  # webpage URL


class PlayMode(Enum):
    SEQUENTIAL = auto()
    SINGLE_LOOP = auto()
    LIST_LOOP = auto()
    SHUFFLE = auto()


HISTORY_PATH = Path.home() / ".ventiplayer" / "history.json"
MAX_HISTORY = 200


class HistoryManager(QObject):
    """Tracks all played videos (most recent first). Persists to disk."""

    history_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[VideoItem] = []
        self._load()

    @property
    def items(self) -> list[VideoItem]:
        return self._items

    def add(self, item: VideoItem):
        """Add a video to history (most recent first). Deduplicates by URL."""
        # Remove existing entry with same URL to avoid duplicates
        self._items = [v for v in self._items if v.url != item.url]
        self._items.insert(0, item)
        # Cap at max
        if len(self._items) > MAX_HISTORY:
            self._items = self._items[:MAX_HISTORY]
        self.history_changed.emit()
        self._save()

    def clear(self):
        self._items.clear()
        self.history_changed.emit()
        self._save()

    def _load(self):
        if HISTORY_PATH.exists():
            try:
                with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._items = [
                    VideoItem(
                        bvid=d.get("bvid", ""),
                        title=d.get("title", ""),
                        duration=d.get("duration"),
                        thumbnail_url=d.get("thumbnail_url", ""),
                        source_type=d.get("source_type", "bilibili"),
                        url=d.get("url", ""),
                    )
                    for d in data
                ]
            except (json.JSONDecodeError, OSError, KeyError):
                self._items = []

    def _save(self):
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump([asdict(v) for v in self._items], f, ensure_ascii=False)
        except OSError:
            pass

    def __len__(self) -> int:
        return len(self._items)


class PlaylistManager(QObject):
    current_changed = Signal(int)
    queue_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: list[VideoItem] = []
        self._current_index: int = -1
        self._mode: PlayMode = PlayMode.SEQUENTIAL
        self._history: list[int] = []

    @property
    def queue(self) -> list[VideoItem]:
        return self._queue

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def mode(self) -> PlayMode:
        return self._mode

    def set_mode(self, mode: PlayMode):
        self._mode = mode

    def set_playlist(self, items: list[VideoItem], current_url: str = ""):
        """Replace the entire queue with a new source-based playlist.

        If current_url is provided, set current_index to the matching item.
        """
        self._queue = list(items)
        self._current_index = -1
        self._history.clear()
        if current_url:
            for i, item in enumerate(self._queue):
                if item.url == current_url:
                    self._current_index = i
                    break
        self.queue_changed.emit()
        self.current_changed.emit(self._current_index)

    def add(self, item: VideoItem):
        self._queue.append(item)
        self.queue_changed.emit()

    def add_many(self, items: list[VideoItem]):
        self._queue.extend(items)
        self.queue_changed.emit()

    def remove(self, index: int):
        if 0 <= index < len(self._queue):
            self._queue.pop(index)
            if index < self._current_index:
                self._current_index -= 1
            elif index == self._current_index:
                if self._current_index >= len(self._queue):
                    self._current_index = len(self._queue) - 1
                self.current_changed.emit(self._current_index)
            self.queue_changed.emit()

    def clear(self):
        self._queue.clear()
        self._current_index = -1
        self._history.clear()
        self.queue_changed.emit()
        self.current_changed.emit(-1)

    def current(self) -> Optional[VideoItem]:
        if 0 <= self._current_index < len(self._queue):
            return self._queue[self._current_index]
        return None

    def next(self) -> Optional[VideoItem]:
        if not self._queue:
            return None

        if self._current_index >= 0:
            self._history.append(self._current_index)

        if self._mode == PlayMode.SINGLE_LOOP:
            # Stay on same item
            pass
        elif self._mode == PlayMode.SHUFFLE:
            self._current_index = random.randint(0, len(self._queue) - 1)
        elif self._mode == PlayMode.LIST_LOOP:
            self._current_index = (self._current_index + 1) % len(self._queue)
        else:
            # SEQUENTIAL
            if self._current_index + 1 >= len(self._queue):
                self.current_changed.emit(self._current_index)
                return None
            self._current_index += 1

        self.current_changed.emit(self._current_index)
        return self.current()

    def prev(self) -> Optional[VideoItem]:
        if not self._queue:
            return None

        if self._history:
            self._current_index = self._history.pop()
        else:
            self._current_index = max(0, self._current_index - 1)

        self.current_changed.emit(self._current_index)
        return self.current()

    def set_current(self, index: int):
        if 0 <= index < len(self._queue):
            if self._current_index >= 0:
                self._history.append(self._current_index)
            self._current_index = index
            self.current_changed.emit(self._current_index)

    def move(self, from_idx: int, to_idx: int):
        if from_idx == to_idx:
            return
        if not (0 <= from_idx < len(self._queue)):
            return
        if not (0 <= to_idx < len(self._queue)):
            return

        item = self._queue.pop(from_idx)
        self._queue.insert(to_idx, item)

        # Update current_index to follow the current item
        if self._current_index == from_idx:
            self._current_index = to_idx
        elif from_idx < self._current_index <= to_idx:
            self._current_index -= 1
        elif to_idx <= self._current_index < from_idx:
            self._current_index += 1

        self.queue_changed.emit()

    def contains_url(self, url: str) -> bool:
        return any(item.url == url for item in self._queue)

    def reorder(self, new_indices: list[int]):
        """Reorder the queue according to new_indices (a permutation of current indices)."""
        if sorted(new_indices) != list(range(len(self._queue))):
            return
        old_queue = self._queue
        self._queue = [old_queue[i] for i in new_indices]
        if 0 <= self._current_index < len(old_queue):
            current_item = old_queue[self._current_index]
            self._current_index = self._queue.index(current_item)
        self.queue_changed.emit()

    def __len__(self) -> int:
        return len(self._queue)
