"""Settings dialog for VentiPlayer."""

import threading
from pathlib import Path
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QCheckBox, QFileDialog, QMessageBox,
    QSlider, QComboBox, QLineEdit, QFormLayout, QScrollArea, QWidget,
)
from PySide6.QtCore import Signal, Slot, Qt

from src.config.settings import Settings
from src.core.asr_engine import scan_whisper_models, KNOWN_MODELS, download_whisper_model


class SettingsDialog(QDialog):
    """Application settings dialog with cookie, display, LLM, and ASR sections."""

    cookie_imported = Signal(str)
    thumbnail_mode_changed = Signal(bool)
    thumbnail_size_changed = Signal(int)
    llm_config_changed = Signal()
    lossless_scaling_changed = Signal()
    export_video_requested = Signal()
    _llm_test_done = Signal(bool, str)
    _model_download_done = Signal(bool, str)

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(480)
        self.setMinimumHeight(500)
        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(12)

        # --- Cookie section ---
        cookie_group = QGroupBox("Cookie 设置")
        cookie_layout = QVBoxLayout(cookie_group)

        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self._cookie_label = QLabel("Cookie 文件:")
        cookie_file = self._settings.get("cookie_file")
        if cookie_file:
            self._cookie_status_label = QLabel(Path(cookie_file).name)
            self._cookie_status_label.setStyleSheet("color: green;")
        else:
            self._cookie_status_label = QLabel("未配置")
            self._cookie_status_label.setStyleSheet("color: gray;")
        status_row.addWidget(self._cookie_label)
        status_row.addWidget(self._cookie_status_label, 1)
        cookie_layout.addLayout(status_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._cookie_btn = QPushButton("导入...")
        self._cookie_btn.setToolTip("导入 cookies.txt (Netscape 格式)")
        self._cookie_btn.clicked.connect(self._on_import_cookie)
        self._cookie_help_btn = QPushButton("帮助")
        self._cookie_help_btn.setToolTip("查看 Cookie 导出教程")
        self._cookie_help_btn.clicked.connect(self._on_cookie_help)
        btn_row.addWidget(self._cookie_btn)
        btn_row.addWidget(self._cookie_help_btn)
        btn_row.addStretch()
        cookie_layout.addLayout(btn_row)

        layout.addWidget(cookie_group)

        # --- Display section ---
        display_group = QGroupBox("显示设置")
        display_layout = QVBoxLayout(display_group)

        self._thumbnail_check = QCheckBox("启用缩略图模式")
        self._thumbnail_check.setChecked(self._settings.get("thumbnail_mode"))
        self._thumbnail_check.toggled.connect(self._on_thumbnail_toggled)
        display_layout.addWidget(self._thumbnail_check)

        size_row = QHBoxLayout()
        size_row.setSpacing(6)
        size_row.addWidget(QLabel("缩略图大小"))
        self._size_slider = QSlider(Qt.Orientation.Horizontal)
        self._size_slider.setRange(60, 160)
        self._size_slider.setValue(self._settings.get("thumbnail_size"))
        self._size_slider.setFixedWidth(120)
        self._size_slider.valueChanged.connect(self._on_size_slider_changed)
        size_row.addWidget(self._size_slider)
        w = self._settings.get("thumbnail_size")
        h = round(w * 9 / 16)
        self._size_value_label = QLabel(f"{w}×{h}")
        self._size_value_label.setFixedWidth(50)
        size_row.addWidget(self._size_value_label)
        size_row.addStretch()
        display_layout.addLayout(size_row)

        self._size_slider.setEnabled(self._thumbnail_check.isChecked())
        self._size_value_label.setEnabled(self._thumbnail_check.isChecked())

        layout.addWidget(display_group)

        # --- LLM section ---
        llm_group = QGroupBox("LLM 设置")
        llm_layout = QVBoxLayout(llm_group)

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("服务商:"))
        self._llm_combo = QComboBox()
        self._llm_combo.setMinimumWidth(180)
        self._llm_combo.currentIndexChanged.connect(self._on_llm_provider_selected)
        provider_row.addWidget(self._llm_combo, 1)
        llm_layout.addLayout(provider_row)

        self._llm_form = QFormLayout()
        self._llm_form.setSpacing(6)
        self._llm_name_edit = QLineEdit()
        self._llm_name_edit.setPlaceholderText("例: my-gpt")
        self._llm_url_edit = QLineEdit()
        self._llm_url_edit.setPlaceholderText("https://api.openai.com/v1")
        self._llm_key_edit = QLineEdit()
        self._llm_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._llm_key_edit.setPlaceholderText("sk-...")
        self._llm_model_edit = QLineEdit()
        self._llm_model_edit.setPlaceholderText("gpt-4o-mini")
        self._llm_form.addRow("名称:", self._llm_name_edit)
        self._llm_form.addRow("Base URL:", self._llm_url_edit)
        self._llm_form.addRow("API Key:", self._llm_key_edit)
        self._llm_form.addRow("模型:", self._llm_model_edit)
        llm_layout.addLayout(self._llm_form)

        llm_btn_row = QHBoxLayout()
        self._llm_add_btn = QPushButton("新建")
        self._llm_add_btn.clicked.connect(self._on_llm_add)
        self._llm_save_btn = QPushButton("保存")
        self._llm_save_btn.clicked.connect(self._on_llm_save)
        self._llm_del_btn = QPushButton("删除")
        self._llm_del_btn.clicked.connect(self._on_llm_delete)
        self._llm_test_btn = QPushButton("测试连接")
        self._llm_test_btn.clicked.connect(self._on_llm_test)
        llm_btn_row.addWidget(self._llm_add_btn)
        llm_btn_row.addWidget(self._llm_save_btn)
        llm_btn_row.addWidget(self._llm_del_btn)
        llm_btn_row.addWidget(self._llm_test_btn)
        llm_btn_row.addStretch()
        llm_layout.addLayout(llm_btn_row)

        self._llm_status_label = QLabel("")
        llm_layout.addWidget(self._llm_status_label)

        layout.addWidget(llm_group)

        # --- ASR / Subtitle section ---
        asr_group = QGroupBox("字幕/ASR 设置")
        asr_layout = QVBoxLayout(asr_group)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Whisper 模型:"))
        self._asr_combo = QComboBox()
        self._asr_combo.setMinimumWidth(200)
        model_row.addWidget(self._asr_combo, 1)
        self._asr_refresh_btn = QPushButton("刷新")
        self._asr_refresh_btn.clicked.connect(self._refresh_asr_models)
        model_row.addWidget(self._asr_refresh_btn)
        asr_layout.addLayout(model_row)

        self._asr_info_label = QLabel(
            "选择未下载的模型时会提示下载\n"
            "模型缓存位置: ~/.cache/huggingface/"
        )
        self._asr_info_label.setStyleSheet("color: gray; font-size: 11px;")
        asr_layout.addWidget(self._asr_info_label)

        layout.addWidget(asr_group)

        # --- 帧生成 / 小黄鸭 (Lossless Scaling) section ---
        ls_group = QGroupBox("帧生成 / 小黄鸭 (Lossless Scaling)")
        ls_layout = QVBoxLayout(ls_group)

        ls_path_row = QHBoxLayout()
        ls_path_row.setSpacing(6)
        ls_path_row.addWidget(QLabel("程序路径:"))
        ls_path = self._settings.get("lossless_scaling_path") or ""
        self._ls_path_edit = QLineEdit(ls_path)
        self._ls_path_edit.setReadOnly(True)
        self._ls_path_edit.setPlaceholderText("选择 LosslessScaling.exe")
        ls_path_row.addWidget(self._ls_path_edit, 1)
        self._ls_browse_btn = QPushButton("浏览...")
        self._ls_browse_btn.clicked.connect(self._on_ls_browse)
        ls_path_row.addWidget(self._ls_browse_btn)
        ls_layout.addLayout(ls_path_row)

        self._ls_status_label = QLabel("")
        ls_layout.addWidget(self._ls_status_label)
        self._update_ls_status(ls_path)

        ls_hotkey_row = QHBoxLayout()
        ls_hotkey_row.setSpacing(6)
        ls_hotkey_row.addWidget(QLabel("缩放快捷键:"))
        self._ls_hotkey_edit = QLineEdit(self._settings.get("lossless_scaling_hotkey") or "ctrl+alt+s")
        self._ls_hotkey_edit.setPlaceholderText("ctrl+alt+s")
        self._ls_hotkey_edit.editingFinished.connect(self._on_ls_hotkey_changed)
        ls_hotkey_row.addWidget(self._ls_hotkey_edit, 1)
        ls_layout.addLayout(ls_hotkey_row)

        ls_help_label = QLabel("须与 Lossless Scaling 内设置的缩放快捷键一致")
        ls_help_label.setStyleSheet("color: gray; font-size: 11px;")
        ls_help_label.setWordWrap(True)
        ls_layout.addWidget(ls_help_label)

        layout.addWidget(ls_group)

        # --- 视频导出 section ---
        export_group = QGroupBox("视频导出")
        export_layout = QVBoxLayout(export_group)

        self._export_btn = QPushButton("导出当前视频为 MP4")
        self._export_btn.setToolTip("把当前视频连同已配置的音频/画面增强烘焙导出为 mp4")
        self._export_btn.clicked.connect(self.export_video_requested.emit)
        export_layout.addWidget(self._export_btn)

        export_hint = QLabel("需先解析一个非直播视频")
        export_hint.setStyleSheet("color: gray; font-size: 11px;")
        export_hint.setWordWrap(True)
        export_layout.addWidget(export_hint)

        layout.addWidget(export_group)

        # --- Close button ---
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        scroll.setWidget(container)
        outer.addWidget(scroll)

        # Internal signal for thread-safe LLM test result
        self._llm_test_done.connect(self._on_llm_test_result)
        self._model_download_done.connect(self._on_model_download_done)

        # Populate LLM and ASR combos
        self._populate_llm_combo()
        self._refresh_asr_models()

    # --- Cookie handlers ---

    def _on_import_cookie(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 Cookie 文件", "",
            "Cookie 文件 (*.txt);;所有文件 (*)",
        )
        if path:
            self._settings.set("cookie_file", path)
            self._settings.set("cookie_browser", "")
            self._cookie_status_label.setText(Path(path).name)
            self._cookie_status_label.setStyleSheet("color: green;")
            self.cookie_imported.emit(path)

    def _on_cookie_help(self):
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

    # --- Display handlers ---

    def _on_thumbnail_toggled(self, checked: bool):
        self._settings.set("thumbnail_mode", checked)
        self._size_slider.setEnabled(checked)
        self._size_value_label.setEnabled(checked)
        self.thumbnail_mode_changed.emit(checked)

    def _on_size_slider_changed(self, value: int):
        h = round(value * 9 / 16)
        self._size_value_label.setText(f"{value}×{h}")
        self._settings.set("thumbnail_size", value)
        self.thumbnail_size_changed.emit(value)

    # --- LLM handlers ---

    def _populate_llm_combo(self):
        self._llm_combo.blockSignals(True)
        self._llm_combo.clear()
        providers = self._settings.get("llm_providers") or []
        default_name = self._settings.get("llm_default_provider") or ""
        select_idx = 0
        for i, p in enumerate(providers):
            self._llm_combo.addItem(p.get("name", f"provider-{i}"))
            if p.get("name") == default_name:
                select_idx = i
        if providers:
            self._llm_combo.setCurrentIndex(select_idx)
            self._load_provider_fields(providers[select_idx])
        else:
            self._clear_provider_fields()
        self._llm_combo.blockSignals(False)

    def _on_llm_provider_selected(self, index: int):
        providers = self._settings.get("llm_providers") or []
        if 0 <= index < len(providers):
            self._load_provider_fields(providers[index])
            self._settings.set("llm_default_provider", providers[index].get("name", ""))
            self.llm_config_changed.emit()

    def _load_provider_fields(self, p: dict):
        self._llm_name_edit.setText(p.get("name", ""))
        self._llm_url_edit.setText(p.get("base_url", ""))
        self._llm_key_edit.setText(p.get("api_key", ""))
        self._llm_model_edit.setText(p.get("model", ""))

    def _clear_provider_fields(self):
        self._llm_name_edit.clear()
        self._llm_url_edit.clear()
        self._llm_key_edit.clear()
        self._llm_model_edit.clear()

    def _on_llm_add(self):
        self._clear_provider_fields()
        self._llm_name_edit.setFocus()
        self._llm_status_label.setText("请填写信息后点击保存")

    def _on_llm_save(self):
        name = self._llm_name_edit.text().strip()
        base_url = self._llm_url_edit.text().strip()
        api_key = self._llm_key_edit.text().strip()
        model = self._llm_model_edit.text().strip()

        if not name or not base_url or not api_key or not model:
            self._llm_status_label.setText("请填写所有字段")
            self._llm_status_label.setStyleSheet("color: red;")
            return

        provider_data = {
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "max_tokens": 4096,
            "temperature": 0.3,
        }

        providers = list(self._settings.get("llm_providers") or [])
        existing_idx = next((i for i, p in enumerate(providers) if p.get("name") == name), -1)
        if existing_idx >= 0:
            providers[existing_idx] = provider_data
        else:
            providers.append(provider_data)

        self._settings.set("llm_providers", providers)
        self._settings.set("llm_default_provider", name)
        self._populate_llm_combo()
        self._llm_status_label.setText(f"已保存: {name}")
        self._llm_status_label.setStyleSheet("color: green;")
        self.llm_config_changed.emit()

    def _on_llm_delete(self):
        name = self._llm_name_edit.text().strip()
        if not name:
            return
        providers = [p for p in (self._settings.get("llm_providers") or []) if p.get("name") != name]
        self._settings.set("llm_providers", providers)
        if providers:
            self._settings.set("llm_default_provider", providers[0].get("name", ""))
        else:
            self._settings.set("llm_default_provider", "")
        self._populate_llm_combo()
        self._llm_status_label.setText(f"已删除: {name}")
        self._llm_status_label.setStyleSheet("color: orange;")
        self.llm_config_changed.emit()

    def _on_llm_test(self):
        base_url = self._llm_url_edit.text().strip()
        api_key = self._llm_key_edit.text().strip()
        model = self._llm_model_edit.text().strip()

        if not base_url or not api_key or not model:
            self._llm_status_label.setText("请先填写 URL / Key / 模型")
            self._llm_status_label.setStyleSheet("color: red;")
            return

        self._llm_status_label.setText("正在测试...")
        self._llm_status_label.setStyleSheet("color: gray;")
        self._llm_test_btn.setEnabled(False)

        from src.core.llm import LLMClient, LLMProvider

        provider = LLMProvider(
            name="test", base_url=base_url, api_key=api_key, model=model
        )

        def _do_test():
            client = LLMClient(provider)
            success, msg = client.test_connection()
            self._llm_test_done.emit(success, msg)

        threading.Thread(target=_do_test, daemon=True).start()

    @Slot(bool, str)
    def _on_llm_test_result(self, success: bool, msg: str):
        self._llm_test_btn.setEnabled(True)
        self._llm_status_label.setText(msg)
        self._llm_status_label.setStyleSheet("color: green;" if success else "color: red;")

    # --- ASR handlers ---

    def _refresh_asr_models(self):
        self._asr_combo.blockSignals(True)
        self._asr_combo.clear()
        current_model = self._settings.get("subtitle_model") or ""

        models = scan_whisper_models()
        select_idx = 0
        for i, m in enumerate(models):
            suffix = "" if m["available"] else " (未下载)"
            self._asr_combo.addItem(f"{m['name']}{suffix}", m["model_id"])
            if m["model_id"] == current_model:
                select_idx = i

        if models:
            self._asr_combo.setCurrentIndex(select_idx)

        try:
            self._asr_combo.currentIndexChanged.disconnect(self._on_asr_model_changed)
        except (RuntimeError, TypeError):
            pass
        self._asr_combo.currentIndexChanged.connect(self._on_asr_model_changed)
        self._asr_combo.blockSignals(False)

    def _on_asr_model_changed(self, index: int):
        model_id = self._asr_combo.itemData(index)
        if not model_id:
            return

        # Check if this model is available locally
        models = scan_whisper_models()
        model_info = next((m for m in models if m["model_id"] == model_id), None)

        if model_info and not model_info["available"]:
            reply = QMessageBox.question(
                self,
                "模型未下载",
                f"模型 {model_info['name']} 尚未下载。\n\n"
                f"是否从 HuggingFace 下载？\n"
                f"（模型 ID: {model_id}）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._start_model_download(model_id)
            else:
                # Revert to previous selection
                self._asr_combo.blockSignals(True)
                prev_model = self._settings.get("subtitle_model") or ""
                for i in range(self._asr_combo.count()):
                    if self._asr_combo.itemData(i) == prev_model:
                        self._asr_combo.setCurrentIndex(i)
                        break
                self._asr_combo.blockSignals(False)
                return

        self._settings.set("subtitle_model", model_id)

    def _start_model_download(self, model_id: str):
        """Start downloading a model in a background thread."""
        self._asr_combo.setEnabled(False)
        self._asr_refresh_btn.setEnabled(False)
        self._asr_info_label.setText(f"正在下载 {model_id}...")
        self._asr_info_label.setStyleSheet("color: blue; font-size: 11px;")

        def _do_download():
            success = download_whisper_model(model_id)
            msg = f"{model_id} 下载完成" if success else f"{model_id} 下载失败"
            self._model_download_done.emit(success, msg)

        threading.Thread(target=_do_download, daemon=True).start()

    @Slot(bool, str)
    def _on_model_download_done(self, success: bool, msg: str):
        """Handle model download completion (runs on UI thread via signal)."""
        self._asr_combo.setEnabled(True)
        self._asr_refresh_btn.setEnabled(True)
        self._asr_info_label.setStyleSheet("color: gray; font-size: 11px;")

        if success:
            self._asr_info_label.setText(msg)
            self._refresh_asr_models()
            # Select the newly downloaded model
            for i in range(self._asr_combo.count()):
                text = self._asr_combo.itemText(i)
                if "(未下载)" not in text:
                    data = self._asr_combo.itemData(i)
                    if data:
                        self._asr_combo.setCurrentIndex(i)
                        self._settings.set("subtitle_model", data)
                        break
        else:
            self._asr_info_label.setText(msg)
            QMessageBox.warning(self, "下载失败", msg)

    # --- Lossless Scaling handlers ---

    def _update_ls_status(self, path: str):
        """刷新小黄鸭路径状态标签：绿=已配置且存在 / 灰=未配置 / 红=路径无效。"""
        if not path:
            self._ls_status_label.setText("未配置")
            self._ls_status_label.setStyleSheet("color: gray;")
            return
        p = Path(path)
        if p.is_file() and p.name.lower() == "losslessscaling.exe":
            self._ls_status_label.setText(p.name)
            self._ls_status_label.setStyleSheet("color: green;")
        else:
            self._ls_status_label.setText("路径无效或文件不存在")
            self._ls_status_label.setStyleSheet("color: red;")

    def _on_ls_browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Lossless Scaling 可执行文件", "",
            "LosslessScaling.exe (LosslessScaling.exe);;可执行文件 (*.exe)",
        )
        if path:
            self._settings.set("lossless_scaling_path", path)
            self._ls_path_edit.setText(path)
            self._update_ls_status(path)
            self.lossless_scaling_changed.emit()

    def _on_ls_hotkey_changed(self):
        text = self._ls_hotkey_edit.text().strip().lower() or "ctrl+alt+s"
        self._ls_hotkey_edit.setText(text)
        self._settings.set("lossless_scaling_hotkey", text)
        self.lossless_scaling_changed.emit()
