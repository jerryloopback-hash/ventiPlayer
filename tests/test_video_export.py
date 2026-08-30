"""视频导出模块单元测试：import、ExportSettings/ExportResult、面板状态捕获、标签格式化。

不跑真实 GPU 渲染或网络导出（headless 环境无显示/无网络）；只验证：
- VideoExporter / ExportSettings / ExportResult 可导入构造
- VideoEnhancePanel.get_export_state() 返回期望的键与 scheme_label
- audio_scheme_label / video_info_label / 截止频率估算格式化正确

运行：QT_QPA_PLATFORM=offscreen .venv312/Scripts/python.exe -m pytest test_video_export.py -q
"""

import os
import sys
import unittest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core.video_export import (
    VideoExporter, ExportSettings, ExportResult,
    _format_audio_scheme, _estimate_cutoff, _format_sr, _format_freq,
)


class _FakeStream:
    """最小 StreamInfo 替身。"""
    def __init__(self):
        self.video_width = 1920
        self.video_height = 1080
        self.video_fps = 23.976
        self.audio_sample_rate = 44100
        self.audio_bitrate = 128
        self.audio_codec = "aac"
        self.is_live = False
        self.title = "测试视频"
        self.video_url = "http://x/v"
        self.audio_url = "http://x/a"
        self.http_headers = {}


class TestExportSettings(unittest.TestCase):

    def test_from_states_merges_all(self):
        audio = {"apollo_enabled": True, "flashsr_enabled": True,
                 "apollo_fp16": False, "flashsr_fp16": True}
        export_state = {
            "shaders": ["/a/Anime4K_Upscale_CNN_x2_M.glsl"],
            "render_props": {"brightness": 10, "deband": "yes"},
            "vf": "lavfi=[hqdn3d=4:3:6:4]",
            "upscale_factor": 4,
            "denoise_mode": "hqdn3d",
            "scheme_label": "超分Anime4K x4 + 降噪hqdn3d",
        }
        es = ExportSettings.from_states("/out/v.mp4", audio, export_state, _FakeStream())
        self.assertEqual(es.output_path, "/out/v.mp4")
        self.assertTrue(es.apollo_enabled)
        self.assertTrue(es.flashsr_fp16)
        self.assertEqual(es.upscale_factor, 4)
        self.assertEqual(es.denoise_mode, "hqdn3d")
        self.assertEqual(es.shaders, ["/a/Anime4K_Upscale_CNN_x2_M.glsl"])
        self.assertEqual(es.render_props["deband"], "yes")
        self.assertEqual(es.src_width, 1920)
        self.assertEqual(es.src_audio_sr, 44100)
        self.assertTrue(es.any_audio_enabled)

    def test_audio_scheme_label(self):
        es = ExportSettings(output_path="x", apollo_enabled=True, apollo_fp16=True,
                            flashsr_enabled=True, flashsr_fp16=False)
        self.assertEqual(es.audio_scheme_label, "Apollo(fp16)+FlashSR(fp32)")
        es2 = ExportSettings(output_path="x")
        self.assertEqual(es2.audio_scheme_label, "原始音频")
        self.assertFalse(es2.any_audio_enabled)


class TestSchemeFormatting(unittest.TestCase):

    def test_format_audio_scheme(self):
        self.assertEqual(_format_audio_scheme(True, False, False, False), "Apollo(fp32)")
        self.assertEqual(_format_audio_scheme(False, True, False, True), "FlashSR(fp16)")
        self.assertEqual(
            _format_audio_scheme(True, True, True, True),
            "Apollo(fp16)+FlashSR(fp16)")
        self.assertEqual(_format_audio_scheme(False, False, False, False), "原始音频")

    def test_format_sr_freq(self):
        self.assertEqual(_format_sr(44100), "44.1kHz")
        self.assertEqual(_format_sr(48000), "48kHz")
        self.assertEqual(_format_freq(16000), "16kHz")
        self.assertEqual(_format_freq(22050), "22kHz")

    def test_estimate_cutoff(self):
        # aac 128kbps → 16kHz 上限
        self.assertEqual(_estimate_cutoff(44100, 128, "aac"), 16000)
        # flac → nyquist
        self.assertEqual(_estimate_cutoff(48000, 1000, "flac"), 24000)


class TestExportResult(unittest.TestCase):

    def test_video_info_label(self):
        r = ExportResult(
            success=True, container_format="mp4",
            width=3840, height=2160, fps=23.976,
            audio_sr=48000, audio_cutoff_hz=24000)
        label = r.video_info_label
        self.assertIn("mp4", label)
        self.assertIn("3840×2160", label)
        self.assertIn("23.976fps", label)
        self.assertIn("48kHz", label)
        self.assertIn("24kHz", label)

    def test_video_info_label_integer_fps(self):
        r = ExportResult(success=True, width=1920, height=1080, fps=30.0,
                         audio_sr=44100, audio_cutoff_hz=16000)
        self.assertIn("30fps", r.video_info_label)


class TestVideoExporterConstruct(unittest.TestCase):

    def test_construct_and_cancel(self):
        from unittest import mock
        exporter = VideoExporter(mock.MagicMock())
        self.assertIsNotNone(exporter)
        exporter.cancel()  # 不应抛错

    def test_compute_audio_cutoff(self):
        from unittest import mock
        exporter = VideoExporter(mock.MagicMock())
        # 启用增强 → Nyquist
        es = ExportSettings(output_path="x", flashsr_enabled=True)
        self.assertEqual(exporter._compute_audio_cutoff(es, 48000), 24000)
        # 未增强 → 按源编码估算
        es2 = ExportSettings(output_path="x", src_audio_bitrate=128,
                             src_audio_codec="aac")
        self.assertEqual(exporter._compute_audio_cutoff(es2, 44100), 16000)

    def test_eq_numpy_identity(self):
        """全 0 调整系数应返回 None（无需处理）。"""
        from unittest import mock
        import numpy as np
        exporter = VideoExporter(mock.MagicMock())
        self.assertIsNone(exporter._build_eq_coeffs({}))
        self.assertIsNone(exporter._build_eq_coeffs(
            {"brightness": 0, "contrast": 0, "saturation": 0, "gamma": 0}))
        eq = exporter._build_eq_coeffs({"brightness": 50})
        self.assertIsNotNone(eq)
        # 亮度+50 应让中灰整体变亮
        gray = np.full((4, 4, 3), 128, dtype=np.uint8)
        out = exporter._apply_eq_numpy(gray, eq)
        self.assertTrue((out >= 128).all())


class TestPanelExportState(unittest.TestCase):
    """VideoEnhancePanel.get_export_state() 需要 QApplication（offscreen）。"""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from src.gui.video_enhance_panel import VideoEnhancePanel
        return VideoEnhancePanel()

    def test_default_state_keys(self):
        panel = self._panel()
        state = panel.get_export_state()
        for key in ("shaders", "render_props", "vf", "upscale_factor",
                    "denoise_mode", "scheme_label"):
            self.assertIn(key, state)
        self.assertIsInstance(state["shaders"], list)
        self.assertIsInstance(state["render_props"], dict)
        self.assertIsInstance(state["vf"], str)
        self.assertIsInstance(state["scheme_label"], str)
        # 默认啥都没开 → 原画，倍率 1
        self.assertEqual(state["upscale_factor"], 1)
        self.assertEqual(state["scheme_label"], "原画")
        self.assertEqual(state["vf"], "")

    def test_enabled_features_reflected(self):
        panel = self._panel()
        # 开启超分 Anime4K x4 + 锐化 + 去色带 + 降噪 + HDR
        panel._enable_upscale.setChecked(True)
        panel._upscale_algo.setCurrentText("Anime4K")
        # 默认 scale 已是 x4
        panel._enable_sharpen.setChecked(True)
        panel._enable_deband.setChecked(True)
        panel._enable_denoise.setChecked(True)
        panel._enable_hdr.setChecked(True)
        panel._enable_basic.setChecked(True)
        for _p, (slider, _vl) in panel._sliders.items():
            slider.setValue(10)

        state = panel.get_export_state()
        # 倍率为 4
        self.assertEqual(state["upscale_factor"], 4)
        # 着色器非空
        self.assertTrue(len(state["shaders"]) > 0)
        # render props 含基础调整 + deband + tone-mapping
        rp = state["render_props"]
        self.assertIn("brightness", rp)
        self.assertEqual(rp.get("deband"), "yes")
        self.assertIn("tone-mapping", rp)
        # vf 为 nlmeans 或 hqdn3d
        self.assertTrue(state["vf"].startswith("lavfi="))
        # 降噪模式
        self.assertIn(state["denoise_mode"], ("hqdn3d", "nlmeans"))
        # scheme_label 含关键中文词
        label = state["scheme_label"]
        self.assertIn("超分Anime4K", label)
        self.assertIn("锐化", label)
        self.assertIn("去色带", label)
        self.assertIn("HDR", label)

    def test_frame_gen_not_baked_note(self):
        panel = self._panel()
        panel._enable_fg.setChecked(True)
        label = panel.get_export_state()["scheme_label"]
        self.assertIn("插帧不烘焙", label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
