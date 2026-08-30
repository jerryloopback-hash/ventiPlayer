"""MainWindow 字幕管线 mixin：Whisper+LLM 字幕生成按钮状态、生成请求、状态处理。

从 main_window.py 拆出；self 即 MainWindow 实例。
"""

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QMessageBox

from src.core.subtitle import SubtitlePipeline, SubtitleStatus, extract_video_id
from src.core.llm import provider_from_dict


class SubtitleMixin:
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
