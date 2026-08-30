from pathlib import Path
from PySide6.QtWidgets import (
    QWidget, QGroupBox, QVBoxLayout, QHBoxLayout,
    QCheckBox, QLabel, QSlider, QPushButton, QSpinBox,
    QComboBox, QStackedWidget,
)
from PySide6.QtCore import Signal, Qt

SHADERS_DIR = Path(__file__).resolve().parent.parent.parent / "shaders"
_CAS_TEMPLATE = SHADERS_DIR / "CAS.glsl"
_CAS_GENERATED = SHADERS_DIR / "CAS_active.glsl"
_FSR_TEMPLATE = SHADERS_DIR / "FSR.glsl"
_FSR_GENERATED = SHADERS_DIR / "FSR_active.glsl"

# Anime4K v4.x preset system
# Modes: A/B/C/A+A/B+B/C+A (content-type optimized)
# Quality: 快速(S) / 均衡(M) / 质量(L) / 极致(VL) — maps to shader variant sizes
# Scale: x2 (single pass) / x4 (two chained x2 passes)

_ANIME4K_RESTORE = {
    "A": {"S": "Anime4K_Restore_CNN_S.glsl", "M": "Anime4K_Restore_CNN_M.glsl",
           "L": "Anime4K_Restore_CNN_L.glsl", "VL": "Anime4K_Restore_CNN_VL.glsl",
           "UL": "Anime4K_Restore_CNN_UL.glsl"},
    "B": {"S": "Anime4K_Restore_CNN_Soft_S.glsl", "M": "Anime4K_Restore_CNN_Soft_M.glsl",
           "L": "Anime4K_Restore_CNN_Soft_L.glsl", "VL": "Anime4K_Restore_CNN_Soft_VL.glsl",
           "UL": "Anime4K_Restore_CNN_Soft_UL.glsl"},
    "C": {},  # Mode C uses Upscale_Denoise instead of separate Restore
}

_ANIME4K_UPSCALE = {
    "S": "Anime4K_Upscale_CNN_x2_S.glsl",
    "M": "Anime4K_Upscale_CNN_x2_M.glsl",
    "L": "Anime4K_Upscale_CNN_x2_L.glsl",
    "VL": "Anime4K_Upscale_CNN_x2_VL.glsl",
    "UL": "Anime4K_Upscale_CNN_x2_UL.glsl",
}

_ANIME4K_UPSCALE_DENOISE = {
    "S": "Anime4K_Upscale_Denoise_CNN_x2_S.glsl",
    "M": "Anime4K_Upscale_Denoise_CNN_x2_M.glsl",
    "L": "Anime4K_Upscale_Denoise_CNN_x2_L.glsl",
    "VL": "Anime4K_Upscale_Denoise_CNN_x2_VL.glsl",
    "UL": "Anime4K_Upscale_Denoise_CNN_x2_UL.glsl",
}

# Quality tier mapping: display name → shader variant size
_ANIME4K_QUALITY_MAP = {
    "快速": "S",
    "均衡": "M",
    "质量": "L",
    "极致": "VL",
    "极限 (性能开销极大)": "UL",
}

_ANIME4K_MODES = ["A", "B", "C", "A+A", "B+B", "C+A"]
_ANIME4K_QUALITIES = list(_ANIME4K_QUALITY_MAP.keys())
_ANIME4K_SCALES = ["x2", "x4"]

_ANIME4K_MODE_DESC = {
    "A": "1080p动画 / 高模糊源",
    "B": "720p动画 / 低模糊源",
    "C": "480p动画 / 无退化源",
    "A+A": "A加强版 (双重修复+超分)",
    "B+B": "B加强版 (双重修复+超分)",
    "C+A": "C+A (去噪超分+修复超分)",
}

_ANIME4K_QUALITY_DESC = {
    "快速": "S档 — 最快，适合低端GPU",
    "均衡": "M档 — 速度与质量平衡",
    "质量": "L档 — 高质量，需中端GPU",
    "极致": "VL档 — 最高质量，需高端GPU",
    "极限 (性能开销极大)": "UL档 — 极限质量，性能开销约为VL的两倍",
}

_HDR_TONE_MAPPING_ALGORITHMS = [
    "auto", "bt.2390", "spline", "mobius", "reinhard", "hable", "clip",
]

_TSCALE_DESC = {
    "oversample": "最近邻，无混合，保持锐利",
    "triangle": "线性混合，锐利但有轻微重影",
    "mitchell": "平滑但可能糊",
    "gaussian": "高斯混合，较平滑",
    "bicubic": "三次混合，最模糊",
    "sphinx": "Sphinx窗口函数",
}


class VideoEnhancePanel(QWidget):
    """视频画面增强控制面板"""

    property_changed = Signal(str, object)
    shader_changed = Signal(list)
    deband_changed = Signal(bool, dict)
    vf_changed = Signal(str)
    hdr_changed = Signal(bool, dict)
    upscale_factor_changed = Signal(int)  # 1 = off, 2 = x2, 4 = x4
    frame_gen_changed = Signal(bool, dict)  # (enabled, {backend, tscale, threshold})

    def __init__(self, parent=None, frame_gen_caps: dict | None = None):
        super().__init__(parent)
        # 帧生成依赖能力（由 main_window 传入 FrameGenManager.check_dependencies() 结果）。
        # 缺省给一个小黄鸭不可用的安全值，仅 display-resample 可用。
        self._fg_caps = frame_gen_caps or {
            "display_resample": True,
            "lossless_scaling": {"available": False, "reason": "", "exe_path": ""},
        }
        self._setup_ui()
        self._refresh_dep_hint()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("视频增强")
        group_layout = QHBoxLayout(group)
        group_layout.setSpacing(12)

        # --- Left column: basic adjustments, CAS, deband, denoise, HDR ---
        left_col = QVBoxLayout()
        left_col.setSpacing(6)

        # --- Basic adjustments ---
        self._enable_basic = QCheckBox("基础画面调整")
        self._enable_basic.toggled.connect(self._on_basic_toggled)
        left_col.addWidget(self._enable_basic)

        self._basic_widget = QWidget()
        basic_layout = QVBoxLayout(self._basic_widget)
        basic_layout.setContentsMargins(16, 0, 0, 0)
        basic_layout.setSpacing(4)

        self._sliders = {}
        slider_defs = [
            ("brightness", "亮度", -100, 100, 0),
            ("contrast", "对比度", -100, 100, 0),
            ("saturation", "饱和度", -100, 100, 0),
            ("gamma", "Gamma", -100, 100, 0),
        ]
        for prop, label, lo, hi, default in slider_defs:
            row = QHBoxLayout()
            lbl = QLabel(f"{label}:")
            lbl.setFixedWidth(52)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(lo, hi)
            slider.setValue(default)
            slider.setTickPosition(QSlider.TickPosition.NoTicks)
            val_label = QLabel(str(default))
            val_label.setFixedWidth(32)
            val_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            slider.valueChanged.connect(
                lambda v, p=prop, vl=val_label: self._on_slider_changed(p, v, vl)
            )
            row.addWidget(lbl)
            row.addWidget(slider, 1)
            row.addWidget(val_label)
            basic_layout.addLayout(row)
            self._sliders[prop] = (slider, val_label)

        left_col.addWidget(self._basic_widget)
        self._basic_widget.setEnabled(False)

        # --- Sharpening (CAS shader) ---
        self._enable_sharpen = QCheckBox("锐化 (CAS)")
        self._enable_sharpen.toggled.connect(self._on_sharpen_toggled)
        left_col.addWidget(self._enable_sharpen)

        self._sharpen_widget = QWidget()
        sharpen_layout = QHBoxLayout(self._sharpen_widget)
        sharpen_layout.setContentsMargins(16, 0, 0, 0)
        lbl = QLabel("强度:")
        lbl.setFixedWidth(52)
        self._sharpen_slider = QSlider(Qt.Orientation.Horizontal)
        self._sharpen_slider.setRange(0, 10)
        self._sharpen_slider.setValue(6)
        self._sharpen_val = QLabel("0.6")
        self._sharpen_val.setFixedWidth(32)
        self._sharpen_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._sharpen_slider.valueChanged.connect(self._on_sharpen_strength_changed)
        sharpen_layout.addWidget(lbl)
        sharpen_layout.addWidget(self._sharpen_slider, 1)
        sharpen_layout.addWidget(self._sharpen_val)
        left_col.addWidget(self._sharpen_widget)
        self._sharpen_widget.setEnabled(False)

        # --- Deband ---
        self._enable_deband = QCheckBox("去色带 (Deband)")
        self._enable_deband.toggled.connect(self._on_deband_toggled)
        left_col.addWidget(self._enable_deband)

        self._deband_widget = QWidget()
        deband_layout = QVBoxLayout(self._deband_widget)
        deband_layout.setContentsMargins(16, 0, 0, 0)
        deband_layout.setSpacing(4)

        row_iter = QHBoxLayout()
        row_iter.addWidget(QLabel("迭代:"))
        self._deband_iterations = QSpinBox()
        self._deband_iterations.setRange(1, 8)
        self._deband_iterations.setValue(2)
        self._deband_iterations.valueChanged.connect(self._emit_deband)
        row_iter.addWidget(self._deband_iterations)
        row_iter.addStretch()
        deband_layout.addLayout(row_iter)

        row_thresh = QHBoxLayout()
        row_thresh.addWidget(QLabel("阈值:"))
        self._deband_threshold = QSlider(Qt.Orientation.Horizontal)
        self._deband_threshold.setRange(16, 128)
        self._deband_threshold.setValue(48)
        self._deband_thresh_val = QLabel("48")
        self._deband_thresh_val.setFixedWidth(32)
        self._deband_threshold.valueChanged.connect(
            lambda v: (self._deband_thresh_val.setText(str(v)), self._emit_deband())
        )
        row_thresh.addWidget(self._deband_threshold, 1)
        row_thresh.addWidget(self._deband_thresh_val)
        deband_layout.addLayout(row_thresh)

        row_range = QHBoxLayout()
        row_range.addWidget(QLabel("范围:"))
        self._deband_range = QSlider(Qt.Orientation.Horizontal)
        self._deband_range.setRange(4, 64)
        self._deband_range.setValue(16)
        self._deband_range_val = QLabel("16")
        self._deband_range_val.setFixedWidth(32)
        self._deband_range.valueChanged.connect(
            lambda v: (self._deband_range_val.setText(str(v)), self._emit_deband())
        )
        row_range.addWidget(self._deband_range, 1)
        row_range.addWidget(self._deband_range_val)
        deband_layout.addLayout(row_range)

        left_col.addWidget(self._deband_widget)
        self._deband_widget.setEnabled(False)

        # --- Denoise ---
        self._enable_denoise = QCheckBox("降噪")
        self._enable_denoise.toggled.connect(self._on_denoise_toggled)
        left_col.addWidget(self._enable_denoise)

        self._denoise_widget = QWidget()
        denoise_layout = QVBoxLayout(self._denoise_widget)
        denoise_layout.setContentsMargins(16, 0, 0, 0)
        denoise_layout.setSpacing(4)

        row_mode = QHBoxLayout()
        row_mode.addWidget(QLabel("算法:"))
        self._denoise_mode = QComboBox()
        self._denoise_mode.addItems(["hqdn3d", "nlmeans"])
        self._denoise_mode.currentIndexChanged.connect(self._emit_vf)
        row_mode.addWidget(self._denoise_mode)
        row_mode.addStretch()
        denoise_layout.addLayout(row_mode)

        row_strength = QHBoxLayout()
        row_strength.addWidget(QLabel("强度:"))
        self._denoise_strength = QSlider(Qt.Orientation.Horizontal)
        self._denoise_strength.setRange(1, 10)
        self._denoise_strength.setValue(4)
        self._denoise_strength_val = QLabel("4")
        self._denoise_strength_val.setFixedWidth(20)
        self._denoise_strength.valueChanged.connect(
            lambda v: (self._denoise_strength_val.setText(str(v)), self._emit_vf())
        )
        row_strength.addWidget(self._denoise_strength, 1)
        row_strength.addWidget(self._denoise_strength_val)
        denoise_layout.addLayout(row_strength)

        left_col.addWidget(self._denoise_widget)
        self._denoise_widget.setEnabled(False)

        # --- HDR Tone Mapping ---
        self._enable_hdr = QCheckBox("HDR 色调映射")
        self._enable_hdr.toggled.connect(self._on_hdr_toggled)
        left_col.addWidget(self._enable_hdr)

        self._hdr_widget = QWidget()
        hdr_layout = QVBoxLayout(self._hdr_widget)
        hdr_layout.setContentsMargins(16, 0, 0, 0)
        hdr_layout.setSpacing(4)

        row_algo = QHBoxLayout()
        row_algo.addWidget(QLabel("算法:"))
        self._hdr_algo = QComboBox()
        self._hdr_algo.addItems(_HDR_TONE_MAPPING_ALGORITHMS)
        self._hdr_algo.setCurrentIndex(1)
        self._hdr_algo.currentIndexChanged.connect(self._emit_hdr)
        row_algo.addWidget(self._hdr_algo)
        row_algo.addStretch()
        hdr_layout.addLayout(row_algo)

        self._hdr_peak = QCheckBox("动态峰值检测")
        self._hdr_peak.setChecked(True)
        self._hdr_peak.toggled.connect(self._emit_hdr)
        hdr_layout.addWidget(self._hdr_peak)

        left_col.addWidget(self._hdr_widget)
        self._hdr_widget.setEnabled(False)

        left_col.addStretch()
        group_layout.addLayout(left_col, 1)

        # --- Right column: super-resolution + future interpolation ---
        right_col = QVBoxLayout()
        right_col.setSpacing(4)

        # --- Super Resolution (unified) ---
        self._enable_upscale = QCheckBox("超分辨率")
        self._enable_upscale.toggled.connect(self._on_upscale_toggled)
        right_col.addWidget(self._enable_upscale)

        self._upscale_widget = QWidget()
        upscale_layout = QVBoxLayout(self._upscale_widget)
        upscale_layout.setContentsMargins(16, 0, 0, 0)
        upscale_layout.setSpacing(2)

        # Algorithm selector
        row_algo_up = QHBoxLayout()
        row_algo_up.addWidget(QLabel("算法:"))
        self._upscale_algo = QComboBox()
        self._upscale_algo.addItems(["Anime4K", "FSR", "FSRCNNX"])
        self._upscale_algo.currentIndexChanged.connect(self._on_upscale_algo_changed)
        row_algo_up.addWidget(self._upscale_algo, 1)
        upscale_layout.addLayout(row_algo_up)

        # Stacked widget for algorithm-specific parameters
        self._upscale_params_stack = QStackedWidget()

        # --- Anime4K params page ---
        self._anime4k_params = QWidget()
        a4k_layout = QVBoxLayout(self._anime4k_params)
        a4k_layout.setContentsMargins(0, 2, 0, 0)
        a4k_layout.setSpacing(2)

        row_mode_a4k = QHBoxLayout()
        row_mode_a4k.addWidget(QLabel("模式:"))
        self._anime4k_mode = QComboBox()
        self._anime4k_mode.addItems(_ANIME4K_MODES)
        self._anime4k_mode.currentIndexChanged.connect(self._on_anime4k_params_changed)
        row_mode_a4k.addWidget(self._anime4k_mode, 1)
        a4k_layout.addLayout(row_mode_a4k)

        self._anime4k_mode_desc = QLabel(_ANIME4K_MODE_DESC["A"])
        self._anime4k_mode_desc.setStyleSheet("color: gray; font-size: 10px;")
        self._anime4k_mode_desc.setContentsMargins(0, 0, 0, 0)
        self._anime4k_mode_desc.setWordWrap(True)
        a4k_layout.addWidget(self._anime4k_mode_desc)

        row_scale_a4k = QHBoxLayout()
        row_scale_a4k.addWidget(QLabel("倍率:"))
        self._anime4k_scale = QComboBox()
        self._anime4k_scale.addItems(_ANIME4K_SCALES)
        self._anime4k_scale.setCurrentIndex(1)  # default x4
        self._anime4k_scale.currentIndexChanged.connect(self._on_anime4k_params_changed)
        row_scale_a4k.addWidget(self._anime4k_scale, 1)
        a4k_layout.addLayout(row_scale_a4k)

        row_quality = QHBoxLayout()
        row_quality.addWidget(QLabel("质量:"))
        self._anime4k_quality = QComboBox()
        self._anime4k_quality.addItems(_ANIME4K_QUALITIES)
        self._anime4k_quality.setCurrentIndex(1)  # default 均衡
        self._anime4k_quality.currentIndexChanged.connect(self._on_anime4k_params_changed)
        row_quality.addWidget(self._anime4k_quality, 1)
        a4k_layout.addLayout(row_quality)

        self._anime4k_quality_desc = QLabel(_ANIME4K_QUALITY_DESC["均衡"])
        self._anime4k_quality_desc.setStyleSheet("color: gray; font-size: 10px;")
        self._anime4k_quality_desc.setContentsMargins(0, 0, 0, 0)
        a4k_layout.addWidget(self._anime4k_quality_desc)

        self._upscale_params_stack.addWidget(self._anime4k_params)

        # --- FSR params page ---
        self._fsr_params = QWidget()
        fsr_layout = QVBoxLayout(self._fsr_params)
        fsr_layout.setContentsMargins(0, 2, 0, 0)
        fsr_layout.setSpacing(2)

        row_fsr_sharp = QHBoxLayout()
        row_fsr_sharp.addWidget(QLabel("锐化:"))
        self._fsr_sharpness = QSlider(Qt.Orientation.Horizontal)
        self._fsr_sharpness.setRange(0, 20)
        self._fsr_sharpness.setValue(2)
        self._fsr_sharp_val = QLabel("0.2")
        self._fsr_sharp_val.setFixedWidth(32)
        self._fsr_sharp_val.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._fsr_sharpness.valueChanged.connect(self._on_fsr_params_changed)
        row_fsr_sharp.addWidget(self._fsr_sharpness, 1)
        row_fsr_sharp.addWidget(self._fsr_sharp_val)
        fsr_layout.addLayout(row_fsr_sharp)

        self._fsr_denoise = QCheckBox("RCAS 降噪")
        self._fsr_denoise.setChecked(True)
        self._fsr_denoise.toggled.connect(self._on_fsr_params_changed)
        fsr_layout.addWidget(self._fsr_denoise)

        self._upscale_params_stack.addWidget(self._fsr_params)

        # --- FSRCNNX params page (no configurable params) ---
        self._fsrcnnx_params = QWidget()
        fsrcnnx_layout = QVBoxLayout(self._fsrcnnx_params)
        fsrcnnx_layout.setContentsMargins(0, 2, 0, 0)
        fsrcnnx_info = QLabel("神经网络超分，无可调参数")
        fsrcnnx_info.setStyleSheet("color: gray; font-size: 10px;")
        fsrcnnx_layout.addWidget(fsrcnnx_info)
        self._upscale_params_stack.addWidget(self._fsrcnnx_params)

        upscale_layout.addWidget(self._upscale_params_stack)
        right_col.addWidget(self._upscale_widget)
        self._upscale_widget.setEnabled(False)

        # --- Frame Generation (display-resample / 小黄鸭) ---
        self._enable_fg = QCheckBox("帧生成 / 插帧")
        self._enable_fg.toggled.connect(self._on_fg_toggled)
        right_col.addWidget(self._enable_fg)

        self._fg_widget = QWidget()
        fg_layout = QVBoxLayout(self._fg_widget)
        fg_layout.setContentsMargins(16, 0, 0, 0)
        fg_layout.setSpacing(2)

        # backend 下拉：display-resample 伪插帧（恒可用） + 小黄鸭外部全屏补帧。
        row_backend = QHBoxLayout()
        row_backend.addWidget(QLabel("后端:"))
        self._fg_backend = QComboBox()
        self._fg_backend.addItems([
            "display-resample (伪插帧)",            # idx 0（默认，恒可用）
            "小黄鸭 (Lossless Scaling 全屏补帧)",   # idx 1
        ])
        self._fg_backend.currentIndexChanged.connect(self._on_fg_backend_changed)
        row_backend.addWidget(self._fg_backend, 1)
        fg_layout.addLayout(row_backend)

        # 依赖缺失提示（默认隐藏）
        self._fg_dep_hint = QLabel("")
        self._fg_dep_hint.setStyleSheet("color: #d08770; font-size: 10px;")
        self._fg_dep_hint.setWordWrap(True)
        self._fg_dep_hint.setVisible(False)
        fg_layout.addWidget(self._fg_dep_hint)

        # 参数页堆栈：page0=display-resample，page1=小黄鸭说明页
        self._fg_params_stack = QStackedWidget()

        # page0: display-resample（沿用旧 tscale + threshold 控件）
        page_dr = QWidget()
        dr = QVBoxLayout(page_dr)
        dr.setContentsMargins(0, 0, 0, 0)
        dr.setSpacing(2)
        row_tscale = QHBoxLayout()
        row_tscale.addWidget(QLabel("算法:"))
        self._tscale_algo = QComboBox()
        self._tscale_algo.addItems([
            "oversample", "triangle", "mitchell",
            "gaussian", "bicubic", "sphinx",
        ])
        self._tscale_algo.setCurrentIndex(0)
        self._tscale_algo.currentIndexChanged.connect(self._on_fg_params_changed)
        row_tscale.addWidget(self._tscale_algo, 1)
        dr.addLayout(row_tscale)
        self._tscale_desc = QLabel("最近邻，无混合，保持锐利")
        self._tscale_desc.setStyleSheet("color: gray; font-size: 10px;")
        self._tscale_desc.setContentsMargins(0, 0, 0, 0)
        dr.addWidget(self._tscale_desc)
        row_threshold = QHBoxLayout()
        row_threshold.addWidget(QLabel("阈值:"))
        self._interp_threshold = QSlider(Qt.Orientation.Horizontal)
        self._interp_threshold.setRange(-1, 10)
        self._interp_threshold.setValue(-1)
        self._interp_threshold_val = QLabel("-1")
        self._interp_threshold_val.setFixedWidth(24)
        self._interp_threshold_val.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._interp_threshold.valueChanged.connect(self._on_fg_params_changed)
        row_threshold.addWidget(self._interp_threshold, 1)
        row_threshold.addWidget(self._interp_threshold_val)
        dr.addLayout(row_threshold)
        self._interp_threshold_desc = QLabel("-1 = 始终插值")
        self._interp_threshold_desc.setStyleSheet("color: gray; font-size: 10px;")
        self._interp_threshold_desc.setContentsMargins(0, 0, 0, 0)
        dr.addWidget(self._interp_threshold_desc)
        self._fg_params_stack.addWidget(page_dr)  # index 0

        # page1: 小黄鸭（外部全屏补帧，仅说明文案，无可调参数）
        page_ls = QWidget()
        ls = QVBoxLayout(page_ls)
        ls.setContentsMargins(0, 0, 0, 0)
        ls.setSpacing(2)
        ls_info1 = QLabel("需在「设置」中配置 Lossless Scaling 程序路径")
        ls_info1.setStyleSheet("color: gray; font-size: 10px;")
        ls_info1.setWordWrap(True)
        ls.addWidget(ls_info1)
        ls_info2 = QLabel("进入全屏自动开启缩放，退出全屏自动关闭")
        ls_info2.setStyleSheet("color: gray; font-size: 10px;")
        ls_info2.setWordWrap(True)
        ls.addWidget(ls_info2)
        ls_info3 = QLabel("快捷键在设置中配置")
        ls_info3.setStyleSheet("color: gray; font-size: 10px;")
        ls_info3.setWordWrap(True)
        ls.addWidget(ls_info3)
        self._fg_params_stack.addWidget(page_ls)  # index 1

        fg_layout.addWidget(self._fg_params_stack)
        right_col.addWidget(self._fg_widget)
        self._fg_widget.setEnabled(False)
        self._fg_params_stack.setCurrentIndex(0)  # 默认 display-resample 页

        right_col.addStretch()

        # --- Reset button ---
        self._reset_btn = QPushButton("重置全部")
        self._reset_btn.clicked.connect(self._reset_all)
        right_col.addWidget(self._reset_btn)

        group_layout.addLayout(right_col, 1)

        layout.addWidget(group)

    # --- Callbacks ---

    def _on_basic_toggled(self, checked: bool):
        self._basic_widget.setEnabled(checked)
        if checked:
            for prop, (slider, _) in self._sliders.items():
                self.property_changed.emit(prop, slider.value())
        else:
            for prop in self._sliders:
                self.property_changed.emit(prop, 0)

    def _on_slider_changed(self, prop: str, value: int, val_label: QLabel):
        val_label.setText(str(value))
        if self._enable_basic.isChecked():
            self.property_changed.emit(prop, value)

    def _on_sharpen_toggled(self, checked: bool):
        self._sharpen_widget.setEnabled(checked)
        self._emit_all_shaders()

    def _on_sharpen_strength_changed(self, value: int):
        self._sharpen_val.setText(f"{value / 10:.1f}")
        self._emit_all_shaders()

    def _on_deband_toggled(self, checked: bool):
        self._deband_widget.setEnabled(checked)
        self._emit_deband()

    def _on_denoise_toggled(self, checked: bool):
        self._denoise_widget.setEnabled(checked)
        # 降噪与小黄鸭可同时开启（小黄鸭是外部叠加程序，不占用 mpv vf 链）。
        self._emit_vf()

    def _on_hdr_toggled(self, checked: bool):
        self._hdr_widget.setEnabled(checked)
        self._emit_hdr()

    def _on_upscale_toggled(self, checked: bool):
        self._upscale_widget.setEnabled(checked)
        self._emit_all_shaders()
        factor = self._get_current_upscale_factor() if checked else 1
        self.upscale_factor_changed.emit(factor)

    def _on_upscale_algo_changed(self, index: int):
        self._upscale_params_stack.setCurrentIndex(index)
        if self._enable_upscale.isChecked():
            self._emit_all_shaders()
            self.upscale_factor_changed.emit(self._get_current_upscale_factor())

    def _on_anime4k_params_changed(self, *_args):
        mode = self._anime4k_mode.currentText()
        quality = self._anime4k_quality.currentText()
        self._anime4k_mode_desc.setText(_ANIME4K_MODE_DESC.get(mode, ""))
        self._anime4k_quality_desc.setText(_ANIME4K_QUALITY_DESC.get(quality, ""))
        if self._enable_upscale.isChecked():
            self._emit_all_shaders()
            self.upscale_factor_changed.emit(self._get_current_upscale_factor())

    def _on_fsr_params_changed(self, *_args):
        val = self._fsr_sharpness.value() / 10.0
        self._fsr_sharp_val.setText(f"{val:.1f}")
        if self._enable_upscale.isChecked():
            self._emit_all_shaders()

    def _on_fg_toggled(self, checked: bool):
        self._fg_widget.setEnabled(checked)
        # 降噪与小黄鸭可同时开启，不再互斥。
        if checked:
            self._refresh_dep_hint()
            self._emit_frame_gen()
        else:
            self.frame_gen_changed.emit(False, {})

    def _on_fg_backend_changed(self, idx: int):
        # 0 -> display-resample 页(0); 1 -> 小黄鸭说明页(1)
        self._fg_params_stack.setCurrentIndex(1 if idx == 1 else 0)
        self._refresh_dep_hint()
        self._update_fg_descs()
        if self._enable_fg.isChecked():
            self._emit_frame_gen()

    def _on_fg_params_changed(self, *_args):
        self._update_fg_descs()
        if self._enable_fg.isChecked():
            self._emit_frame_gen()

    def _update_fg_descs(self):
        """同步 display-resample 页的 tscale_desc / threshold 文案。"""
        tscale = self._tscale_algo.currentText()
        self._tscale_desc.setText(_TSCALE_DESC.get(tscale, ""))
        threshold = self._interp_threshold.value()
        self._interp_threshold_val.setText(str(threshold))
        if threshold == -1:
            self._interp_threshold_desc.setText("-1 = 始终插值")
        elif threshold == 0:
            self._interp_threshold_desc.setText("0 = 仅帧率差距大时插值")
        else:
            self._interp_threshold_desc.setText(f"帧率差>{threshold}x时插值")

    def _refresh_dep_hint(self):
        """根据依赖能力灰显小黄鸭项，并显示原因提示。"""
        caps = self._fg_caps
        ls_ok = caps.get("lossless_scaling", {}).get("available", False)
        model = self._fg_backend.model()
        self._set_item_enabled(model, 1, ls_ok)   # 小黄鸭

        # 若当前选中小黄鸭但不可用，回落到 display-resample(idx0)
        bidx = self._fg_backend.currentIndex()
        if bidx == 1 and not ls_ok:
            self._fg_backend.setCurrentIndex(0)
            bidx = 0

        if not ls_ok:
            reason = caps.get("lossless_scaling", {}).get("reason") or "依赖未就绪"
            self._fg_dep_hint.setText(f"小黄鸭不可用：{reason}")
            self._fg_dep_hint.setVisible(True)
        else:
            self._fg_dep_hint.setVisible(False)

    @staticmethod
    def _set_item_enabled(combo_model, row: int, enabled: bool):
        """把 QComboBox 底层 model 的某一项置灰/恢复。"""
        item = combo_model.item(row)
        if item is not None:
            item.setEnabled(enabled)

    _BACKEND_KEY = {0: "display-resample", 1: "lossless-scaling"}

    def _emit_frame_gen(self):
        enabled = self._enable_fg.isChecked()
        bidx = self._fg_backend.currentIndex()
        backend = self._BACKEND_KEY.get(bidx, "display-resample")
        if backend == "lossless-scaling":
            params = {"backend": "lossless-scaling"}
        else:
            params = {
                "backend":   "display-resample",
                "tscale":    self._tscale_algo.currentText(),
                "threshold": self._interp_threshold.value(),
            }
        self.frame_gen_changed.emit(enabled, params)

    def _build_shader_list(self) -> list[str]:
        """构建 CAS + 超分着色器的完整路径列表（不发信号）。

        从 _emit_all_shaders 中抽出，便于导出模块离线复用同一条着色器链。
        """
        shaders = []
        if self._enable_sharpen.isChecked():
            cas_path = self._generate_cas_shader()
            if cas_path:
                shaders.append(cas_path)
        if self._enable_upscale.isChecked():
            algo = self._upscale_algo.currentText()
            if algo == "Anime4K":
                shaders.extend(self._get_anime4k_shaders())
            elif algo == "FSR":
                fsr_path = self._generate_fsr_shader()
                if fsr_path:
                    shaders.append(fsr_path)
            elif algo == "FSRCNNX":
                fsrcnnx = SHADERS_DIR / "FSRCNNX_x2_16-0-4-1.glsl"
                if fsrcnnx.exists():
                    shaders.append(str(fsrcnnx))
        return shaders

    def _emit_all_shaders(self, *_args):
        """Combine CAS + super-resolution shaders into a single list."""
        self.shader_changed.emit(self._build_shader_list())

    def _get_anime4k_shaders(self) -> list[str]:
        """Build Anime4K shader chain based on mode, quality, and scale settings."""
        mode = self._anime4k_mode.currentText()
        quality_display = self._anime4k_quality.currentText()
        scale = self._anime4k_scale.currentText()
        variant = _ANIME4K_QUALITY_MAP.get(quality_display, "M")

        shaders: list[str] = []

        # Always start with Clamp_Highlights
        shaders.append("Anime4K_Clamp_Highlights.glsl")

        base_mode = mode.split("+")[0]  # "A+A" → "A", "C+A" → "C"
        is_compound = "+" in mode

        if base_mode == "C":
            # Mode C: Upscale_Denoise (no separate Restore)
            denoise_shader = _ANIME4K_UPSCALE_DENOISE.get(variant)
            if denoise_shader:
                shaders.append(denoise_shader)
        else:
            # Mode A/B: Restore + Upscale
            restore_map = _ANIME4K_RESTORE.get(base_mode, {})
            restore_shader = restore_map.get(variant)
            if restore_shader:
                shaders.append(restore_shader)
            upscale_shader = _ANIME4K_UPSCALE.get(variant)
            if upscale_shader:
                shaders.append(upscale_shader)

        # For compound modes (A+A, B+B, C+A): add second pass
        if is_compound:
            second_mode = mode.split("+")[1]  # "A+A" → "A", "C+A" → "A"
            # AutoDownscale between passes
            shaders.append("Anime4K_AutoDownscalePre_x2.glsl")
            shaders.append("Anime4K_AutoDownscalePre_x4.glsl")
            # Second pass restore + upscale
            second_restore_map = _ANIME4K_RESTORE.get(second_mode, {})
            # Use one tier lower for second pass to save performance
            lower_variant = {"UL": "VL", "VL": "M", "L": "M", "M": "S", "S": "S"}[variant]
            second_restore = second_restore_map.get(lower_variant)
            if second_restore:
                shaders.append(second_restore)
            second_upscale = _ANIME4K_UPSCALE.get(lower_variant)
            if second_upscale:
                shaders.append(second_upscale)
        elif scale == "x4":
            # x4 mode for non-compound: add AutoDownscale + second upscale pass
            shaders.append("Anime4K_AutoDownscalePre_x2.glsl")
            shaders.append("Anime4K_AutoDownscalePre_x4.glsl")
            # Second upscale pass uses one tier lower
            lower_variant = {"UL": "VL", "VL": "M", "L": "M", "M": "S", "S": "S"}[variant]
            second_upscale = _ANIME4K_UPSCALE.get(lower_variant)
            if second_upscale:
                shaders.append(second_upscale)

        # Resolve paths and filter to existing files
        result = []
        for fname in shaders:
            p = SHADERS_DIR / fname
            if p.exists():
                result.append(str(p))
        return result

    def _get_current_upscale_factor(self) -> int:
        """Return the effective upscale factor based on current settings."""
        algo = self._upscale_algo.currentText()
        if algo == "Anime4K":
            scale = self._anime4k_scale.currentText()
            mode = self._anime4k_mode.currentText()
            if "+" in mode:
                return 4  # compound modes always do x4
            return 4 if scale == "x4" else 2
        # FSR and FSRCNNX are always x2
        return 2

    def _generate_cas_shader(self) -> str | None:
        if not _CAS_TEMPLATE.exists():
            return None
        # CAS SHARPNESS param: 0=max sharpening, 1=none; invert from user-facing strength
        sharpness = 1.0 - self.get_cas_sharpness()
        source = _CAS_TEMPLATE.read_text(encoding="utf-8")
        source = source.replace(
            "#define SHARPNESS 0.4",
            f"#define SHARPNESS {sharpness:.2f}",
        )
        _CAS_GENERATED.write_text(source, encoding="utf-8")
        return str(_CAS_GENERATED)

    def _generate_fsr_shader(self) -> str | None:
        if not _FSR_TEMPLATE.exists():
            return None
        sharpness = self._fsr_sharpness.value() / 10.0
        denoise = 1 if self._fsr_denoise.isChecked() else 0
        source = _FSR_TEMPLATE.read_text(encoding="utf-8")
        source = source.replace(
            "#define SHARPNESS 0.2",
            f"#define SHARPNESS {sharpness:.1f}",
        )
        source = source.replace(
            "#define FSR_RCAS_DENOISE 1",
            f"#define FSR_RCAS_DENOISE {denoise}",
        )
        _FSR_GENERATED.write_text(source, encoding="utf-8")
        return str(_FSR_GENERATED)

    def _build_deband_params(self) -> dict:
        """构建 deband 参数字典（不发信号），供导出离线复用。"""
        return {
            "iterations": self._deband_iterations.value(),
            "threshold": self._deband_threshold.value(),
            "range": self._deband_range.value(),
        }

    def _emit_deband(self, *_args):
        enabled = self._enable_deband.isChecked()
        self.deband_changed.emit(enabled, self._build_deband_params())

    def _build_vf_string(self) -> str:
        """构建降噪 vf 字符串（不发信号）。未启用降噪时返回空串。"""
        if not self._enable_denoise.isChecked():
            return ""
        mode = self._denoise_mode.currentText()
        strength = self._denoise_strength.value()
        if mode == "hqdn3d":
            return f"lavfi=[hqdn3d={strength}:{strength - 1}:{strength + 2}:{strength}]"
        return f"lavfi=[nlmeans=s={strength}:p=7:r=15]"

    def _emit_vf(self, *_args):
        """Emit vf filter string for denoise."""
        self.vf_changed.emit(self._build_vf_string())

    def _build_hdr_params(self) -> dict:
        """构建 HDR 色调映射参数字典（不发信号）。"""
        return {
            "tone-mapping": self._hdr_algo.currentText(),
            "hdr-compute-peak": self._hdr_peak.isChecked(),
        }

    def _emit_hdr(self, *_args):
        enabled = self._enable_hdr.isChecked()
        self.hdr_changed.emit(enabled, self._build_hdr_params())

    def _reset_all(self):
        self._enable_basic.setChecked(False)
        self._enable_sharpen.setChecked(False)
        self._enable_deband.setChecked(False)
        self._enable_denoise.setChecked(False)
        self._enable_hdr.setChecked(False)
        self._enable_upscale.setChecked(False)
        for prop, (slider, val_label) in self._sliders.items():
            slider.setValue(0)
            val_label.setText("0")
            self.property_changed.emit(prop, 0)
        self._sharpen_slider.setValue(6)
        self._denoise_strength.setValue(4)
        self._denoise_mode.setCurrentIndex(0)
        self._hdr_algo.setCurrentIndex(1)
        self._hdr_peak.setChecked(True)
        self._deband_iterations.setValue(2)
        self._deband_threshold.setValue(48)
        self._deband_range.setValue(16)
        self._upscale_algo.setCurrentIndex(0)
        self._anime4k_mode.setCurrentIndex(0)
        self._anime4k_scale.setCurrentIndex(1)  # default x4
        self._anime4k_quality.setCurrentIndex(1)  # default 均衡
        self._fsr_sharpness.setValue(2)
        self._fsr_denoise.setChecked(True)
        self._enable_fg.setChecked(False)
        self._fg_backend.setCurrentIndex(0)        # 默认 display-resample
        self._fg_params_stack.setCurrentIndex(0)
        self._tscale_algo.setCurrentIndex(0)
        self._interp_threshold.setValue(-1)
        self._refresh_dep_hint()
        self.shader_changed.emit([])
        self.deband_changed.emit(False, {})
        self.vf_changed.emit("")
        self.hdr_changed.emit(False, {})
        self.upscale_factor_changed.emit(1)
        self.frame_gen_changed.emit(False, {})

    def set_frame_gen_caps(self, caps: dict):
        """更新帧生成依赖能力并刷新后端可选项（供 main_window 在异步检测后调用）。"""
        if caps:
            self._fg_caps = caps
            self._refresh_dep_hint()

    def refresh_caps(self, caps: dict):
        """设置变更后由 main_window 调用，更新依赖能力并刷新后端可选项。"""
        self.set_frame_gen_caps(caps)

    def get_cas_sharpness(self) -> float:
        return self._sharpen_slider.value() / 10.0

    # ─── 导出状态捕获 ────────────────────────────────────────────────────
    # 把当前画面增强的「完整可复现状态」打包给离线导出模块（video_export）。
    # 复用上面的纯构建函数（_build_*），不触发任何 live-apply 信号。

    def _build_render_properties(self) -> dict:
        """构建离屏渲染时要写给 mpv 的 render property 字典。

        包含：基础画面调整(brightness/contrast/saturation/gamma，仅启用时)、
        deband 系列(仅启用时)、HDR 色调映射(仅启用时)。键名与 mpv property 完全一致，
        导出端可直接 player[k]=v 套用，和 main_window 的 live-apply 行为对齐。
        """
        props: dict = {}
        # 基础画面调整
        if self._enable_basic.isChecked():
            for prop, (slider, _vl) in self._sliders.items():
                props[prop] = slider.value()
        # 去色带
        if self._enable_deband.isChecked():
            dp = self._build_deband_params()
            props["deband"] = "yes"
            props["deband-iterations"] = dp["iterations"]
            props["deband-threshold"] = dp["threshold"]
            props["deband-range"] = dp["range"]
        # HDR 色调映射
        if self._enable_hdr.isChecked():
            hp = self._build_hdr_params()
            props["tone-mapping"] = hp.get("tone-mapping", "bt.2390")
            props["hdr-compute-peak"] = "yes" if hp.get("hdr-compute-peak", True) else "no"
        return props

    def _build_scheme_label(self) -> str:
        """生成中文画面方案摘要，如
        '超分Anime4K x4 + 锐化 + 去色带 + 降噪hqdn3d + HDR'。未启用任何画面增强时返回 '原画'。
        注意：插帧(display-resample 伪插帧)是显示期属性、无法烘焙进文件，故方案标签里
        显式标注「插帧不烘焙」。
        """
        parts: list[str] = []
        if self._enable_upscale.isChecked():
            algo = self._upscale_algo.currentText()
            if algo == "Anime4K":
                scale = "x4" if self._get_current_upscale_factor() == 4 else "x2"
                parts.append(f"超分Anime4K {scale}")
            else:
                parts.append(f"超分{algo} x2")
        if self._enable_sharpen.isChecked():
            parts.append("锐化")
        if self._enable_deband.isChecked():
            parts.append("去色带")
        if self._enable_basic.isChecked():
            parts.append("基础调整")
        if self._enable_denoise.isChecked():
            parts.append(f"降噪{self._denoise_mode.currentText()}")
        if self._enable_hdr.isChecked():
            parts.append("HDR")
        label = " + ".join(parts) if parts else "原画"
        if self._enable_fg.isChecked():
            label += "（插帧不烘焙）"
        return label

    def get_export_state(self) -> dict:
        """返回离线复现当前画面所需的完整状态，供 video_export.VideoExporter 使用。

        返回字段：
            shaders:        list[str]  CAS+超分着色器绝对路径列表
            render_props:   dict       要写给 mpv 的 render property（亮度/对比度/饱和度/
                                       gamma/deband*/tone-mapping/hdr-compute-peak）
            vf:             str        降噪 vf 字符串（hqdn3d/nlmeans），未启用为 ""
            upscale_factor: int        有效超分倍率(1/2/4)，未启用超分为 1
            denoise_mode:   str        当前降噪算法名（hqdn3d/nlmeans），未启用为 ""
            scheme_label:   str        中文画面方案摘要（含「插帧不烘焙」标注）
        """
        upscale_factor = (
            self._get_current_upscale_factor()
            if self._enable_upscale.isChecked() else 1
        )
        denoise_mode = (
            self._denoise_mode.currentText()
            if self._enable_denoise.isChecked() else ""
        )
        return {
            "shaders": self._build_shader_list(),
            "render_props": self._build_render_properties(),
            "vf": self._build_vf_string(),
            "upscale_factor": upscale_factor,
            "denoise_mode": denoise_mode,
            "scheme_label": self._build_scheme_label(),
        }
