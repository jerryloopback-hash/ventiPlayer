"""Phase 1.5 性能归因：真实链路分阶段计时（vs bench 纯推理的差距定位）。

用 --untimed + vo=null 让 mpv 自由消耗帧 → 墙钟吞吐 = 真实处理能力。
vpy 回调内部按 6 阶段累计耗时，每 120 帧落盘 PROBE 文件：
  read   : VS 帧 → numpy/tensor 三平面（CPU 拷贝）
  h2d    : .cuda().to(DT) 上传
  down   : interpolate 降采样 + pad
  infer  : model.inference
  up     : crop + 回升 + clamp + .cpu()（D2H）
  write  : f.copy() + 平面写回

用法: python it_profile_chain.py [--video xxx] [--model v4_25_lite] [--scale 0.5]
"""
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent
VENV = REPO / ".venv312"
VS_DIR = VENV / "Lib" / "site-packages" / "vapoursynth"
PROFILE = SPIKE / "profile_result.txt"

import argparse as _ap
_ap = _ap.ArgumentParser()
_ap.add_argument("--video", default=str(SPIKE / "test_1080p24.mp4"))
_ap.add_argument("--model", default="v4_25_lite")
_ap.add_argument("--scale", type=float, default=0.5)
_ap.add_argument("--threads", default="")
_args = _ap.parse_args()

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(REPO))
from src.core.export.common import ensure_libmpv_on_path  # noqa: E402
ensure_libmpv_on_path()
os.environ["PATH"] = str(VS_DIR) + os.pathsep + os.environ["PATH"]
os.environ["MIOPEN_USER_DB_PATH"] = str(REPO / ".miopen_cache")
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = os.environ["MIOPEN_USER_DB_PATH"]
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"

import torch  # noqa: E402
assert torch.cuda.is_available()
from src.models.rife import get_model  # noqa: E402

model = get_model(_args.model, fp16=True)
print("[prof] model loaded", flush=True)

PROFILE.write_text("", encoding="utf-8")

vpy = SPIKE / "profile_chain.vpy"
vpy.write_text(f"""\
import sys, time, ctypes
sys.path.insert(0, {str(REPO)!r})
import numpy as np
import torch
import torch.nn.functional as F
import vapoursynth as vs

from src.models.rife import get_model

MODEL = {_args.model!r}
MODE = {'"down"' if _args.scale < 1.0 else '"native"'}
SCALE = {_args.scale!r}
PROF = {str(PROFILE)!r}

model = get_model(MODEL, fp16=True)
DT = torch.float16
TS = torch.tensor([0.5], device="cuda", dtype=DT)

core = vs.core
src = video_in

# 单一专用推理线程：torch/MIOpen handle 是 per-thread 的，VS 多工作线程轮流
# 调回调会让每个新线程首帧触发 handle 初始化+kernel 加载（~数百 ms/次），
# 是实测 infer 114ms(基准7ms) 的主因。收拢到单线程后 handle 终生复用。
import threading as _th, queue as _q
_in_q = _q.Queue()
def _worker():
    while True:
        job = _in_q.get()
        if job is None:
            return
        fn, args, box = job
        try:
            box["r"] = fn(*args)
        except BaseException as e:
            box["e"] = e
_th.Thread(target=_worker, daemon=True).start()

def run_infer(a, b):
    box = {{"ev": _th.Event()}}
    _in_q.put((model.inference, (a, b, TS), box))
    box["ev"].wait()
    if "e" in box:
        raise box["e"]
    return box["r"]
H, W = src.height, src.width
rgb = src.resize.Bicubic(format=vs.RGBS, matrix_in_s="709")
shifted = rgb.std.Trim(first=1)
pair = core.std.StackHorizontal(clips=[rgb, shifted])

PAD = 128
if MODE == "down":
    DH = int(H * SCALE) // 2 * 2
    DW = int(W * SCALE) // 2 * 2
else:
    DH, DW = H, W
PH = (PAD - DH % PAD) % PAD
PW = (PAD - DW % PAD) % PAD

STATS = {{"n": 0, "read": 0.0, "h2d": 0.0, "down": 0.0, "infer": 0.0,
         "up": 0.0, "write": 0.0, "t0": time.perf_counter()}}

def _half(f, x0, x1):
    h, w = f.height, x1 - x0
    planes = []
    for p in range(3):
        stride = f.get_stride(p) // 4
        ptr = ctypes.cast(f.get_read_ptr(p), ctypes.POINTER(ctypes.c_float))
        row = np.ctypeslib.as_array(ptr, shape=(h, stride))
        planes.append(torch.from_numpy(np.ascontiguousarray(row[:, x0:x1])))
    return torch.stack(planes)

def rife_cb(n, f):
    t00 = time.perf_counter()
    t0 = _half(f, 0, f.width // 2)
    t1 = _half(f, f.width // 2, f.width)
    t_read = time.perf_counter()
    t0 = t0.cuda().to(DT)
    t1 = t1.cuda().to(DT)
        t_h2d = time.perf_counter()
        if MODE == "down":
            a = F.interpolate(t0[None], size=(DH, DW), mode="bilinear", align_corners=False)
            b = F.interpolate(t1[None], size=(DH, DW), mode="bilinear", align_corners=False)
            a = F.pad(a, (0, PW, 0, PH))
            b = F.pad(b, (0, PW, 0, PH))
            t_down = time.perf_counter()
            out = run_infer(a, b)
            t_inf = time.perf_counter()
            out = out[:, :, :DH, :DW]
            out = F.interpolate(out.float(), size=(H, W), mode="bilinear", align_corners=False)[0]
        else:
            a = F.pad(t0[None], (0, PW, 0, PH))
            b = F.pad(t1[None], (0, PW, 0, PH))
            t_down = time.perf_counter()
            out = run_infer(a, b)
            t_inf = time.perf_counter()
            out = out[0, :, :H, :W].float()
        mid = out.clamp_(0.0, 1.0).cpu().numpy()
        t_up = time.perf_counter()
    nf = f.copy()
    for p in range(3):
        stride = nf.get_stride(p) // 4
        ptr = ctypes.cast(nf.get_write_ptr(p), ctypes.POINTER(ctypes.c_float))
        dst = np.ctypeslib.as_array(ptr, shape=(H, stride))
        dst[:, :W] = mid[p]
    t_write = time.perf_counter()
    s = STATS
    s["n"] += 1
    s["read"] += t_read - t00
    s["h2d"] += t_h2d - t_read
    s["down"] += t_down - t_h2d
    s["infer"] += t_inf - t_down
    s["up"] += t_up - t_inf
    s["write"] += t_write - t_up
    if s["n"] % 120 == 0:
        el = time.perf_counter() - s["t0"]
        with open(PROF, "a", encoding="utf-8") as fh:
            fh.write(f"n={{s['n']}} wall={{el:.1f}}s pairs_per_s={{s['n']/el:.1f}} "
                     f"avg_ms: read={{s['read']/s['n']*1e3:.1f}} h2d={{s['h2d']/s['n']*1e3:.1f}} "
                     f"down={{s['down']/s['n']*1e3:.1f}} infer={{s['infer']/s['n']*1e3:.1f}} "
                     f"up={{s['up']/s['n']*1e3:.1f}} write={{s['write']/s['n']*1e3:.1f}}\\n")
    return nf

mid2w = pair.std.ModifyFrame(pair, rife_cb)
mid = mid2w.std.Crop(right=W)
out = core.std.Interleave(clips=[rgb, mid])
out = core.std.AssumeFPS(out, fpsnum=48, fpsden=1)
out = out.resize.Bicubic(format=vs.YUV420P8, matrix_s="709")
out.set_output()
""", encoding="utf-8")

import mpv  # noqa: E402
errors = []
player = mpv.MPV(vo="null", hwdec="no", idle="yes", keep_open="yes",
                 untimed="yes",  # 自由消耗帧，墙钟吞吐=真实处理能力
                 log_handler=lambda l, c, m: errors.append((l, c, m))
                 if l in ("error", "fatal") else None,
                 loglevel="warn")
player.terminal = False
s_path = vpy.as_posix()
player["vf"] = f"vapoursynth=file=%{len(s_path.encode('utf-8'))}%{s_path}"
player.play(_args.video)

# 等 40s 墙钟（或播放结束），期间轮询 PROBE
t_end = time.time() + 40
last = ""
while time.time() < t_end:
    try:
        if player.idle_active:
            break
    except Exception:
        pass
    if PROFILE.exists():
        txt = PROFILE.read_text(encoding="utf-8")
        if txt and txt != last:
            last = txt
            print(txt.strip().splitlines()[-1], flush=True)
    time.sleep(2)

try:
    player.terminate()
except Exception:
    pass
print(f"[prof] errors={len(errors)}", flush=True)
for e in errors[:6]:
    print("   E", str(e)[:200], flush=True)
print("[prof] FINAL:", last.strip().splitlines()[-1] if last else "(无)", flush=True)
sys.stdout.flush()
os._exit(0)
