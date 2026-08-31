# -*- coding: utf-8 -*-
"""Phase 1.5b YUV 域链分阶段计时：生成生产 vpy → 注入 STATS 计时 → untimed 墙钟。"""
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent
VENV = REPO / ".venv312"
VS_DIR = VENV / "Lib" / "site-packages" / "vapoursynth"
PROFILE = SPIKE / "profile_yuv_result.txt"

import argparse as _ap
_ap = _ap.ArgumentParser()
_ap.add_argument("--video", default=str(SPIKE / "test_1080p24.mp4"))
_ap.add_argument("--model", default="v4_25_lite")
_ap.add_argument("--scale", type=float, default=0.75)
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
from src.core.rife_service import RifeFrameGenService  # noqa: E402

svc = RifeFrameGenService(config_dir=Path.home() / ".ventiplayer")
vpy_path = svc.write_vpy(_args.model, _args.scale, 24.0)
text = vpy_path.read_text(encoding="utf-8")

# 注入分阶段计时
text = text.replace(
    "def rife_cb(n, f):",
    '''STATS = {"n": 0, "read": 0.0, "cvt": 0.0, "pre": 0.0, "wait": 0.0,
         "up": 0.0, "d2h": 0.0, "write": 0.0, "t0": time.perf_counter()}

def rife_cb(n, f):
    _t00 = time.perf_counter()''', 1)
text = text.replace(
    "        rgb0 = _to_rgb(halves[0]).to(DT)",
    '''        _t_read = time.perf_counter()
        rgb0 = _to_rgb(halves[0]).to(DT)''', 1)
text = text.replace(
    "        rgb1 = _to_rgb(halves[1]).to(DT)",
    '''        rgb1 = _to_rgb(halves[1]).to(DT)
        _t_cvt = time.perf_counter()''', 1)
text = text.replace(
    "            box = _submit_infer(a, b)",
    '''            _t_pre = time.perf_counter()
            box = _submit_infer(a, b)''', 1)
text = text.replace(
    '''            box["ev"].wait()
            if "e" in box:
                raise box["e"]
            o = box["r"][:, :, :DH, :DW]''',
    '''            box["ev"].wait()
            _t_wait = time.perf_counter()
            if "e" in box:
                raise box["e"]
            o = box["r"][:, :, :DH, :DW]''', 1)
text = text.replace(
    '''            box["ev"].wait()
            if "e" in box:
                raise box["e"]
            o = box["r"][0, :, :H, :W].float()''',
    '''            box["ev"].wait()
            _t_wait = time.perf_counter()
            if "e" in box:
                raise box["e"]
            o = box["r"][0, :, :H, :W].float()''', 1)
text = text.replace(
    '''        flat = torch.cat([((y2 * y_span + y_off) * MAXV).round().to(_DTYPE).reshape(-1),''',
    '''        _t_up = time.perf_counter()
        flat = torch.cat([((y2 * y_span + y_off) * MAXV).round().to(_DTYPE).reshape(-1),''', 1)
text = text.replace(
    '''        v_np = all_np[H * W + (H // 2) * (W // 2):].reshape(H // 2, W // 2)''',
    '''        v_np = all_np[H * W + (H // 2) * (W // 2):].reshape(H // 2, W // 2)
        _t_d2h = time.perf_counter()''', 1)
text = text.replace(
    '''    nf = f.copy()
    el = 1 if B <= 8 else 2''',
    '''    _t2 = time.perf_counter()
    s = STATS
    s["n"] += 1
    s["read"] += _t_read - _t00
    s["cvt"] += _t_cvt - _t_read
    s["pre"] += _t_pre - _t_cvt
    s["wait"] += _t_wait - _t_pre
    s["up"] += _t_up - _t_wait
    s["d2h"] += _t_d2h - _t_up
    if s["n"] % 60 == 0:
        el = time.perf_counter() - s["t0"]
        with open(r"D:/devWorkshopForCC/20260517-VentiPlayer/tools/rife_spike/profile_yuv_result.txt", "a", encoding="utf-8") as fh:
            fh.write(f"n={s['n']} wall={el:.1f}s pairs_per_s={s['n']/el:.1f} "
                     f"avg_ms: read={s['read']/s['n']*1e3:.1f} cvt={s['cvt']/s['n']*1e3:.1f} "
                     f"pre={s['pre']/s['n']*1e3:.1f} wait={s['wait']/s['n']*1e3:.1f} "
                     f"up={s['up']/s['n']*1e3:.1f} d2h={s['d2h']/s['n']*1e3:.1f} "
                     f"write={(time.perf_counter()-_t2 if False else 0):.1f}\\n")
    nf = f.copy()
    el = 1 if B <= 8 else 2''', 1)

# 模板顶部需 import time
if "import time" not in text:
    text = text.replace("import sys\n", "import sys\nimport time\n", 1)

vpy2 = SPIKE / "profile_yuv.vpy"
vpy2.write_text(text, encoding="utf-8")
print("[prof] vpy written", flush=True)

import mpv  # noqa: E402
errors = []
player = mpv.MPV(vo="null", hwdec="no", idle="yes", keep_open="yes",
                 untimed="yes",
                 log_handler=lambda l, c, m: errors.append((l, c, m))
                 if l in ("error", "fatal") else None,
                 loglevel="warn")
player.terminal = False
s_path = vpy2.as_posix()
player["vf"] = f"vapoursynth=file=%{len(s_path.encode('utf-8'))}%{s_path}"
player.play(_args.video)

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
    print("   E", str(e)[:240], flush=True)
print("[prof] FINAL:", last.strip().splitlines()[-1] if last else "(无)", flush=True)
sys.stdout.flush()
os._exit(0)
