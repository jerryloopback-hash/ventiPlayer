"""导出画面烘焙（RIFE 真插帧 pass）——Phase 2：PyAV 解码 → 共享内核推理 → 重编码。

与实时链共用 src/core/rife_kernel.py（同一 worker 线程/同一套 YUV 域数学）。
编码策略与实时 interleave 语义一致：源帧平面直通零转换，仅中点帧过推理，
输出帧率 = 源 × 2（帧序 F0, M01, F1, M12, ...，尾帧无后继不出中点）。

组合策略（先插帧后超分，与实时管线顺序一致）：
  - 纯 RIFE（无任何画面增强）：本 pass 输出即最终视频（保留源位深，
    10-bit 源编码 yuv420p10le）
  - RIFE + 画面增强：本 pass 先产出高质量中间文件（crf 12），再交给既有
    GPU/PyAV 烘焙套用着色器/超分/eq —— 插帧在前、超分在后
失败语义：任何异常向上抛出，由 video_export._run 捕获后降级为不插帧导出。
"""

from __future__ import annotations

import logging
from fractions import Fraction
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.core.export.common import ExportSettings

import numpy as np

logger = logging.getLogger(__name__)

# 支持的源像素格式（420 族，与实时链的格式守卫一致）
_SUPPORTED_FMTS = ("yuv420p", "yuv420p10le", "nv12")


class RifeBakeMixin:
    """VideoExporter 的 RIFE 插帧烘焙 pass。宿主需提供 _report/_check_cancel/_tmp_path。"""

    def _rife_fg_request(self, es: "ExportSettings") -> Optional[dict]:
        """从 export_state 解出 RIFE 配置并核验依赖；不可用返回 None（不插帧导出）。"""
        fg = es.framegen or {}
        if not fg.get("enabled") or fg.get("backend") != "rife-torch":
            return None
        import importlib.util
        if importlib.util.find_spec("torch") is None:
            logger.warning("RIFE 烘焙跳过：torch 不可导入")
            return None
        from src.models.rife import VERSIONS, weights_exist
        model = fg.get("model") or "v4_25_lite"
        if model not in VERSIONS or not weights_exist(model):
            logger.warning("RIFE 烘焙跳过：模型 %s 权重缺失", model)
            return None
        return {"model": model, "scale": float(fg.get("scale") or 0.75)}

    @staticmethod
    def _is_pure_rife(es: "ExportSettings") -> bool:
        """是否纯 RIFE 导出（无着色器/超分/eq 等任何画面增强）。
        纯 RIFE 时插帧 pass 直接产出最终视频，不再走第二遍烘焙。"""
        return not (es.shaders or int(es.upscale_factor or 1) > 1
                    or bool(es.render_props))

    def _bake_video_rife(self, video_url: str, http_headers: Optional[dict],
                         rife_fg: dict, out_path: str, *,
                         pure: bool) -> dict:
        """RIFE 插帧烘焙 pass：解码 → 源帧直通 + 中点帧推理 → 重编码 2x fps。

        返回 {'width','height','fps'}。失败抛异常（调用方降级为不插帧）。
        进度区间：pure → 0.02-0.92（它是唯一重活）；组合 → 0.02-0.38（后段
        留给既有 GPU/PyAV 烘焙的 0.4-0.9 与混流）。
        """
        import av
        import torch

        from src.core.rife_kernel import MATRICES, get_kernel

        options = {}
        if http_headers:
            full_h = dict(http_headers)
            full_h.setdefault("User-Agent",
                              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36")
            options["user_agent"] = full_h["User-Agent"]
            if "Referer" in full_h:
                options["referer"] = full_h["Referer"]

        inp = av.open(video_url, options=options)
        try:
            vstream = inp.streams.video[0]
            vstream.thread_type = "AUTO"
            src_w = vstream.codec_context.width or 1920
            src_h = vstream.codec_context.height or 1080

            kernel = get_kernel(rife_fg["model"], fp16=True)
            scale = float(rife_fg["scale"])
            rate = self._src_rate_of(vstream)
            out_rate = rate * 2

            # 预热：MIOpen handle + 新分辨率 kernel 编译（命中磁盘缓存时毫秒级）
            self._report(0.02, "RIFE 插帧预热...")
            kernel.warm(src_h, src_w, scale)

            # 源位深保留：纯 RIFE 直出时 10-bit 源编码 10-bit
            bits = 10 if "10" in (vstream.codec_context.pix_fmt or "") else 8
            pix_fmt = "yuv420p10le" if bits == 10 else "yuv420p"
            enc_opts = ({"crf": "18", "preset": "medium"} if pure
                        else {"crf": "12", "preset": "medium"})  # 中间文件高质量留再编码余量
            container, stream, _rt = self._open_video_encoder(
                out_path, src_w, src_h, float(out_rate), "libx264",
                pix_fmt=pix_fmt, options=enc_opts)

            # 进度总数：流帧数优先，退化按时长估算
            total = int(getattr(vstream, "frames", 0) or 0)
            if not total:
                try:
                    dur = float(vstream.duration * vstream.time_base)
                except Exception:
                    dur = 0
                total = int(dur * float(rate)) if dur else 0
            lo, hi = (0.02, 0.92) if pure else (0.02, 0.38)

            out_pts = 0
            decoded = 0
            prev = None  # 上一源帧的 (y,u,v) numpy 平面（1 帧前瞻求中点）
            for frame in inp.decode(vstream):
                self._check_cancel()
                if frame.format.name not in _SUPPORTED_FMTS:
                    raise RuntimeError("RIFE 烘焙不支持源像素格式 "
                                       + frame.format.name)
                cur = self._frame_planes(frame)
                bits_f = 10 if frame.format.name == "yuv420p10le" else 8
                mtx, full = self._frame_color_meta(frame)
                if prev is not None:
                    t_prev = tuple(torch.from_numpy(p).cuda() for p in prev)
                    t_cur = tuple(torch.from_numpy(p).cuda() for p in cur)
                    y, u, v = kernel.midpoint(
                        t_prev, t_cur, bits=bits_f, full=full, mtx=mtx,
                        scale=scale, out_h=src_h, out_w=src_w)
                    self._encode_planes(container, stream, y, u, v,
                                        pix_fmt, out_pts)
                    out_pts += 1
                self._encode_planes(container, stream, *cur, pix_fmt, out_pts)
                out_pts += 1
                prev = cur
                decoded += 1
                if total:
                    self._report(lo + (hi - lo) * min(1.0, decoded / total),
                                 f"RIFE 插帧 {decoded}/{total} 帧")
                else:
                    self._report(lo + (hi - lo) * 0.5, f"RIFE 插帧 {decoded} 帧")

            self._flush_encoder(container, stream)
            container.close()
            fps = float(out_rate)
            logger.info("RIFE 插帧烘焙完成：%dx%d @%.3ffps, %d 输出帧（%d 源帧）",
                        src_w, src_h, fps, out_pts, decoded)
            return {"width": src_w, "height": src_h, "fps": fps}
        finally:
            inp.close()

    # ─── RIFE 烘焙辅助 ───────────────────────────────────────────────────

    @staticmethod
    def _src_rate_of(vstream) -> Fraction:
        """源帧率 → 精确有理数（平均帧率/猜测帧率，退化 25）。"""
        r = getattr(vstream, "average_rate", None) or getattr(vstream, "guessed_rate", None)
        if r and float(r) > 0:
            return Fraction(r).limit_denominator(1001)
        return Fraction(25)

    @staticmethod
    def _plane_from_frame(frame, idx: int, dtype) -> np.ndarray:
        """按 plane 直读一个平面 → (h, w) ndarray（只读，去行尾 padding）。

        不用 to_ndarray()：yuv420p 会把 u/v 并排塞进剩余行（(h*1.5, w) 布局），
        按行切会切错；plane 的 line_size×height 语义无歧义（实时链同样按 plane 读）。
        """
        p = frame.planes[idx]
        el = 1 if dtype == np.uint8 else 2
        arr = np.frombuffer(memoryview(p), dtype=dtype,
                            count=(p.line_size * p.height) // el)
        return arr.reshape(p.height, p.line_size // el)[:, :p.width]

    @staticmethod
    def _frame_planes(frame) -> tuple:
        """VideoFrame → (y, u, v) 2D ndarray（原生格式原生采样，零色彩转换）。"""
        name = frame.format.name
        dtype = np.uint16 if "10" in name else np.uint8
        get = lambda i: RifeBakeMixin._plane_from_frame(frame, i, dtype)
        if name == "nv12":
            c = get(1)  # 交错 uv
            return get(0), c[:, 0::2], c[:, 1::2]
        return get(0), get(1), get(2)

    @staticmethod
    def _frame_color_meta(frame) -> tuple:
        """帧元数据 → (mtx, full)。缺省 709/limited，与实时链的 prop 缺省一致。"""
        # 延迟导入：本模块在 app 启动期被 video_export 引入，不能连带拉起 torch
        from src.core.rife_kernel import MATRICES
        cs = str(getattr(frame, "colorspace", "") or "")
        if "2020" in cs:
            mtx = MATRICES[9]
        elif any(k in cs for k in ("470BG", "170M", "601", "FCC")):
            mtx = MATRICES[5]
        else:
            mtx = MATRICES[1]
        cr = str(getattr(frame, "color_range", "") or "")
        return mtx, "JPEG" in cr or "FULL" in cr.upper()

    @staticmethod
    def _encode_planes(container, stream, y: np.ndarray, u: np.ndarray,
                       v: np.ndarray, pix_fmt: str, pts: int):
        """三平面 → PyAV 堆叠布局（y 在上，下方 u|v 左右并排，10-bit 为 uint16）
        → VideoFrame → 编码并 mux。布局已与 to_ndarray 逐字节对照验证。"""
        import av
        h, w = y.shape
        dtype = y.dtype
        packed = np.empty((h * 3 // 2, w), dtype=dtype)
        packed[:h] = y
        packed[h:, :w // 2] = u
        packed[h:, w // 2:] = v
        frame = av.VideoFrame.from_ndarray(packed, format=pix_fmt)
        frame.pts = pts
        for pkt in stream.encode(frame):
            container.mux(pkt)
