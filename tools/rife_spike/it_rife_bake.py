"""Phase 2 集成测试：RIFE 插帧烘焙 pass（PyAV 解码 → 共享内核 → 重编码 2x fps）。

验证点:
  1. 纯 RIFE 烘焙：_bake_video_rife 产出 fps=x2 的视频，帧数 = 2N-1（尾帧无中点）
  2. 组合链：RIFE pass 产中间文件 → _bake_video_pyav 再烘焙，fps 保持 x2
  3. 中点帧与相邻源帧内容不同（确实生成了新帧，非复制）
  4. 进程自动退出（防 MIOpen 僵尸）

通过判据: fps≈48（24fps 源 x2）、帧数正确、中点帧非复制、零异常。
"""
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent

sys.path.insert(0, str(REPO))
os.environ["MIOPEN_USER_DB_PATH"] = str(REPO / ".miopen_cache")
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = os.environ["MIOPEN_USER_DB_PATH"]
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"

import av  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402
assert torch.cuda.is_available()

import argparse as _ap
_ap = _ap.ArgumentParser()
_ap.add_argument("--video", default=str(SPIKE / "test_1080p24.mp4"))
_ap.add_argument("--model", default="v4_25_lite")
_ap.add_argument("--scale", type=float, default=0.75)
_args = _ap.parse_args()

OUT_DIR = SPIKE / "runtime"
OUT_DIR.mkdir(parents=True, exist_ok=True)

from src.core.export.common import ExportSettings  # noqa: E402
from src.core.video_export import VideoExporter  # noqa: E402


def probe(path: Path) -> dict:
    with av.open(str(path)) as c:
        vs_ = c.streams.video[0]
        n = 0
        first_mid_differs = None
        prev_arr = None
        for fr in c.decode(vs_):
            arr = fr.to_ndarray()
            if n == 1 and prev_arr is not None:
                first_mid_differs = not np.array_equal(arr, prev_arr)
            prev_arr = arr
            n += 1
        rate = float(vs_.average_rate)
    return {"frames": n, "fps": rate, "mid_differs": first_mid_differs}


es = ExportSettings(output_path=str(OUT_DIR / "bake_pure.mp4"),
                    src_fps=24.0, framegen={})
exporter = VideoExporter(None)

# ── 1. 纯 RIFE 烘焙 ──────────────────────────────────────────────────────
t0 = time.perf_counter()
info = exporter._bake_video_rife(
    _args.video, None, {"model": _args.model, "scale": _args.scale},
    str(OUT_DIR / "bake_pure.mp4"), pure=True)
t_bake = time.perf_counter() - t0
print(f"[it] pure bake {t_bake:.1f}s info={info}", flush=True)

with av.open(_args.video) as c:
    src_n = sum(1 for _ in c.decode(c.streams.video[0]))
    src_fps = float(c.streams.video[0].average_rate)

r1 = probe(OUT_DIR / "bake_pure.mp4")
print(f"[it] pure result: {r1}", flush=True)
exp_frames = 2 * src_n - 1
ok1 = (abs(r1["fps"] - src_fps * 2) < 0.01 and r1["frames"] == exp_frames
       and r1["mid_differs"])
print(f"[it] check1: fps {r1['fps']:.3f} vs {src_fps * 2:.3f} | "
      f"frames {r1['frames']} vs {exp_frames} | mid_differs={r1['mid_differs']}",
      flush=True)

# ── 2. 组合链：RIFE 中间文件 → PyAV 近似烘焙 ─────────────────────────────
t0 = time.perf_counter()
info2 = exporter._bake_video_rife(
    _args.video, None, {"model": _args.model, "scale": _args.scale},
    str(OUT_DIR / "bake_intermediate.mp4"), pure=False)
es2 = ExportSettings(output_path=str(OUT_DIR / "bake_combined.mp4"),
                     src_fps=24.0, framegen={})
exporter._bake_video_pyav(str(OUT_DIR / "bake_intermediate.mp4"), None,
                          es2, str(OUT_DIR / "bake_combined.mp4"))
t_combo = time.perf_counter() - t0
r2 = probe(OUT_DIR / "bake_combined.mp4")
print(f"[it] combined {t_combo:.1f}s: {r2}", flush=True)
ok2 = (abs(r2["fps"] - src_fps * 2) < 0.01 and r2["frames"] == exp_frames)

print(f"[it] check2: fps {r2['fps']:.3f} | frames {r2['frames']} "
      f"vs {exp_frames}", flush=True)

ok = bool(ok1 and ok2)
print("IT_RESULT|", "PASS" if ok else "FAIL", flush=True)
sys.stdout.flush()
os._exit(0 if ok else 1)
