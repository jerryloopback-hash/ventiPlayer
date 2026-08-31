"""导出模块公共部分：工具函数、ExportSettings / ExportResult 数据类。

被 src/core/export/{audio,bake_gpu,bake_pyav,mux}.py 共享；
对外入口仍是 src/core/video_export.py（保持既有 import 路径不变）。
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 项目根目录（libmpv-2.dll 所在）—— 本文件位于 src/core/export/，向上 4 层
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 进度回调签名：callback(progress: float 0..1, message: str)
ProgressCallback = Callable[[float, str], None]
DoneCallback = Callable[["ExportResult"], None]


def ensure_libmpv_on_path():
    """确保项目根目录（含 libmpv-2.dll）在 PATH 上，便于 import mpv。

    宿主正常启动时 src/main.py 已设过；独立运行/测试时这里兜底。"""
    root = str(PROJECT_ROOT)
    if root not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")


# ─── 工具：复用 main_window 的采样率/频率/截止估算（避免跨文件耦合，这里复刻一份）──

def format_sr(sr: int) -> str:
    """44100 → '44.1kHz'，48000 → '48kHz'。"""
    if not sr:
        return ""
    khz = sr / 1000
    return f"{int(khz)}kHz" if khz == int(khz) else f"{khz:.1f}kHz"


def format_freq(freq: int) -> str:
    """16000 → '16kHz'，22050 → '22kHz'。"""
    if not freq:
        return ""
    khz = freq / 1000
    return f"{int(round(khz))}kHz" if khz >= 10 else f"{khz:.1f}kHz"


def estimate_cutoff(sample_rate: int, bitrate: Optional[int], codec: str) -> Optional[int]:
    """按编码信息估算音频带宽截止频率（Hz）。复刻自 main_window._estimate_cutoff。"""
    if not sample_rate:
        return None
    nyquist = sample_rate // 2
    if not bitrate:
        if codec and codec.lower() in ("opus", "vorbis", "flac", "alac", "pcm"):
            return nyquist
        return int(nyquist * 0.75)
    codec_lower = (codec or "").lower()
    if codec_lower in ("flac", "alac", "pcm", "pcm_s16le", "pcm_s24le"):
        return nyquist
    if codec_lower == "opus":
        if bitrate >= 128:
            return min(nyquist, 20000)
        if bitrate >= 64:
            return min(nyquist, 18000)
        return min(nyquist, 12000)
    if bitrate >= 256:
        return min(nyquist, 20000)
    if bitrate >= 192:
        return min(nyquist, 18000)
    if bitrate >= 128:
        return min(nyquist, 16000)
    if bitrate >= 96:
        return min(nyquist, 14000)
    if bitrate >= 64:
        return min(nyquist, 12000)
    return min(nyquist, 8000)


def format_audio_scheme(apollo_enabled: bool, flashsr_enabled: bool,
                        apollo_fp16: bool, flashsr_fp16: bool) -> str:
    """拼出音频修复方案标签，如 'Apollo(fp32)+FlashSR(fp16)'；都没开返回 '原始音频'。

    与 main_window._format_enhance_scheme 一致，区别是无增强时给出更明确的 '原始音频'。"""
    parts = []
    if apollo_enabled:
        parts.append("Apollo(fp16)" if apollo_fp16 else "Apollo(fp32)")
    if flashsr_enabled:
        parts.append("FlashSR(fp16)" if flashsr_fp16 else "FlashSR(fp32)")
    return "+".join(parts) if parts else "原始音频"
@dataclass
class ExportSettings:
    """一次导出所需的全部配置：输出路径 + 音频方案 + 可复现画面的完整状态。"""

    output_path: str

    # 音频方案
    apollo_enabled: bool = False
    flashsr_enabled: bool = False
    apollo_fp16: bool = False
    flashsr_fp16: bool = False

    # 画面状态（来自 VideoEnhancePanel.get_export_state()）
    shaders: list = field(default_factory=list)          # GLSL 着色器绝对路径列表（含 GPU 降噪）
    render_props: dict = field(default_factory=dict)     # mpv render property 字典
    upscale_factor: int = 1                              # 有效超分倍率 1/2/4
    video_scheme_label: str = "原画"                     # 中文画面方案摘要

    # 源信息（用于 ExportResult 里报告真实参数与截止频率估算）
    src_width: Optional[int] = None
    src_height: Optional[int] = None
    src_fps: Optional[float] = None
    src_audio_sr: Optional[int] = None
    src_audio_bitrate: Optional[int] = None
    src_audio_codec: str = ""

    video_codec: str = "libx264"

    @classmethod
    def from_states(cls, output_path: str, audio_settings: dict,
                    export_state: dict, stream) -> "ExportSettings":
        """便捷构造：合并 EnhancePanel.get_settings() + VideoEnhancePanel.get_export_state()
        + StreamInfo。供 main_window 一行拼好。"""
        return cls(
            output_path=output_path,
            apollo_enabled=bool(audio_settings.get("apollo_enabled")),
            flashsr_enabled=bool(audio_settings.get("flashsr_enabled")),
            apollo_fp16=bool(audio_settings.get("apollo_fp16")),
            flashsr_fp16=bool(audio_settings.get("flashsr_fp16")),
            shaders=list(export_state.get("shaders", [])),
            render_props=dict(export_state.get("render_props", {})),
            upscale_factor=int(export_state.get("upscale_factor", 1) or 1),
            video_scheme_label=export_state.get("scheme_label", "原画"),
            src_width=getattr(stream, "video_width", None),
            src_height=getattr(stream, "video_height", None),
            src_fps=getattr(stream, "video_fps", None),
            src_audio_sr=getattr(stream, "audio_sample_rate", None),
            src_audio_bitrate=getattr(stream, "audio_bitrate", None),
            src_audio_codec=getattr(stream, "audio_codec", "") or "",
        )

    @property
    def audio_scheme_label(self) -> str:
        return format_audio_scheme(
            self.apollo_enabled, self.flashsr_enabled,
            self.apollo_fp16, self.flashsr_fp16,
        )

    @property
    def any_audio_enabled(self) -> bool:
        return self.apollo_enabled or self.flashsr_enabled


@dataclass
class ExportResult:
    """导出结果：成功标记 + 路径 + 提示语 + 实际烘焙出的视频/音频参数 + 方案标签。"""

    success: bool
    output_path: str = ""
    message: str = ""

    # 实际烘焙参数
    container_format: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    audio_sr: int = 0
    audio_cutoff_hz: int = 0

    audio_scheme_label: str = ""
    video_scheme_label: str = ""
    gpu_baked: bool = False  # True=离屏 GPU 真实烘焙；False=退化为 PyAV 近似

    @property
    def video_info_label(self) -> str:
        """成功弹窗用：'mp4 / 3840×2160 / 23.976fps / 48kHz / 24kHz'。"""
        fps_str = (f"{self.fps:.3f}".rstrip("0").rstrip(".") + "fps") if self.fps else ""
        bits = [
            self.container_format or "mp4",
            f"{self.width}×{self.height}" if self.width and self.height else "",
            fps_str,
            format_sr(self.audio_sr),
            format_freq(self.audio_cutoff_hz),
        ]
        return " / ".join(b for b in bits if b)


