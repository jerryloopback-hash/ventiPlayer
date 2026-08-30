"""Asynchronous thumbnail downloader with in-memory LRU cache."""

import logging
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import QObject, Signal, Qt, QPoint
from PySide6.QtGui import QPixmap, QImage, QColor, QPainter

logger = logging.getLogger(__name__)

_THUMBNAIL_SIZE = (160, 90)  # fixed canvas size (16:9) — all output images are exactly this size
_MAX_CACHE_SIZE = 100
_CANVAS_BG = QColor(0x1A, 0x1A, 0x1A)  # dark gray background for letterbox areas


class ThumbnailCache(QObject):
    """Downloads and caches video thumbnails asynchronously.

    Usage:
        cache = ThumbnailCache()
        cache.thumbnail_ready.connect(on_thumbnail)
        pixmap = cache.request(url)
        if pixmap is None:
            # Will arrive via thumbnail_ready signal later
    """

    thumbnail_ready = Signal(str, QPixmap)  # url, pixmap
    _image_downloaded = Signal(str, QImage)  # internal: url, image (thread-safe delivery)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._pending: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._image_downloaded.connect(self._on_image_downloaded)

    def request(self, url: str) -> QPixmap | None:
        """Return cached pixmap if available, else start async download.

        Returns None if not cached (will emit thumbnail_ready when done).
        """
        if not url:
            return None

        if url in self._cache:
            self._cache.move_to_end(url)
            return self._cache[url]

        if url not in self._pending:
            self._pending.add(url)
            self._pool.submit(self._download, url)

        return None

    def _download(self, url: str):
        """Download, decode, and normalize thumbnail to fixed canvas size."""
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()

            image = QImage()
            if not image.loadFromData(data):
                logger.debug("Failed to decode image from %s", url)
                self._pending.discard(url)
                return

            # Scale source image to fit within the fixed canvas, maintaining aspect ratio
            canvas_w, canvas_h = _THUMBNAIL_SIZE
            scaled = image.scaled(
                canvas_w,
                canvas_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

            # Create fixed-size canvas and center the scaled image on it
            canvas = QImage(canvas_w, canvas_h, QImage.Format.Format_ARGB32_Premultiplied)
            canvas.fill(_CANVAS_BG)

            x_offset = (canvas_w - scaled.width()) // 2
            y_offset = (canvas_h - scaled.height()) // 2

            painter = QPainter(canvas)
            painter.drawImage(QPoint(x_offset, y_offset), scaled)
            painter.end()

            self._image_downloaded.emit(url, canvas)

        except Exception as e:
            logger.debug("Thumbnail download failed for %s: %s", url, e)
            self._pending.discard(url)

    def _on_image_downloaded(self, url: str, image: QImage):
        """Convert QImage to QPixmap on the main thread and cache it."""
        pixmap = QPixmap.fromImage(image)
        self._cache[url] = pixmap
        if len(self._cache) > _MAX_CACHE_SIZE:
            self._cache.popitem(last=False)
        self._pending.discard(url)
        self.thumbnail_ready.emit(url, pixmap)

    def clear(self):
        """Clear all cached thumbnails."""
        self._cache.clear()
        self._pending.clear()

    def shutdown(self):
        """Shutdown the thread pool (call on application exit)."""
        self._pool.shutdown(wait=False)
