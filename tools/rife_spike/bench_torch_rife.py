"""Phase 0-C: torch ROCm RIFE 吞吐基准（决策门 A 的 torch 路线实测）。

用 Practical-RIFE 官方权重（flownet.pkl）+ 官方推理代码测真实吞吐:
  RIFE_HDv3.Model.inference(img0, img1, timestep, scale) —— 含 pad/warp/merge 全链
测试矩阵: {v4_25_lite, v4_25} x {fp16, fp32} x {scale 1.0, 0.5} @ 1080p24
达标线: >= 60 fps（48 x 1.25）

环境变量与 src/main.py 保持一致（MIOpen/hipBLASLt 规避），预热数据真实反映
应用内首次启动成本。
"""
import argparse
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent

# 与 src/main.py 相同的 ROCm 规避项
_ascii_base = REPO if str(REPO).isascii() else os.environ.get("TEMP", ".")
_miopen_cache = str(Path(_ascii_base) / ".miopen_cache")
os.environ["MIOPEN_USER_DB_PATH"] = _miopen_cache
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = _miopen_cache
os.makedirs(_miopen_cache, exist_ok=True)
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
os.environ.setdefault("MIOPEN_LOG_LEVEL", "2")
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"

ap = argparse.ArgumentParser()
ap.add_argument("--model", default="v4_25_lite", choices=["v4_25_lite", "v4_25"])
ap.add_argument("--fp16", action="store_true", default=True)
ap.add_argument("--fp32", action="store_true")
ap.add_argument("--scale", type=float, default=1.0)
ap.add_argument("--w", type=int, default=1920)
ap.add_argument("--h", type=int, default=1080)
ap.add_argument("--fps", type=int, default=24)
ap.add_argument("--iters", type=int, default=60)
args = ap.parse_args()

import torch  # noqa: E402

sys.path.insert(0, str(SPIKE / "torch_models"))  # model/ 包与 train_log 包路径
sys.path.insert(0, str(SPIKE / "torch_models" / args.model))
sys.path.insert(0, str(SPIKE / "torch_models" / args.model / "train_log"))
from RIFE_HDv3 import Model  # noqa: E402

dev = torch.device("cuda")
dt = torch.float16 if (args.fp16 and not args.fp32) else torch.float32
model = Model()
model.load_model(str(SPIKE / "torch_models" / args.model / "train_log"), -1)
model.eval()
model.device()
if dt == torch.float16:
    model.half()

print(f"[torch] device={torch.cuda.get_device_name(0)} dtype={dt} "
      f"model={args.model} scale={args.scale}")

H, W = args.h, args.w
img0 = torch.rand(1, 3, H, W, device=dev, dtype=dt)
img1 = torch.rand(1, 3, H, W, device=dev, dtype=dt)
timestep = torch.tensor([0.5], device=dev, dtype=dt)

# 预热（MIOpen 算子查找/编译）
with torch.no_grad():
    for _ in range(5):
        out = model.inference(img0, img1, timestep, scale=args.scale)
torch.cuda.synchronize()

t0 = time.perf_counter()
with torch.no_grad():
    for _ in range(args.iters):
        out = model.inference(img0, img1, timestep, scale=args.scale)
torch.cuda.synchronize()
dt_s = time.perf_counter() - t0

fps = args.iters / dt_s
target = args.fps * 2 * 1.25
tag = (f"w={W} h={H} src_fps={args.fps} model={args.model} "
       f"dtype={dt} scale={args.scale}")
print(f"[torch] 稳态吞吐 {fps:.1f} fps | 达标线 {target:.0f} fps | "
      f"{'PASS' if fps >= target else 'FAIL'}")
print(f"RESULT|{tag}|{fps:.2f}|{'PASS' if fps >= target else 'FAIL'}")
