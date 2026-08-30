"""帧生成总管 FrameGenManager。

职责（精简后）：
仅做帧生成后端的依赖检测，供面板灰显不可用项。本期保留两种后端：

后端命名（贯穿信号与本类）：
    "display-resample"  伪插帧，恒可用。不经本类（main_window 走 mpv property）。
    "lossless-scaling"  小黄鸭 (Lossless Scaling) 外部程序全屏补帧。由全局快捷键驱动，
                        进入全屏时发送快捷键开启缩放，退出全屏时再次发送关闭。
                        本类只做可执行文件存在性检测，进程/快捷键控制在
                        LosslessScalingController（src/core/lossless_scaling.py）。

设计说明：内置「真插帧」三后端（SVP/svpflow、PyTorch+RIFE、VapourSynth+RIFE）已移除。
小黄鸭是外部叠加程序，不接入 mpv 的 vf 链，故宿主进程不再生成任何 .vpy，也
**绝不 import vapoursynth / vsrife**（否则原生崩溃 0xe24c4a02）。
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class FrameGenManager:
    """帧生成编排器：仅做后端依赖检测。不直接执行任何推理或进程控制。"""

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = Path(config_dir) if config_dir else (Path.home() / ".ventiplayer")
        self.runtime_dir = self.config_dir / "runtime"
        self._caps: dict | None = None

    # ---- 依赖检测 ----

    def detect_lossless_scaling(self, exe_path: str) -> dict:
        """小黄鸭 (Lossless Scaling) 可执行文件静态检测。

        仅做文件存在性检查：exe_path 非空、是已存在的文件、且文件名为
        LosslessScaling.exe（不区分大小写）时视为可用。返回
        {available, reason, exe_path}，reason 为中文，可用时为空串。
        """
        exe_path = exe_path or ""
        if not exe_path:
            return {"available": False, "reason": "未配置 Lossless Scaling 路径", "exe_path": ""}
        p = Path(exe_path)
        if not p.is_file() or p.name.lower() != "losslessscaling.exe":
            return {"available": False, "reason": "路径无效或文件不存在", "exe_path": exe_path}
        return {"available": True, "reason": "", "exe_path": exe_path}

    def check_dependencies(self, ls_exe_path: str = "", force: bool = False) -> dict:
        """探测各后端依赖完备性，不抛异常。

        返回结构：
        {
          "display_resample": True,                 # 伪插帧恒可用
          "lossless_scaling": {"available": bool, "reason": str, "exe_path": str},
        }
        注意：本结果与 ls_exe_path 强相关，故不做缓存复用（force 形参保留以兼容旧调用）。
        """
        ls = self.detect_lossless_scaling(ls_exe_path)
        self._caps = {
            "display_resample": True,
            "lossless_scaling": ls,
        }
        print(f"[帧生成] 依赖检测: display_resample=True lossless_scaling={ls['available']} ({ls['reason'] or 'ok'})")
        return self._caps

    def available_backends(self, ls_exe_path: str = "") -> list[str]:
        """返回当前可用的后端列表（display-resample 恒可用）。"""
        caps = self.check_dependencies(ls_exe_path=ls_exe_path)
        backends = ["display-resample"]
        if caps["lossless_scaling"]["available"]:
            backends.append("lossless-scaling")
        return backends
