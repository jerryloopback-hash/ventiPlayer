"""Phase 1 前置探针：验证 vpy 帧倍增链全链路（无 torch 推理，CPU 均值替代）。

一次性验证 5 个未知数（结果写 probe_chain_result.json）:
  1. vsscript 与宿主是否共享 sys.modules（sys 注入属性双向检查）
  2. 帧对构造链: DuplicateFrames + Trim + StackHorizontal（时间平移 + 水平拼接）
  3. RGBS 三平面 get_read_ptr/get_write_ptr + get_stride 处理（含 stride 对齐）
  4. ModifyFrame 输出 2W 帧 → Crop 半宽 → Interleave([rgb, mid]) → AssumeFPS x2
  5. mpv 源的 clip.fps / num_frames / _Matrix props 可用性

通过判据: estimated_vf_fps ≈ 源 fps x2（24 → ~48），无 error/fatal 日志。
"""
import json
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
VENV = SPIKE.parent.parent / ".venv312"
VS_DIR = VENV / "Lib" / "site-packages" / "vapoursynth"
RESULT = SPIKE / "probe_chain_result.json"

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication([])
sys.path.insert(0, str(SPIKE.parent.parent))
from src.core.export.common import ensure_libmpv_on_path  # noqa: E402
ensure_libmpv_on_path()

os.environ["MIOPEN_USER_DB_PATH"] = str(SPIKE.parent.parent / ".miopen_cache")
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = os.environ["MIOPEN_USER_DB_PATH"]
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"

import torch  # noqa: E402
assert torch.cuda.is_available()

# 宿主侧 sys 注入（检查 vsscript 是否可见）
sys.venti_fg_probe_host = 12345

os.environ["PATH"] = str(VS_DIR) + os.pathsep + os.environ["PATH"]
import mpv  # noqa: E402

vpy = SPIKE / "probe_chain.vpy"
vpy.write_text(f"""\
import sys
import json
import ctypes
import numpy as np
import vapoursynth as vs

report = {{"host_attr": getattr(sys, "venti_fg_probe_host", None)}}
try:
    import torch
    report["torch_cuda"] = bool(torch.cuda.is_available())
except Exception as e:
    report["torch_err"] = str(e)[:120]

core = vs.core
src = video_in
report["fmt_id"] = src.format.id
report["fmt_name"] = src.format.name
try:
    fps = src.fps
    report["fps"] = f"{{fps.numerator}}/{{fps.denominator}}"
except Exception as e:
    fps = None
    report["fps_err"] = str(e)[:120]
report["num_frames"] = src.num_frames
report["src_w"] = src.width
report["src_h"] = src.height

# 注: 不能在脚本 init 期 get_frame(0) 读 props（mpv 报 "Frame requested during
# init" 且返回黑帧），矩阵检测放弃，统一按 709（HD 动画源的标准矩阵）。
rgb = src.resize.Bicubic(format=vs.RGBS, matrix_in_s="709")
report["rgb_fmt"] = rgb.format.name

N = rgb.num_frames
if not N or N <= 1:
    raise RuntimeError("num_frames unknown/too short")
# num_frames 是 0x7FFFFFF 哨兵（mpv 流式源），不做 DuplicateFrames；
# Trim(first=1) 使 pair[i]=[F_i|F_(i+1)]，最后一对的第i+1帧请求自然落到 EOF
shifted = rgb.std.Trim(first=1)
pair = core.std.StackHorizontal(clips=[rgb, shifted])
report["pair_w"] = pair.width

H, W = rgb.height, rgb.width
PF = np.float32

def mid_cb(n, f):
    h, w2 = f.height, f.width
    w = w2 // 2
    planes0, planes1, dst = [], [], []
    nf = f.copy()
    for p in range(3):
        stride = f.get_stride(p) // 4
        rp = ctypes.cast(f.get_read_ptr(p), ctypes.POINTER(ctypes.c_float))
        row = np.ctypeslib.as_array(rp, shape=(h, stride)).copy()
        planes0.append(row[:, :w])
        planes1.append(row[:, w:w * 2])
        dp = ctypes.cast(nf.get_write_ptr(p), ctypes.POINTER(ctypes.c_float))
        dst.append(np.ctypeslib.as_array(dp, shape=(h, stride))[:, :w])
    mid = [(a + b) * 0.5 for a, b in zip(planes0, planes1)]
    for d, m in zip(dst, mid):
        d[:] = m
    return nf

mid2w = pair.std.ModifyFrame(pair, mid_cb)
mid = mid2w.std.Crop(right=W)

out = core.std.Interleave(clips=[rgb, mid])
if fps and fps.numerator > 0:
    out = core.std.AssumeFPS(out, fpsnum=fps.numerator * 2, fpsden=fps.denominator)
    report["out_fps"] = f"{{fps.numerator * 2}}/{{fps.denominator}}"
else:
    report["out_fps"] = "assumed 48/1"
    out = core.std.AssumeFPS(out, fpsnum=48, fpsden=1)
out = out.resize.Bicubic(format=vs.YUV420P8, matrix_s="709")
out.set_output()
report["chain_ok"] = True

with open(r"{RESULT.as_posix()}", "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, indent=1)
""", encoding="utf-8")

errors, warns = [], []
player = mpv.MPV(vo="null", hwdec="no", idle="yes", keep_open="yes",
                 log_handler=lambda l, c, m:
                 (errors.append((l, c, m)) if l in ("error", "fatal")
                  else warns.append((l, c, m)) if l == "warn" else None),
                 loglevel="warn")
player.terminal = False

player["vf"] = f"vapoursynth=file={vpy.as_posix()}"
player.play(str(SPIKE / "test_360p24.mp4"))

deadline = time.time() + 45
vf_fps = None
while time.time() < deadline:
    try:
        pt = player.playback_time
        if pt is not None and pt > 1.5:
            try:
                vf_fps = player.estimated_vf_fps
            except Exception:
                pass
            break
    except Exception:
        pass
    time.sleep(0.1)

print(f"[probe] estimated_vf_fps={vf_fps}")
rep = {}
if RESULT.exists():
    rep = json.loads(RESULT.read_text(encoding="utf-8"))
print("[probe] vpy report:", json.dumps(rep, ensure_ascii=False, indent=1))
print(f"[probe] errors={len(errors)} warns={len(warns)}")
for e in errors[:12]:
    print("   E", str(e)[:160])
for w in warns[:6]:
    print("   W", str(w)[:160])

try:
    player.terminate()
except Exception:
    pass

# vpy 侧 sys 回写检查（若共享 interpreter，这里应能读到）
print(f"[probe] vpy_attr_visible_to_host={getattr(sys, 'venti_fg_probe_vpy', None)}")

ok = (not errors and vf_fps is not None and vf_fps > 35
      and rep.get("chain_ok") is True)
print("PROBE_RESULT|", "PASS" if ok else "FAIL", flush=True)
sys.stdout.flush()
os._exit(0 if ok else 1)
