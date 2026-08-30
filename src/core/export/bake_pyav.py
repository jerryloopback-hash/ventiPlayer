"""导出画面烘焙（退化路径）：PyAV 解码 → numpy/lavfi 近似处理 → 重编码。

GPU 离屏渲染不可用时的回退。能烘焙：亮度/对比度/饱和度/gamma（numpy）、
降噪（nlmeans）、按超分倍率 lanczos 缩放。不能烘焙：GLSL 着色器、deband、
HDR tone-mapping（mpv GPU 管线特性，PyAV 无对应实现）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.core.export.common import ExportSettings

import numpy as np

logger = logging.getLogger(__name__)


class VideoEncoderMixin:
    """共享的 PyAV 视频编码器工具（GPU/PyAV 两条烘焙路径都用）。"""

    def _open_video_encoder(self, path: str, width: int, height: int,
                            fps: float, codec: str):
        """打开一个仅含视频流的 PyAV 输出，返回 (container, stream, time_base_fps)。

        编码 H.264(libx264)，像素格式 yuv420p（兼容性最佳，宽高需为偶数）。"""
        import av
        from fractions import Fraction

        fps = fps if fps and fps > 0 else 25.0
        rate = Fraction(fps).limit_denominator(100000)
        # H.264 yuv420p 要求宽高为偶数
        width -= width % 2
        height -= height % 2

        container = av.open(path, mode="w")
        stream = container.add_stream(codec or "libx264", rate=rate)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        try:
            stream.codec_context.time_base = Fraction(1, 1) / rate
        except Exception:
            pass
        # 合理默认：crf 18 接近视觉无损，preset medium 平衡速度/体积
        try:
            stream.options = {"crf": "18", "preset": "medium"}
        except Exception:
            pass
        return container, stream, rate

    @staticmethod
    def _encode_rgb_frame(container, stream, rgb: np.ndarray, pts: int):
        """把一帧 (H,W,3) uint8 RGB 编码并 mux。"""
        import av
        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
        frame.pts = pts
        for pkt in stream.encode(frame):
            container.mux(pkt)

    @staticmethod
    def _flush_encoder(container, stream):
        for pkt in stream.encode():
            container.mux(pkt)

    # ─── (b2) PyAV 近似烘焙（退化回退） ─────────────────────────────────

    def _bake_video_pyav(self, video_url: str, http_headers: Optional[dict],
                         es: ExportSettings, out_path: str) -> dict:
        """GPU 路径不可用时的退化烘焙：用 PyAV 解码 → numpy/lavfi 近似处理 → 重编码。

        能烘焙的：亮度/对比度/饱和度/gamma（numpy，因本机 PyAV 无 eq 滤镜）、
        降噪（nlmeans，若选 hqdn3d 也用 nlmeans 近似——本机 PyAV 无 hqdn3d）、
        按超分倍率做 lanczos 缩放近似。不能烘焙：GLSL 着色器(Anime4K/FSR/CAS)、deband、
        HDR tone-mapping —— 这些是 mpv GPU 管线特性，PyAV 无对应实现，故仅缩放近似。
        """
        import av
        from fractions import Fraction

        options = {}
        if http_headers:
            full = dict(http_headers)
            full.setdefault("User-Agent",
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36")
            options["user_agent"] = full["User-Agent"]
            if "Referer" in full:
                options["referer"] = full["Referer"]

        inp = av.open(video_url, options=options)
        try:
            vstream = inp.streams.video[0]
            vstream.thread_type = "AUTO"
            src_w = vstream.codec_context.width or es.src_width or 1920
            src_h = vstream.codec_context.height or es.src_height or 1080
            factor = max(1, int(es.upscale_factor or 1))
            out_w, out_h = src_w * factor, src_h * factor
            fps = self._fps_from_stream(vstream, es)

            container, stream, _rate = self._open_video_encoder(
                out_path, out_w, out_h, fps, es.video_codec)
            out_w, out_h = stream.width, stream.height

            # 预编译 numpy 系数（亮度/对比度/饱和度/gamma）
            eq = self._build_eq_coeffs(es.render_props)
            denoise_graph = self._build_denoise_graph(es, out_w, out_h)

            try:
                duration = float(vstream.duration * vstream.time_base) if vstream.duration else 0
            except Exception:
                duration = 0
            total = int(duration * fps) if duration and fps else 0

            pts = 0
            for frame in inp.decode(vstream):
                self._check_cancel()
                rgb = frame.to_ndarray(format="rgb24")
                # 缩放到目标分辨率（lanczos 近似超分）
                if (rgb.shape[1], rgb.shape[0]) != (out_w, out_h):
                    rgb = self._lanczos_scale(frame, out_w, out_h)
                # eq（numpy）
                if eq is not None:
                    rgb = self._apply_eq_numpy(rgb, eq)
                # 降噪（nlmeans lavfi）
                if denoise_graph is not None:
                    rgb = self._apply_denoise(denoise_graph, rgb, out_w, out_h)
                self._encode_rgb_frame(container, stream, rgb, pts)
                pts += 1
                if total:
                    self._report(0.4 + 0.5 * min(1.0, pts / total),
                                  f"PyAV 烘焙 {pts}/{total} 帧")
                else:
                    self._report(0.6, f"PyAV 烘焙 {pts} 帧")

            self._flush_encoder(container, stream)
            container.close()
            logger.info("PyAV 近似烘焙完成：%dx%d @%.3ffps, %d 帧",
                        out_w, out_h, fps, pts)
            return {"width": out_w, "height": out_h, "fps": fps}
        finally:
            inp.close()

    @staticmethod
    def _fps_from_stream(vstream, es: ExportSettings) -> float:
        try:
            r = vstream.average_rate or vstream.guessed_rate
            if r and float(r) > 0:
                return float(r)
        except Exception:
            pass
        return float(es.src_fps) if es.src_fps and es.src_fps > 0 else 25.0

    @staticmethod
    def _lanczos_scale(frame, out_w: int, out_h: int) -> np.ndarray:
        """用 PyAV 的 reformat（lanczos）缩放一帧，返回 rgb24 ndarray。"""
        from av.video.reformatter import Interpolation
        scaled = frame.reformat(width=out_w, height=out_h, format="rgb24",
                                interpolation=Interpolation.LANCZOS)
        return scaled.to_ndarray(format="rgb24")

    @staticmethod
    def _build_eq_coeffs(render_props: dict):
        """从 render props 提取 brightness/contrast/saturation/gamma（滑块 -100..100）。
        全为 0/缺省时返回 None（无需处理）。"""
        if not render_props:
            return None
        b = render_props.get("brightness", 0)
        c = render_props.get("contrast", 0)
        s = render_props.get("saturation", 0)
        g = render_props.get("gamma", 0)
        if not any((b, c, s, g)):
            return None
        # 映射到合理系数：mpv 取值 -100..100
        return {
            "brightness": b / 100.0,          # 加性，[-1,1]
            "contrast": 1.0 + c / 100.0,      # 乘性，围绕 0.5
            "saturation": 1.0 + s / 100.0,    # 饱和度系数
            "gamma": 2.0 ** (-g / 100.0),     # gamma 指数
        }

    @staticmethod
    def _apply_eq_numpy(rgb: np.ndarray, eq: dict) -> np.ndarray:
        """numpy 实现亮度/对比度/饱和度/gamma（本机 PyAV 无 eq 滤镜，故自己算）。"""
        x = rgb.astype(np.float32) / 255.0
        # gamma
        if abs(eq["gamma"] - 1.0) > 1e-3:
            x = np.power(np.clip(x, 0, 1), eq["gamma"])
        # contrast（围绕 0.5）
        if abs(eq["contrast"] - 1.0) > 1e-3:
            x = (x - 0.5) * eq["contrast"] + 0.5
        # brightness（加性）
        if abs(eq["brightness"]) > 1e-3:
            x = x + eq["brightness"]
        # saturation（向灰度插值）
        if abs(eq["saturation"] - 1.0) > 1e-3:
            luma = (0.299 * x[..., 0] + 0.587 * x[..., 1] + 0.114 * x[..., 2])[..., None]
            x = luma + (x - luma) * eq["saturation"]
        return (np.clip(x, 0, 1) * 255.0).astype(np.uint8)

    def _build_denoise_graph(self, es: ExportSettings, w: int, h: int):
        """构建 nlmeans 降噪 filter graph（本机 PyAV 无 hqdn3d，统一用 nlmeans 近似）。
        未启用降噪返回 None。"""
        if not es.denoise_mode:
            return None
        try:
            import av
            graph = av.filter.Graph()
            src = graph.add_buffer(width=w, height=h, format="rgb24")
            # nlmeans 强度近似：默认 s=4 与面板一致量级
            nl = graph.add("nlmeans", "s=4:p=7:r=15")
            sink = graph.add("buffersink")
            src.link_to(nl)
            nl.link_to(sink)
            graph.configure()
            return graph
        except Exception as e:
            logger.debug("构建降噪 graph 失败，跳过降噪: %s", e)
            return None

    @staticmethod
    def _apply_denoise(graph, rgb: np.ndarray, w: int, h: int) -> np.ndarray:
        import av
        try:
            f = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
            graph.push(f)
            out = graph.pull()
            return out.to_ndarray(format="rgb24")
        except Exception:
            return rgb

