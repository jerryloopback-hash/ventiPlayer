"""MainWindow 应用级初始化与设置接线 mixin：mpv 初始化、设备/cookie 刷新、
SyncManager 配线、设置对话框入口、缩略图与 cookie 导入回调。

从 main_window.py 拆出；self 即 MainWindow 实例。
"""

import logging
import threading

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

from src.gui.settings_dialog import SettingsDialog

from src.core.stream import check_cookie_status, CookieStatus

logger = logging.getLogger(__name__)


class AppInitMixin:
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
