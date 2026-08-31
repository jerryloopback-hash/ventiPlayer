"""torch ROCm RIFE 隔离探针：直接写日志文件（无管道缓冲），阶梯分辨率 + fp32/fp16。

用法: python probe_torch_stage.py fp32|fp16
"""
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent
LOG = SPIKE / "probe_torch_stage.log"

cache = REPO / ".miopen_cache"
os.environ["MIOPEN_USER_DB_PATH"] = str(cache)
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = str(cache)
os.makedirs(cache, exist_ok=True)
os.environ["MIOPEN_FIND_MODE"] = "FAST"
os.environ["MIOPEN_LOG_LEVEL"] = "2"
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"

dtype_name = sys.argv[1] if len(sys.argv) > 1 else "fp32"
rife_scale = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
log_f = open(LOG, "a", buffering=1)


def say(msg):
    log_f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


say(f"=== probe start dtype={dtype_name} scale={rife_scale} pid={os.getpid()} ===")
try:
    import torch
    say(f"torch {torch.__version__} cuda={torch.cuda.is_available()}")
    sys.path.insert(0, str(SPIKE / "torch_models"))
    sys.path.insert(0, str(SPIKE / "torch_models" / "v4_25_lite"))
    sys.path.insert(0, str(SPIKE / "torch_models" / "v4_25_lite" / "train_log"))
    from RIFE_HDv3 import Model
    say("import ok")
    m = Model()
    m.load_model(str(SPIKE / "torch_models" / "v4_25_lite" / "train_log"), -1)
    m.eval()
    m.device()
    dt = torch.float32
    if dtype_name == "fp16":
        m.flownet.half()
        dt = torch.float16
    say("model ready")

    if rife_scale != 1.0:
        # scale<1: 官方要求 pad 到 max(128, 128/scale) 倍数
        pad_mod = int(128 / rife_scale)
    for (h, w) in ([(720, 1280), (1080, 1920)] if rife_scale != 1.0
                   else [(256, 256), (540, 960), (720, 1280), (1080, 1920)]):
        # 官方 inference_video.py: pad 到 max(128, 128/scale) 的倍数
        pm = pad_mod if rife_scale != 1.0 else 128
        ph = (pm - h % pm) % pm
        pw = (pm - w % pm) % pm
        img0 = torch.rand(1, 3, h + ph, w + pw, device="cuda", dtype=dt)
        img1 = torch.rand(1, 3, h + ph, w + pw, device="cuda", dtype=dt)
        ts = torch.tensor([0.5], device="cuda", dtype=dt)
        say(f"{h}x{w} first inference ...")
        t0 = time.perf_counter()
        with torch.no_grad():
            out = m.inference(img0, img1, ts, scale=rife_scale)
        torch.cuda.synchronize()
        say(f"{h}x{w} first done {time.perf_counter()-t0:.2f}s")

        t0 = time.perf_counter()
        n = 20
        with torch.no_grad():
            for _ in range(n):
                out = m.inference(img0, img1, ts, scale=rife_scale)
        torch.cuda.synchronize()
        fps = n / (time.perf_counter() - t0)
        say(f"{h}x{w} steady {fps:.1f} fps")
    say("=== probe end OK ===")
except Exception:
    import traceback
    say("EXCEPTION:\n" + traceback.format_exc())
finally:
    log_f.close()
