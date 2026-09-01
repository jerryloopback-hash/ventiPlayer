"""MainWindow 视频导出接线 mixin：导出请求→选路径→后台烘焙→进度/完成弹窗。

从 main_window.py 拆出；self 即 MainWindow 实例。
"""

import re
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QMessageBox, QFileDialog, QProgressDialog

from src.core.video_export import VideoExporter, ExportSettings, ExportResult


class ExportMixin:
    @Slot()
    def _on_export_video_requested(self):
        """设置面板「导出当前视频为 MP4」：门控→选保存路径→后台烘焙导出。

        导出文件会真实套用当前面板上的音频(Apollo/FlashSR)与画面(超分/锐化/去色带/
        降噪/HDR 等)增强；RIFE 真插帧也会烘焙进文件（帧率x2）；
        伪插帧(display-resample)是显示期特性、小黄鸭是外部叠加，两者不烘焙。
        """
        stream = self._current_stream
        if stream is None or stream.is_live:
            QMessageBox.warning(self, "提示", "请先解析一个非直播视频")
            return
        if self._exporter is not None:
            QMessageBox.information(self, "提示", "已有导出任务在进行中")
            return

        last_dir = self._settings.get("export_last_dir") or ""
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", stream.title or "video")
        default_path = str(Path(last_dir) / f"{safe_title}.mp4") if last_dir else f"{safe_title}.mp4"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出为 MP4", default_path, "MP4 视频 (*.mp4)")
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
        self._settings.set("export_last_dir", str(Path(path).parent))

        es = ExportSettings.from_states(
            output_path=path,
            audio_settings=self._enhance_panel.get_settings(),
            export_state=self._video_enhance_panel.get_export_state(),
            stream=stream,
        )

        # 进度对话框（可取消）
        self._export_dialog = QProgressDialog("准备导出...", "取消", 0, 100, self)
        self._export_dialog.setWindowTitle("导出视频")
        self._export_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._export_dialog.setMinimumDuration(0)
        self._export_dialog.setAutoClose(False)
        self._export_dialog.setAutoReset(False)
        self._export_dialog.canceled.connect(self._on_export_cancel)
        self._export_dialog.setValue(0)

        self._exporter = VideoExporter(self._enhancer)
        self._exporter.export(
            video_url=stream.video_url,
            audio_url=stream.audio_url or stream.video_url,
            http_headers=stream.http_headers,
            export_settings=es,
            progress_callback=lambda p, msg: self._export_progress.emit(p, msg),
            done_callback=lambda r: self._export_done.emit(r),
        )
        self._status_label.setText("视频导出中...")

    @Slot(float, str)
    def _on_export_progress(self, progress: float, message: str):
        if self._export_dialog is not None:
            self._export_dialog.setValue(int(progress * 100))
            self._export_dialog.setLabelText(message)

    @Slot()
    def _on_export_cancel(self):
        if self._exporter is not None:
            self._exporter.cancel()
        self._status_label.setText("正在取消导出...")

    @Slot(object)
    def _on_export_done(self, result: ExportResult):
        """导出结束（成功/失败/取消）：关进度框、弹结果、清理 exporter。"""
        if self._export_dialog is not None:
            self._export_dialog.close()
            self._export_dialog = None
        self._exporter = None

        if not result.success:
            self._status_label.setText(f"导出结束: {result.message}")
            if "取消" not in result.message:
                QMessageBox.warning(self, "导出失败", result.message)
            return

        self._status_label.setText("导出成功")
        body = (
            f"已保存到：\n{result.output_path}\n\n"
            f"视频信息：{result.video_info_label}\n"
            f"音频修复方案：{result.audio_scheme_label}\n"
            f"画面增强方案：{result.video_scheme_label}"
        )
        if result.framegen_baked:
            body += "\n\nRIFE 真插帧已烘焙进文件（帧率翻倍）。"
        if result.gpu_degraded:
            body += (
                "\n\n注意：当前环境无法创建离屏 GPU 渲染上下文，已退化为 PyAV 近似烘焙；"
                "GLSL 着色器（Anime4K/FSR 超分、CAS 锐化等）未能真实烘焙到文件。"
            )
        QMessageBox.information(self, "导出成功", body)
