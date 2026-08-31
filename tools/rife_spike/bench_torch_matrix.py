"""Phase 0-C 干净 GPU 全矩阵基准（torch ROCm RIFE）。

设计要点（按用户要求）:
  - 一个进程只跑一个模型的全配置集，结束后 os._exit(0) 保证退出、不留僵尸
  - 结果逐条增量写 report_torch_matrix.jsonl（原生崩溃也不丢已有数据）
  - pad 需求先用官方公式 max(128, int(128/scale))，失败自动尝试 2x/64/256 阶梯
  - 三种档位实现:
      native  : 原分辨率直推 (pad 到 128 倍数)
      official: 官方 scale 参数（模型内部金字塔缩放）
      down    : 用户侧降采样 → RIFE(scale=1.0) → 回升（0.5/0.75 档主实现）
  - 达标线: 24fps 源 x2 插帧需吞吐 >= 60fps（48 x 1.25 余量）

用法: python bench_torch_matrix.py --model v4_26|v4_25_lite|v4_25
"""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent
JSONL = SPIKE / "report_torch_matrix.jsonl"
MD = SPIKE / "report_torch_matrix.md"

# ---- ROCm 环境与 src/main.py 一致 ----
_ascii_base = str(REPO) if str(REPO).isascii() else os.environ.get("TEMP", ".")
_miopen = str(Path(_ascii_base) / ".miopen_cache")
os.environ["MIOPEN_USER_DB_PATH"] = _miopen
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = _miopen
os.makedirs(_miopen, exist_ok=True)
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
os.environ.setdefault("MIOPEN_LOG_LEVEL", "2")
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True, choices=["v4_26", "v4_25_lite", "v4_25"])
args = ap.parse_args()

import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402


def emit(row):
    row["ts"] = time.strftime("%H:%M:%S")
    with open(JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("ROW " + json.dumps(row, ensure_ascii=False), flush=True)


def gpu_sanity():
    x = torch.rand(4096, 4096, device="cuda", dtype=torch.float16)
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(10):
        y = x @ x
    torch.cuda.synchronize()
    tf = 10 * 2 * 4096**3 / (time.perf_counter() - t0) / 1e12
    del x, y
    torch.cuda.empty_cache()
    emit({"kind": "sanity", "gpu": torch.cuda.get_device_name(0),
          "torch": torch.__version__, "fp16_tflops": round(tf, 1)})
    return tf


def pad_mod_for(scale):
    """官方 inference_video.py 公式: tmp = max(128, int(128/scale))。"""
    return max(128, int(128 / scale))


def pad_to(h, w, mod):
    ph = (mod - h % mod) % mod
    pw = (mod - w % mod) % mod
    return h + ph, w + pw


def bench_config(model, dt, h, w, mode, scale, tag):
    """跑一个配置: 预热(含MIOpen编译) + 计时, 返回结果行。

    链路含真实 per-frame 开销: pad / 下采样 / 裁剪 / 回升。
    """
    ts = torch.tensor([0.5], device="cuda", dtype=dt)
    pad_mod = pad_mod_for(scale if mode == "official" else 1.0)

    try:
        if mode == "down":
            dh, dw = int(h * scale) // 2 * 2, int(w * scale) // 2 * 2
            p_h, p_w = pad_to(dh, dw, pad_mod)
            img0 = torch.rand(1, 3, h, w, device="cuda", dtype=dt)
            img1 = torch.rand(1, 3, h, w, device="cuda", dtype=dt)

            def run():
                a = F.pad(F.interpolate(img0, size=(dh, dw), mode="bilinear",
                                        align_corners=False),
                          (0, p_w - dw, 0, p_h - dh))
                b = F.pad(F.interpolate(img1, size=(dh, dw), mode="bilinear",
                                        align_corners=False),
                          (0, p_w - dw, 0, p_h - dh))
                out = model.inference(a, b, ts, scale=1.0)
                out = out[:, :, :dh, :dw]
                return F.interpolate(out, size=(h, w), mode="bilinear",
                                     align_corners=False)
        elif mode == "official":
            p_h, p_w = pad_to(h, w, pad_mod)
            img0 = torch.rand(1, 3, h, w, device="cuda", dtype=dt)
            img1 = torch.rand(1, 3, h, w, device="cuda", dtype=dt)

            def run():
                a = F.pad(img0, (0, p_w - w, 0, p_h - h))
                b = F.pad(img1, (0, p_w - w, 0, p_h - h))
                return model.inference(a, b, ts, scale=scale)
        else:  # native
            p_h, p_w = pad_to(h, w, pad_mod)
            img0 = torch.rand(1, 3, h, w, device="cuda", dtype=dt)
            img1 = torch.rand(1, 3, h, w, device="cuda", dtype=dt)

            def run():
                a = F.pad(img0, (0, p_w - w, 0, p_h - h))
                b = F.pad(img1, (0, p_w - w, 0, p_h - h))
                return model.inference(a, b, ts, scale=1.0)

        # 预热（含 MIOpen 首次编译）
        t0 = time.perf_counter()
        with torch.no_grad():
            out = run()
        torch.cuda.synchronize()
        first_s = time.perf_counter() - t0

        # 稳态: >=3s 且 >=10 iters, 上限 60
        n = 0
        t0 = time.perf_counter()
        with torch.no_grad():
            while True:
                out = run()
                n += 1
                el = time.perf_counter() - t0
                if (el >= 3.0 and n >= 10) or n >= 60:
                    break
        torch.cuda.synchronize()
        fps = n / (time.perf_counter() - t0)
        row = {"kind": "bench", "model": args.model,
               "dtype": "fp16" if dt == torch.float16 else "fp32",
               "res": f"{w}x{h}", "mode": mode, "scale": scale,
               "padded": f"{p_w}x{p_h}", "first_s": round(first_s, 2),
               "fps": round(fps, 1), "status": "OK"}
        del img0, img1, out
    except Exception as e:
        torch.cuda.empty_cache()
        row = {"kind": "bench", "model": args.model,
               "dtype": "fp16" if dt == torch.float16 else "fp32",
               "res": f"{w}x{h}", "mode": mode, "scale": scale,
               "status": "ERROR", "error": str(e)[:180]}
    torch.cuda.empty_cache()
    emit(row)
    return row


def main():
    tf = gpu_sanity()
    with open(MD, "a", encoding="utf-8") as f:
        f.write(f"\n## {args.model} @ {time.strftime('%H:%M:%S')} | "
                f"sanity {tf:.1f} TFLOPS\n\n")

    sys.path.insert(0, str(SPIKE / "torch_models"))
    sys.path.insert(0, str(SPIKE / "torch_models" / args.model))
    sys.path.insert(0, str(SPIKE / "torch_models" / args.model / "train_log"))
    from RIFE_HDv3 import Model
    mdir = str(SPIKE / "torch_models" / args.model / "train_log")

    RES = {"720p": (720, 1280), "1080p": (1080, 1920), "1440p": (1440, 2560)}

    # 配置集: (res, mode, scale)
    def configs_for(dtype):
        if dtype == "fp16":
            return [("1080p", "native", 1.0), ("1080p", "official", 0.75),
                    ("1080p", "official", 0.5), ("1080p", "down", 0.75),
                    ("1080p", "down", 0.5), ("720p", "native", 1.0),
                    ("1440p", "native", 1.0), ("1440p", "down", 0.5)]
        if args.model == "v4_26":  # 用户指定: v4.26 fp32 全档位
            return [("1080p", "native", 1.0), ("1080p", "official", 0.75),
                    ("1080p", "official", 0.5), ("1080p", "down", 0.75),
                    ("1080p", "down", 0.5)]
        return [("1080p", "native", 1.0)]

    for dtype_name in ("fp32", "fp16"):
        dt = torch.float16 if dtype_name == "fp16" else torch.float32
        model = Model()
        model.load_model(mdir, -1)
        model.eval()
        model.device()
        if dt == torch.float16:
            model.flownet.half()
        emit({"kind": "model_loaded", "model": args.model, "dtype": dtype_name})
        for (res, mode, scale) in configs_for(dtype_name):
            h, w = RES[res]
            bench_config(model, dt, h, w, mode, scale, res)

    # 生成 Markdown 小节后退出
    rows = []
    with open(JSONL, encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    with open(MD, "a", encoding="utf-8") as f:
        f.write("| dtype | res | mode | scale | padded | 首推s | fps | 状态 |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            if r.get("kind") != "bench" or r.get("model") != args.model:
                continue
            f.write(f"| {r['dtype']} | {r['res']} | {r['mode']} | {r['scale']} "
                    f"| {r.get('padded','-')} | {r.get('first_s','-')} "
                    f"| {r.get('fps','-')} | {r.get('status')} |\n")
    print(f"[done] {args.model} 全部配置完成，进程退出", flush=True)
    sys.stdout.flush()
    os._exit(0)  # 跳过 MIOpen/teardown 钩子，杜绝僵尸进程


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        with open(MD, "a", encoding="utf-8") as f:
            f.write(f"\nFATAL {args.model}: {traceback.format_exc()[-600:]}\n")
        os._exit(1)
