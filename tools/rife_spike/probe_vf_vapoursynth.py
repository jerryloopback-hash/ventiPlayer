"""Phase 0.3b 组合冒烟：PySide6 + torch(ROCm) + libmpv(vf_vapoursynth+RIFE) 同进程。

历史事故: 在 PySide6 宿主进程中 import vapoursynth 曾触发原生崩溃 0xe24c4a02。
本探针验证真实宿主环境下 RIFE vpy 能否在 mpv 内运行:
  1. 先 import PySide6 + torch 并做一次真实 GPU 运算（模拟主程序加载顺序）
  2. PATH 注入 venv 的 vapoursynth 目录（Phase 1 的 PATH 管理预演）
  3. libmpv 播放测试视频，挂 vf=vapoursynth:file=<vpy>（vpy 内走 vsmlrt RIFE）
  4. 检查 estimated-vf-fps 是否翻倍（24→~48）、有无 VS/ncnn/DML 致命错误

已验证的坑（不要改回去）:
  - vf 子选项是 file= 且路径必须正斜杠（反斜杠破坏 vf 列表解析器）
  - keep_open 下 eof_reached 不触发，用 playback_time 判定
  - 该 libmpv 的 lavfi:// 协议被禁用，必须用真实文件源
用法: python probe_vf_vapoursynth.py [--no-torch] [--backend ncnn_vk|ort_dml]
"""
import argparse
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
VENV = SPIKE.parent.parent / ".venv312"
VS_DIR = VENV / "Lib" / "site-packages" / "vapoursynth"

ap = argparse.ArgumentParser()
ap.add_argument("--no-torch", action="store_true")
ap.add_argument("--backend", default="ncnn_vk", choices=["ncnn_vk", "ort_dml"])
args = ap.parse_args()

# ---- 1. 模拟主程序环境 ----
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication([])
sys.path.insert(0, str(SPIKE.parent.parent))
from src.core.export.common import ensure_libmpv_on_path  # noqa: E402
ensure_libmpv_on_path()

if not args.no_torch:
    import torch
    assert torch.cuda.is_available(), "torch ROCm 不可用"
    _ = torch.zeros(8, device="cuda") * 2
    print(f"[probe] torch OK: {torch.cuda.get_device_name(0)}")
else:
    print("[probe] torch 跳过")

# ---- 2. PATH 注入 ----
os.environ["PATH"] = str(VS_DIR) + os.pathsep + os.environ["PATH"]

import mpv  # noqa: E402

# ---- 3. 生成 vpy ----
vpy = SPIKE / "probe_rife.vpy"
if args.backend == "ncnn_vk":
    backend_expr = "Backend.NCNN_VK(fp16=True)"
else:
    backend_expr = "Backend.ORT_DML(fp16=True)"
vpy.write_text(f"""\
import sys
sys.path.insert(0, r"{(SPIKE / 'runtime').as_posix()}")
import vapoursynth as vs
from vsmlrt import RIFE, RIFEModel, Backend

core = vs.core
clip = video_in
clip = clip.resize.Bicubic(format=vs.RGBS, matrix_in_s="709")
clip = RIFE(clip, multi=2, model=RIFEModel.v4_25_lite, backend={backend_expr}, _implementation=2)
clip = clip.resize.Bicubic(format=vs.YUV420P8, matrix_s="709")
clip.set_output()
""", encoding="utf-8")

errors, logs = [], []
player = mpv.MPV(vo="null", hwdec="no", idle="yes", keep_open="yes",
                 log_handler=lambda l, c, m:
                 (errors.append((l, c, m)) if l in ("error", "fatal")
                  else logs.append((l, c, m))),
                 loglevel="debug")
player.terminal = False

t0 = time.perf_counter()
vpy_fwd = vpy.as_posix()
player["vf"] = f"vapoursynth=file={vpy_fwd}"
player.play(str(SPIKE / "test_360p24.mp4"))

deadline = time.time() + 90
vf_fps = None
while time.time() < deadline:
    try:
        pt = player.playback_time
        if pt is not None and pt > 2.0:
            try:
                vf_fps = player.estimated_vf_fps
            except Exception:
                pass
            if vf_fps:
                break
    except Exception:
        pass
    time.sleep(0.1)

elapsed = time.perf_counter() - t0
print(f"[probe] backend={args.backend} 耗时 {elapsed:.1f}s")
print(f"[probe] estimated-vf-fps = {vf_fps} (源 24 → 期望 ~48)")

ml_logs = [l for l in logs + errors
           if any(k in str(l).lower() for k in ("rife", "ncnn", "directml", "mlrt", "vulkan"))]
print(f"[probe] ML 相关日志 {len(ml_logs)} 条:")
for l in ml_logs[:8]:
    print("   ", str(l)[:200])
fatal = [e for e in errors]
print(f"[probe] 错误/致命 {len(errors)} 条:")
for e in errors[:8]:
    print("   ", str(e)[:200])

player.terminate()
ok = vf_fps is not None and vf_fps > 30 and not fatal
print("PROBE_RESULT|", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
