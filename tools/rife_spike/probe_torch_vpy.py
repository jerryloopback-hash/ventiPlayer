"""Phase 0.3c 集成探针：vpy 内直接调用 torch(ROCm) 做 GPU 推理。

vsscript 在 mpv 进程内用同一个 python312.dll 执行 vpy —— 与宿主共享解释器，
`import torch` 返回宿主已加载的模块（零重复初始化）。vpy 对每帧 R 平面做
真实 GPU 往返（+0.1），验证 "mpv → VS → torch → VS → mpv" 整链。

已验证的坑（不要改回去）:
  - vf 子选项 file= 且路径必须正斜杠
  - keep_open 下用 playback_time 判定，不用 eof_reached
  - 逐帧处理用 std.ModifyFrame（FrameEval 是"返回整段 clip"语义，会挂死）
  - 进程末尾 os._exit 保底，防 mpv/VS teardown 挂死变僵尸
"""
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
VENV = SPIKE.parent.parent / ".venv312"
VS_DIR = VENV / "Lib" / "site-packages" / "vapoursynth"

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication([])
sys.path.insert(0, str(SPIKE.parent.parent))
from src.core.export.common import ensure_libmpv_on_path  # noqa: E402
ensure_libmpv_on_path()

os.environ["MIOPEN_USER_DB_PATH"] = str(SPIKE.parent.parent / ".miopen_cache")
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = os.environ["MIOPEN_USER_DB_PATH"]
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"
os.environ["MIOPEN_FIND_MODE"] = "FAST"
os.environ["MIOPEN_LOG_LEVEL"] = "2"

import torch  # noqa: E402
assert torch.cuda.is_available()
_ = torch.rand(8, device="cuda")
print(f"[probe] torch OK: {torch.cuda.get_device_name(0)}", flush=True)

os.environ["PATH"] = str(VS_DIR) + os.pathsep + os.environ["PATH"]
import mpv  # noqa: E402

vpy = SPIKE / "probe_torch.vpy"
vpy.write_text("""\
import torch
import numpy as np
import vapoursynth as vs

core = vs.core
clip = video_in
clip = clip.resize.Bicubic(format=vs.RGBS, matrix_in_s="709")

import ctypes

def boost(n, f):
    h, w = f.height, f.width
    src = ctypes.cast(f.get_read_ptr(0), ctypes.POINTER(ctypes.c_float))
    arr = np.ctypeslib.as_array(src, shape=(h, w)).copy()
    t = torch.from_numpy(arr).cuda().add_(0.1).clamp_(0, 1)
    nf = f.copy()
    dst = ctypes.cast(nf.get_write_ptr(0), ctypes.POINTER(ctypes.c_float))
    np.ctypeslib.as_array(dst, shape=(h, w))[:] = t.cpu().numpy()
    return nf

clip = clip.std.ModifyFrame(clip, boost)
clip = clip.resize.Bicubic(format=vs.YUV420P8, matrix_s="709")
clip.set_output()
""", encoding="utf-8")

errors, logs = [], []
player = mpv.MPV(vo="null", hwdec="no", idle="yes", keep_open="yes",
                 log_handler=lambda l, c, m:
                 (errors.append((l, c, m)) if l in ("error", "fatal")
                  else logs.append((l, c, m))),
                 loglevel="warn")
player.terminal = False

t0 = time.perf_counter()
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

print(f"[probe] 耗时 {time.perf_counter()-t0:.1f}s, estimated-vf-fps={vf_fps}",
      flush=True)
print(f"[probe] 错误 {len(errors)} 条:", flush=True)
for e in errors[:14]:
    print("   ", str(e)[:160], flush=True)
try:
    player.terminate()
except Exception:
    pass
ok = not errors and vf_fps is not None and vf_fps > 20
print("PROBE_RESULT|", "PASS" if ok else "FAIL", flush=True)
sys.stdout.flush()
os._exit(0 if ok else 1)  # 保底退出
