"""应用配置持久化：DEFAULT_CONFIG + 防抖写入 ~/.ventiplayer/config.json。"""
import json
import threading
from pathlib import Path

DEFAULT_CONFIG = {
    "audio_exclusive": True,
    "audio_device": "auto",
    "apollo_enabled": False,    # Apollo codec-artifact repair (44.1kHz)
    "flashsr_enabled": False,   # FlashSR sample-rate super-resolution (48kHz)
    "apollo_fp16": False,       # Apollo fp16 autocast inference
    "flashsr_fp16": False,      # FlashSR fp16 autocast inference
    "cookie_file": "",
    "cookie_browser": "edge",  # e.g. "chrome", "firefox", "edge"
    "volume": 100,
    "last_url": "",
    "thumbnail_mode": False,
    "thumbnail_size": 80,
    "llm_providers": [],
    "llm_default_provider": "",
    "subtitle_language": "zh",
    "subtitle_model": "openai/whisper-large-v3",
    "lossless_scaling_path": "",
    "lossless_scaling_hotkey": "ctrl+alt+s",
    "export_last_dir": "",          # 记住上次视频导出目录
    "export_video_codec": "libx264",  # 导出视频编码器（预留）
}

CONFIG_PATH = Path.home() / ".ventiplayer" / "config.json"

_SAVE_DELAY = 1.0  # seconds


class Settings:
    def __init__(self):
        self._data = dict(DEFAULT_CONFIG)
        self._dirty = False
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._data.update(saved)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        self._dirty = False

    def get(self, key: str):
        return self._data.get(key, DEFAULT_CONFIG.get(key))

    def set(self, key: str, value):
        self._data[key] = value
        self._schedule_save()

    def _schedule_save(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._dirty = True
            self._timer = threading.Timer(_SAVE_DELAY, self.save)
            self._timer.daemon = True
            self._timer.start()

    def flush(self):
        """Force immediate save if dirty (call on app exit)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._dirty:
            self.save()
