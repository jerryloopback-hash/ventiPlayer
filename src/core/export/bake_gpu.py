"""导出画面烘焙（GPU 主路径）：Qt 离屏 OpenGL + libmpv render API 真实烘焙 GLSL 着色器。

设计要点（承自原 video_export.py 模块头）：
- 驱动 libmpv 的 render API (mpv.MpvRenderContext)，配合 Qt 的离屏 OpenGL 上下文，
  让每一帧走完 mpv 的完整 GPU 着色器管线（Anime4K/FSR/FSRCNNX 超分、CAS 锐化、
  deband、HDR tone-mapping、亮度/对比度/饱和度/gamma），回读 framebuffer 后编码。
- 安全约束：绝不 import vapoursynth / vsrife；离屏 mpv 实例只用 lavfi 降噪 vf。
- 失败（无显示设备等）会抛出，由调用方退化到 bake_pyav。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.core.export.common import ExportSettings

import numpy as np

logger = logging.getLogger(__name__)


class GpuBakeMixin:
    """VideoExporter 的离屏 GPU 烘焙子管线。宿主需提供 _report/_check_cancel。"""

    # ─── (b1) 离屏 GPU 渲染真实烘焙（primary） ───────────────────────────

    def _bake_video_gpu(self, video_url: str, http_headers: Optional[dict],
                        es: ExportSettings, out_path: str) -> dict:
        """用 Qt 离屏 OpenGL 上下文 + libmpv render API 真实烘焙画面增强。

        失败（无显示设备/上下文创建失败/渲染异常）会抛出，由调用方退化到 PyAV。
        返回烘焙出的 {'width','height','fps'}。
        """
        from PySide6.QtGui import (
            QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat,
        )
        from PySide6.QtOpenGL import (
            QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
        )
        import mpv

        if QGuiApplication.instance() is None:
            raise RuntimeError("无 QGuiApplication 实例，无法创建离屏 GL 上下文")

        # 1) 离屏 GL 上下文 + surface
        fmt = QSurfaceFormat()
        fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        surface = QOffscreenSurface()
        surface.setFormat(fmt)
        surface.create()
        if not surface.isValid():
            raise RuntimeError("离屏 surface 创建失败")
        gl_ctx = QOpenGLContext()
        gl_ctx.setFormat(fmt)
        if not gl_ctx.create() or not gl_ctx.isValid():
            raise RuntimeError("OpenGL 上下文创建失败（headless/无 GPU）")
        if not gl_ctx.makeCurrent(surface):
            raise RuntimeError("makeCurrent 失败（无法绑定 GL 上下文到线程）")

        player = None
        render_ctx = None
        fbo = None
        try:
            # 2) get_proc_address 回调：mpv 通过它拿 GL 函数地址（经 Qt 上下文）
            def _get_proc_address(_ctx, name):
                try:
                    addr = gl_ctx.getProcAddress(name)  # name 为 bytes，Qt 接受
                    return int(addr) if addr else 0
                except Exception:
                    return 0
            proc_fn = mpv.MpvGlGetProcAddressFn(_get_proc_address)
            self._proc_fn_ref = proc_fn  # 防止被 GC

            # 3) 离屏 mpv 实例：vo=libmpv（不传 wid！），关音频，软解保证可读/确定性
            player = mpv.MPV(
                vo="libmpv",
                hwdec="no",            # 离屏渲染禁硬解，避免 GPU surface interop 问题
                audio="no",
                video_sync="audio",
                keep_open="yes",
                idle="yes",
                pause="yes",           # 加载后暂停，逐帧步进
                input_default_bindings=False,
                input_vo_keyboard=False,
                osc=False,
                loglevel="error",
            )

            # 4) render context（OpenGL）
            render_ctx = mpv.MpvRenderContext(
                player, "opengl",
                opengl_init_params={"get_proc_address": proc_fn},
            )
            # 新帧就绪时由 mpv 线程触发，置事件供渲染循环唤醒
            frame_ready = threading.Event()
            render_ctx.update_cb = lambda: frame_ready.set()

            # 5) 应用画面增强（着色器/deband/render props/降噪 vf）
            self._apply_video_enhancements(player, es)

            # 6) 加载文件
            self._set_http_headers(player, http_headers)
            player.play(video_url)
            self._wait_video_ready(player)

            # 7) 计算输出分辨率 = 源分辨率 * 超分倍率
            src_w, src_h = self._get_source_resolution(player, es)
            factor = max(1, int(es.upscale_factor or 1))
            out_w, out_h = src_w * factor, src_h * factor
            fps = self._get_fps(player, es)

            container, stream, _rate = self._open_video_encoder(
                out_path, out_w, out_h, fps, es.video_codec)
            out_w, out_h = stream.width, stream.height  # 取偶数对齐后的真实值

            # 8) FBO（目标分辨率）
            fbo_fmt = QOpenGLFramebufferObjectFormat()
            fbo = QOpenGLFramebufferObject(out_w, out_h, fbo_fmt)
            fbo_id = fbo.handle()

            # 9) 逐帧渲染循环
            baked = self._render_loop(
                player, render_ctx, frame_ready, fbo, fbo_id,
                container, stream, out_w, out_h, fps, es)

            self._flush_encoder(container, stream)
            container.close()
            logger.info("离屏 GPU 烘焙完成：%dx%d @%.3ffps, %d 帧",
                        out_w, out_h, fps, baked)
            return {"width": out_w, "height": out_h, "fps": fps}
        finally:
            try:
                if render_ctx is not None:
                    render_ctx.update_cb = None
                    render_ctx.free()
            except Exception:
                pass
            try:
                if player is not None:
                    # 先清 vf，避免析构期原生崩溃（与 player_widget.destroy 同理）
                    try:
                        player.command("vf", "set", "")
                    except Exception:
                        pass
                    player.terminate()
            except Exception:
                pass
            try:
                if fbo is not None:
                    fbo.release()
            except Exception:
                pass
            try:
                gl_ctx.doneCurrent()
            except Exception:
                pass

    # ─── GPU 烘焙辅助 ────────────────────────────────────────────────────

    @staticmethod
    def _set_http_headers(player, headers: Optional[dict]):
        if headers:
            try:
                player.http_header_fields = [f"{k}: {v}" for k, v in headers.items()]
            except Exception:
                pass

    def _apply_video_enhancements(self, player, es: ExportSettings):
        """把 get_export_state 捕获的画面增强套到离屏 mpv，对齐 main_window 的 live-apply。

        安全：vf 仅允许 lavfi 的 hqdn3d/nlmeans 降噪；绝不注入 vapoursynth vf。"""
        import sys as _sys
        # render props：brightness/contrast/saturation/gamma/deband*/tone-mapping/...
        for k, v in (es.render_props or {}).items():
            try:
                player[k] = v
            except Exception as e:
                logger.debug("set render prop %s=%s 失败: %s", k, v, e)
        # GLSL 着色器链
        try:
            shaders = [p for p in (es.shaders or []) if Path(p).is_file()]
            if shaders:
                sep = ";" if _sys.platform == "win32" else ":"
                player.command("change-list", "glsl-shaders", "set", sep.join(shaders))
            else:
                player.command("change-list", "glsl-shaders", "clr", "")
        except Exception as e:
            logger.debug("应用着色器失败: %s", e)
        # 降噪 vf（仅 lavfi，安全）
        try:
            vf = es.vf or ""
            if vf and "vapoursynth" not in vf.lower():
                player.command("vf", "set", vf)
            else:
                player.command("vf", "set", "")
        except Exception as e:
            logger.debug("应用 vf 失败: %s", e)

    def _wait_video_ready(self, player, timeout: float = 30.0):
        """等待文件加载、video 参数就绪。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_cancel()
            try:
                vp = player.video_out_params
                if vp and vp.get("w"):
                    return
            except Exception:
                pass
            time.sleep(0.05)
        raise RuntimeError("等待视频就绪超时")

    @staticmethod
    def _get_source_resolution(player, es: ExportSettings) -> tuple:
        try:
            vp = player.video_out_params
            if vp and vp.get("w") and vp.get("h"):
                return int(vp["w"]), int(vp["h"])
        except Exception:
            pass
        if es.src_width and es.src_height:
            return int(es.src_width), int(es.src_height)
        return 1920, 1080

    @staticmethod
    def _get_fps(player, es: ExportSettings) -> float:
        for getter in (lambda: player.container_fps,
                       lambda: player.estimated_vf_fps):
            try:
                v = getter()
                if v and v > 0:
                    return float(v)
            except Exception:
                pass
        if es.src_fps and es.src_fps > 0:
            return float(es.src_fps)
        return 25.0

    def _render_loop(self, player, render_ctx, frame_ready, fbo, fbo_id,
                     container, stream, out_w, out_h, fps, es: ExportSettings) -> int:
        """逐帧步进 + 离屏渲染 + 回读 + 编码，直到 EOF。返回烘焙帧数。

        deterministic 思路：暂停态下用 frame-step 精确步进每一解码帧；render() 把当前帧
        画进我们的 FBO；toImage() 回读为 RGBA，转 RGB 后交给 PyAV 编码。EOF 通过
        eof-reached / idle-active 判定。
        """
        # 估算总帧数用于进度（时长*fps），拿不到时按时间比例兜底
        try:
            duration = float(player.duration or 0)
        except Exception:
            duration = 0
        total_frames = int(duration * fps) if duration and fps else 0

        pts = 0
        # 渲染首帧（文件已加载、暂停在第 0 帧）
        while True:
            self._check_cancel()
            # 渲染当前帧到 FBO
            self._render_one(render_ctx, fbo_id, out_w, out_h)
            img = fbo.toImage()  # QImage（GL 读回，可能上下翻转，已用 flip_y 修正）
            rgb = self._qimage_to_rgb(img, out_w, out_h)
            self._encode_rgb_frame(container, stream, rgb, pts)
            pts += 1

            if total_frames:
                self._report(0.4 + 0.5 * min(1.0, pts / total_frames),
                              f"烘焙画面 {pts}/{total_frames} 帧")
            else:
                self._report(0.6, f"烘焙画面 {pts} 帧")

            # 判断是否已到结尾
            if self._is_eof(player):
                break
            # 步进下一帧
            if not self._step_next_frame(player, render_ctx, frame_ready):
                break
            if total_frames and pts >= total_frames + 2:
                break  # 安全上限，防异常文件死循环
        return pts

    def _render_one(self, render_ctx, fbo_id: int, w: int, h: int):
        """调一次 mpv render，把当前帧渲染进指定 FBO。flip_y=True 修正 GL 上下颠倒。"""
        render_ctx.render(
            opengl_fbo={"w": w, "h": h, "fbo": int(fbo_id)},
            flip_y=True,
        )

    @staticmethod
    def _qimage_to_rgb(img, w: int, h: int) -> np.ndarray:
        """QImage → (h,w,3) uint8 RGB numpy。"""
        from PySide6.QtGui import QImage
        img = img.convertToFormat(QImage.Format.Format_RGBA8888)
        ptr = img.constBits()
        bpl = img.bytesPerLine()
        arr = np.frombuffer(ptr, dtype=np.uint8, count=bpl * img.height())
        arr = arr.reshape((img.height(), bpl // 4, 4))
        arr = arr[:h, :w, :3]  # 去 padding + 丢 alpha
        return np.ascontiguousarray(arr)

    def _step_next_frame(self, player, render_ctx, frame_ready, timeout: float = 5.0) -> bool:
        """frame-step 到下一解码帧并等 render 更新。返回 False 表示已到 EOF/无新帧。"""
        if self._is_eof(player):
            return False
        frame_ready.clear()
        try:
            player.command("frame-step")
        except Exception:
            return False
        # 等 mpv 报告新帧（update_cb 置位）；超时则尝试读 eof 再决定
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_cancel()
            if frame_ready.wait(0.05):
                render_ctx.update()  # 消费 update 标志
                return True
            if self._is_eof(player):
                return False
        return False

    @staticmethod
    def _is_eof(player) -> bool:
        try:
            if player.eof_reached:
                return True
        except Exception:
            pass
        try:
            if player.idle_active:
                return True
        except Exception:
            pass
        return False

