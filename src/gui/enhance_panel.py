"""音频增强面板：Apollo/FlashSR 独立勾选 + fp16 子选项 + 修复按钮 + 进度条 + 开源方案介绍。"""
from PySide6.QtWidgets import (
    QWidget, QGroupBox, QVBoxLayout, QHBoxLayout,
    QCheckBox, QLabel,
    QProgressBar, QPushButton,
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QPainter, QColor


class PlaybackMarkerProgressBar(QProgressBar):
    """Progress bar with a red dot indicating current playback position."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._playback_ratio = -1.0  # -1 means hidden

    def set_playback_ratio(self, ratio: float):
        """Set playback position as 0.0-1.0 ratio, or -1 to hide."""
        self._playback_ratio = ratio
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._playback_ratio < 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = int(self._playback_ratio * self.width())
        y = self.height() // 2
        painter.setBrush(QColor(220, 40, 40))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(x - 5, y - 5, 10, 10)
        painter.end()


class EnhancePanel(QWidget):
    """Audio enhancement control panel.

    Two independent toggles (Apollo codec repair / FlashSR super-resolution).
    Either or both can be enabled; both → chained Apollo → FlashSR.
    The "修复当前音频" button kicks off background processing; original audio
    keeps playing until the repaired track is ready.
    """

    settings_changed = Signal(dict)
    enhance_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stream_ready = False     # True after a stream has been resolved
        self._apollo_available = False  # weights present
        self._flashsr_available = False
        self._high_sr = False          # source SR >= 48kHz → FlashSR pointless
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("音频增强")
        group_layout = QVBoxLayout(group)

        hint = QLabel("修复流媒体压缩损伤。可单独或同时启用；两者同开时先 Apollo 修复再 FlashSR 超分。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        group_layout.addWidget(hint)

        # Apollo toggle
        self._apollo_check = QCheckBox("编解码修复 (Apollo) — 修复高频损失/压缩伪影，保持源采样率")
        self._apollo_check.stateChanged.connect(self._on_settings_changed)
        group_layout.addWidget(self._apollo_check)

        # Apollo precision (indented under Apollo)
        self._apollo_fp16_check = QCheckBox("    └ fp16 半精度 (更快、更省显存，质量略降)")
        self._apollo_fp16_check.setEnabled(False)
        self._apollo_fp16_check.stateChanged.connect(self._on_settings_changed)
        group_layout.addWidget(self._apollo_fp16_check)

        # FlashSR toggle
        self._flashsr_check = QCheckBox("采样率超分 (FlashSR) — 重建高频并升频到 48kHz")
        self._flashsr_check.stateChanged.connect(self._on_settings_changed)
        group_layout.addWidget(self._flashsr_check)

        # FlashSR precision (indented under FlashSR)
        self._flashsr_fp16_check = QCheckBox("    └ fp16 半精度 (更快、更省显存，质量略降)")
        self._flashsr_fp16_check.setEnabled(False)
        self._flashsr_fp16_check.stateChanged.connect(self._on_settings_changed)
        group_layout.addWidget(self._flashsr_fp16_check)

        # Backend status
        backend_layout = QHBoxLayout()
        backend_layout.addWidget(QLabel("推理后端:"))
        self._backend_label = QLabel("检测中...")
        self._backend_label.setStyleSheet("color: gray;")
        backend_layout.addWidget(self._backend_label)
        backend_layout.addStretch()
        group_layout.addLayout(backend_layout)

        # Model status
        model_layout = QHBoxLayout()
        model_layout.addWidget(QLabel("模型状态:"))
        self._model_label = QLabel("未加载")
        self._model_label.setStyleSheet("color: gray;")
        model_layout.addWidget(self._model_label)
        model_layout.addStretch()
        group_layout.addLayout(model_layout)

        # Progress bar with playback marker
        self._progress = PlaybackMarkerProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%")
        self._progress.setMinimumHeight(20)
        self._progress.hide()
        group_layout.addWidget(self._progress)

        # Status message
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #666; font-size: 11px;")
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        group_layout.addWidget(self._status_label)

        # Action buttons
        btn_layout = QHBoxLayout()
        self._enhance_btn = QPushButton("修复当前音频")
        self._enhance_btn.clicked.connect(self.enhance_requested.emit)
        self._enhance_btn.setEnabled(False)
        btn_layout.addWidget(self._enhance_btn)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self.cancel_requested.emit)
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.hide()
        btn_layout.addWidget(self._cancel_btn)
        group_layout.addLayout(btn_layout)

        layout.addWidget(group)
        layout.addWidget(self._build_open_source_group())

    def _build_open_source_group(self) -> QGroupBox:
        """介绍所用的两个开源方案 (Apollo / FlashSR)，含作者、简介、特点与论文引用。"""
        group = QGroupBox("所用开源方案")
        group_layout = QVBoxLayout(group)

        intro = QLabel(
            "本播放器的音频修复能力基于以下两个开源研究成果，按下列顺序级联："
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #888; font-size: 11px;")
        group_layout.addWidget(intro)

        group_layout.addWidget(self._build_scheme_card(
            name="① Apollo — 编解码修复",
            author="作者：Kai Li、Yi Luo（腾讯 AI Lab）",
            summary=(
                "面向有损编解码（MP3/AAC 等低码率压缩）的音乐修复模型，"
                "在频域工作，通过显式的频带切分模块建模各频带间的关系，"
                "修复压缩伪影与高频损失，并保持源采样率。"
            ),
            features=(
                "特点：频带序列建模、GAN 生成式修复；保持原采样率不升频；"
                "推理高效，在低码率下相比传统 SR-GAN 方案音质更优。"
            ),
            citation=(
                "Li, K., & Luo, Y. (2024). Apollo: Band-sequence Modeling "
                "for High-Quality Audio Restoration. arXiv:2409.08514."
            ),
        ))

        group_layout.addWidget(self._build_scheme_card(
            name="② FlashSR — 采样率超分",
            author="作者：Jaekwon Im、Juhan Nam（KAIST）",
            summary=(
                "通用音频超分模型，把低采样率音频重建并升频到 48kHz。"
                "通过扩散蒸馏把多步扩散压缩为单步推理，"
                "在语音、音乐、音效等多种内容上重建高频细节。"
            ),
            features=(
                "特点：一步扩散（distillation + adversarial + DMD 蒸馏目标）、"
                "推理极快；通用于多类音频；输出统一为 48kHz。"
            ),
            citation=(
                "Im, J., & Nam, J. (2025). FlashSR: One-step Versatile Audio "
                "Super-resolution via Diffusion Distillation. arXiv:2501.10807."
            ),
        ))

        return group

    @staticmethod
    def _build_scheme_card(name: str, author: str, summary: str,
                           features: str, citation: str) -> QWidget:
        """构建单个方案介绍卡片：方案名 + 作者 + 简介 + 特点 + 论文引用。"""
        card = QWidget()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(4, 4, 4, 4)
        card_layout.setSpacing(2)

        title = QLabel(name)
        title.setStyleSheet("font-weight: bold;")
        card_layout.addWidget(title)

        author_label = QLabel(author)
        author_label.setWordWrap(True)
        author_label.setStyleSheet("color: #666; font-size: 11px;")
        card_layout.addWidget(author_label)

        summary_label = QLabel(summary)
        summary_label.setWordWrap(True)
        summary_label.setStyleSheet("font-size: 11px;")
        card_layout.addWidget(summary_label)

        features_label = QLabel(features)
        features_label.setWordWrap(True)
        features_label.setStyleSheet("color: #555; font-size: 11px;")
        card_layout.addWidget(features_label)

        citation_label = QLabel(citation)
        citation_label.setWordWrap(True)
        citation_label.setStyleSheet(
            "color: #888; font-size: 10px; font-style: italic;"
        )
        # 允许鼠标选中复制引用文本
        citation_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        card_layout.addWidget(citation_label)

        return card

    def _on_settings_changed(self):
        # fp16 checkbox is only meaningful when its model is enabled
        self._apollo_fp16_check.setEnabled(
            self._apollo_check.isChecked() and self._apollo_check.isEnabled()
        )
        self._flashsr_fp16_check.setEnabled(
            self._flashsr_check.isChecked() and self._flashsr_check.isEnabled()
        )
        self._update_enhance_btn_state()
        self.settings_changed.emit(self.get_settings())

    def get_settings(self) -> dict:
        return {
            "apollo_enabled": self._apollo_check.isChecked(),
            "flashsr_enabled": self._flashsr_check.isChecked(),
            "apollo_fp16": self._apollo_fp16_check.isChecked(),
            "flashsr_fp16": self._flashsr_fp16_check.isChecked(),
        }

    def set_backend_info(self, text: str, available: bool):
        self._backend_label.setText(text)
        self._backend_label.setStyleSheet(
            "color: green;" if available else "color: orange;"
        )

    def set_model_status(self, text: str, loaded: bool):
        self._model_label.setText(text)
        self._model_label.setStyleSheet(
            "color: green;" if loaded else "color: gray;"
        )

    def set_models_available(self, apollo: bool, flashsr: bool):
        """Gate each checkbox by whether its weights are present."""
        self._apollo_available = apollo
        self._flashsr_available = flashsr
        self._refresh_checkbox_state()

    def set_enhance_enabled(self, enabled: bool):
        self._stream_ready = enabled
        self._update_enhance_btn_state()

    def set_enhance_blocked(self, blocked: bool):
        """Source SR >= 48kHz: FlashSR has nothing to super-resolve, so disable
        it. Apollo (artifact repair) still applies, so it stays available."""
        self._high_sr = blocked
        self._refresh_checkbox_state()

    def _refresh_checkbox_state(self):
        # Apollo: enabled iff weights present
        apollo_ok = self._apollo_available
        self._apollo_check.setEnabled(apollo_ok)
        if not apollo_ok and self._apollo_check.isChecked():
            self._apollo_check.setChecked(False)

        # FlashSR: enabled iff weights present AND source SR not already high
        flashsr_ok = self._flashsr_available and not self._high_sr
        self._flashsr_check.setEnabled(flashsr_ok)
        if not flashsr_ok and self._flashsr_check.isChecked():
            self._flashsr_check.setChecked(False)

        suffix = " (源采样率已达 48kHz)" if self._high_sr else ""
        self._flashsr_check.setText(
            "采样率超分 (FlashSR) — 重建高频并升频到 48kHz" + suffix
        )
        # Keep fp16 gates in sync with their parent model's enabled+checked state
        self._apollo_fp16_check.setEnabled(
            self._apollo_check.isChecked() and self._apollo_check.isEnabled()
        )
        self._flashsr_fp16_check.setEnabled(
            self._flashsr_check.isChecked() and self._flashsr_check.isEnabled()
        )
        self._update_enhance_btn_state()

    def _update_enhance_btn_state(self):
        """Enable run button only when a stream is ready and >=1 model selected."""
        any_selected = (
            (self._apollo_check.isChecked() and self._apollo_check.isEnabled())
            or (self._flashsr_check.isChecked() and self._flashsr_check.isEnabled())
        )
        self._enhance_btn.setEnabled(self._stream_ready and any_selected)

    def show_progress(self, visible: bool):
        self._progress.setVisible(visible)
        self._status_label.setVisible(visible)
        self._cancel_btn.setVisible(visible)
        self._cancel_btn.setEnabled(visible)
        if visible:
            self._enhance_btn.setText("重新修复")
        else:
            self._enhance_btn.setText("修复当前音频")
            self._progress.setValue(0)
            self._progress.set_playback_ratio(-1.0)
            self._status_label.setText("")

    def update_progress(self, progress: float, message: str = ""):
        self._progress.setValue(int(progress * 100))
        if message:
            self._status_label.setText(message)
            self._status_label.show()

    def update_playback_marker(self, ratio: float):
        """Update the red dot position on the progress bar (0.0-1.0)."""
        self._progress.set_playback_ratio(ratio)
