"""Phase 1 集成测试：rife_service 生成的 vpy + mpv 内 torch RIFE 推理整链验证。

验证点:
  1. write_vpy() 产出的 vpy 在 mpv vf_vapoursynth 内评估成功（权重加载/缓存复用）
  2. torch RIFE 推理在 mpv 进程内正常出帧，estimated_vf_fps ≈ 源 x2
  3. 无 error/fatal 日志；进程自动退出（防 MIOpen 僵尸）

通过判据: estimated_vf_fps >= 40（24fps 源 x2 = 48），errors == 0。
"""
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent
VENV = REPO / ".venv312"
VS_DIR = VENV / "Lib" / "site-packages" / "vapoursynth"

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication([])
sys.path.insert(0, str(REPO))
from src.core.export.common import ensure_libmpv_on_path  # noqa: E402
ensure_libmpv_on_path()

# 模拟 src/main.py 的环境准备
os.environ["PATH"] = str(VS_DIR) + os.pathsep + os.environ["PATH"]
os.environ["MIOPEN_USER_DB_PATH"] = str(REPO / ".miopen_cache")
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = os.environ["MIOPEN_USER_DB_PATH"]
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"

from src.core.rife_service import RifeFrameGenService  # noqa: E402
import torch  # noqa: E402
assert torch.cuda.is_available()

import argparse as _ap
_ap = _ap.ArgumentParser()
_ap.add_argument("--video", default=str(SPIKE / "test_360p24.mp4"))
_ap.add_argument("--model", default="v4_25_lite")
_ap.add_argument("--scale", type=float, default=0.75)
_ap.add_argument("--min-fps", type=float, default=40)
_args = _ap.parse_args()
svc = RifeFrameGenService(config_dir=Path.home() / ".ventiplayer")
vpy = svc.write_vpy(_args.model, _args.scale, 24.0)
VIDEO = _args.video
print(f"[it] vpy={vpy}", flush=True)

# 模型构建（不含尺寸预热，尺寸预热在读取视频后做）

# 从视频读实际尺寸供 prime（避免与后台任务撞同一 MIOpen 配置编译）
import av as _av  # noqa: E402
with _av.open(VIDEO) as _c:
    _st = _c.streams.video[0]
    _w, _h = _st.codec_context.width, _st.codec_context.height
print(f"[it] video {_w}x{_h}", flush=True)
t0 = time.perf_counter()
svc.prime(_args.model, _args.scale, _w, _h)
print(f"[it] prime2 {time.perf_counter()-t0:.1f}s", flush=True)

import mpv  # noqa: E402
errors = []
player = mpv.MPV(vo="null", hwdec="no", idle="yes", keep_open="yes",
                 log_handler=lambda l, c, m: errors.append((l, c, m))
                 if l in ("error", "fatal") else None,
                 loglevel="warn")
player.terminal = False
player["vf"] = svc.vf_arg(vpy)
player.play(VIDEO)

deadline = time.time() + 60
vf_fps = None
while time.time() < deadline:
    try:
        pt = player.playback_time
        if pt is not None and pt > 1.5:
            time.sleep(0.5)  # 让 estimated-vf-fps 平滑
            try:
                vf_fps = player.estimated_vf_fps
            except Exception:
                pass
            break
    except Exception:
        pass
    time.sleep(0.1)

print(f"[it] estimated_vf_fps={vf_fps}", flush=True)
print(f"[it] errors={len(errors)}", flush=True)
for e in errors[:12]:
    print("   E", str(e), flush=True)
try:
    player.terminate()
except Exception:
    pass
ok = (not errors and vf_fps is not None and vf_fps >= _args.min_fps)
print("IT_RESULT|", "PASS" if ok else "FAIL", flush=True)
sys.stdout.flush()
os._exit(0 if ok else 1)
