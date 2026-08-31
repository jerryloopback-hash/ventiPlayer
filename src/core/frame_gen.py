"""帧生成总管 FrameGenManager。

职责（精简后）：
仅做帧生成后端的依赖检测，供面板灰显不可用项。本期保留后端：

后端命名（贯穿信号与本类）：
    "display-resample"  伪插帧，恒可用。不经本类（main_window 走 mpv property）。
    "lossless-scaling"  小黄鸭 (Lossless Scaling) 外部程序全屏补帧。由全局快捷键驱动，
                        进入全屏时发送快捷键开启缩放，退出全屏时再次发送关闭。
                        本类只做可执行文件存在性检测，进程/快捷键控制在
                        LosslessScalingController（src/core/lossless_scaling.py）。
    "rife-torch"        RIFE AI 真插帧：mpv vf_vapoursynth + vpy 内直调宿主 torch
                        (ROCm) 推理。本类只做轻量依赖检测（torch/vapoursynth 可导入、
                        权重文件在位），编排逻辑在 RifeFrameGenService
                        （src/core/rife_service.py）。

设计说明：早期内置「真插帧」三后端（SVP/svpflow、PyTorch+RIFE、VapourSynth+RIFE）
曾因 mpv 内嵌 VSScript 的坏 handler/logging 反噬而整体移除（崩溃 0xe24c4a02）。
2026-08 Phase 0/1 重引入 torch+vpy 真插帧：Phase 0 探针确认 PySide6+torch+libmpv
组合不再复现该崩溃，且 vsscript 与宿主共享 interpreter（模型零重复加载）。
"""

import importlib.util
import logging
from pathlib import Path

from src.models.rife import VERSIONS as RIFE_VERSIONS, weights_exist as rife_weights_exist

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

    def detect_rife(self) -> dict:
        """RIFE 真插帧轻量依赖检测（不 import torch，避免主线程卡顿）。

        检查项：torch / vapoursynth 可导入（find_spec 静态查找）、各版本权重文件
        在位。返回 {available, reason, versions}，versions 为权重齐备的可选模型。
        """
        missing_pkgs = [name for name in ("torch", "vapoursynth")
                        if importlib.util.find_spec(name) is None]
        if missing_pkgs:
            return {"available": False,
                    "reason": f"缺少依赖包: {'、'.join(missing_pkgs)}",
                    "versions": []}
        ok_versions = [v for v in RIFE_VERSIONS if rife_weights_exist(v)]
        if not ok_versions:
            return {"available": False,
                    "reason": "缺少 RIFE 权重（请运行 download_models.py）",
                    "versions": []}
        return {"available": True, "reason": "", "versions": ok_versions}

    def check_dependencies(self, ls_exe_path: str = "", force: bool = False) -> dict:
        """探测各后端依赖完备性，不抛异常。

        返回结构：
        {
          "display_resample": True,                 # 伪插帧恒可用
          "lossless_scaling": {"available": bool, "reason": str, "exe_path": str},
          "rife_torch":       {"available": bool, "reason": str, "versions": [str]},
        }
        注意：本结果与 ls_exe_path 强相关，故不做缓存复用（force 形参保留以兼容旧调用）。
        """
        ls = self.detect_lossless_scaling(ls_exe_path)
        rife = self.detect_rife()
        self._caps = {
            "display_resample": True,
            "lossless_scaling": ls,
            "rife_torch": rife,
        }
        print(f"[帧生成] 依赖检测: display_resample=True "
              f"lossless_scaling={ls['available']} ({ls['reason'] or 'ok'}) "
              f"rife_torch={rife['available']} ({rife['reason'] or 'ok'})")
        return self._caps

    def available_backends(self, ls_exe_path: str = "") -> list[str]:
        """返回当前可用的后端列表（display-resample 恒可用）。"""
        caps = self.check_dependencies(ls_exe_path=ls_exe_path)
        backends = ["display-resample"]
        if caps["lossless_scaling"]["available"]:
            backends.append("lossless-scaling")
        if caps["rife_torch"]["available"]:
            backends.append("rife-torch")
        return backends
