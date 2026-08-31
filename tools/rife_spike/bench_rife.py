"""RIFE spike 吞吐量基准（Phase 0.2）。

用法:
    python bench_rife.py --w 1920 --h 1080 --model v4_25_lite --backend ncnn_vk \
        --fp16 --num-streams 2 --frames 100 [--scale 0.5]

- 源用 std.BlankClip（RIFE 计算量只取决于分辨率，与运动内容无关，
  Blank 的 flow≈0 对 grid_sample 计算量影响可忽略，吞吐数据有效）。
- 达标线: 吞吐 >= 源fps*2*1.25（1080p24→48 需 >=60fps）。
- 输出: 推理吞吐 fps（含 resize/格式转换开销，即真实 vf 链口径）。
"""
import argparse
import sys
import time
from pathlib import Path

SPIKE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SPIKE_DIR / "runtime"))

import vapoursynth as vs

core = vs.core
_p = SPIKE_DIR / "runtime" / "plugins"
# vsmlrt 导入时通过 core.ort/ncnn.Version() 定位插件目录，必须先加载
core.std.LoadPlugin(str(_p / "vsort.dll"))
core.std.LoadPlugin(str(_p / "vsncnn.dll"))

from vsmlrt import RIFE, RIFEModel, Backend


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=int, default=1920)
    ap.add_argument("--h", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--model", default="v4_25_lite",
                    choices=["v4_25", "v4_25_lite", "v4_26", "v4_22", "v4_22_lite"])
    ap.add_argument("--backend", default="ncnn_vk", choices=["ncnn_vk", "ort_dml"])
    ap.add_argument("--fp16", action="store_true")
    ap.add_argument("--num-streams", type=int, default=1)
    ap.add_argument("--scale", type=float, default=1.0,
                    help="用户侧降采样档位: 1.0/0.75/0.5（降采样→RIFE→回升）")
    ap.add_argument("--frames", type=int, default=100)
    ap.add_argument("--impl", type=int, default=2, choices=[1, 2])
    ap.add_argument("--tile", type=int, default=0, help="impl1 用的分块边长(需被 mod 整除)")
    args = ap.parse_args()

    core = vs.core
    p = SPIKE_DIR / "runtime" / "plugins"

    backend = {
        ("ncnn_vk", True): lambda: Backend.NCNN_VK(fp16=True, device_id=0,
                                                   num_streams=args.num_streams),
        ("ncnn_vk", False): lambda: Backend.NCNN_VK(device_id=0,
                                                    num_streams=args.num_streams),
        ("ort_dml", True): lambda: Backend.ORT_DML(fp16=True, device_id=0,
                                                   num_streams=args.num_streams, verbosity=5),
        ("ort_dml", False): lambda: Backend.ORT_DML(device_id=0,
                                                    num_streams=args.num_streams),
    }[(args.backend, args.fp16)]()

    model = RIFEModel[args.model]

    # BlankClip 直接给 RGBS（vsmlrt RIFE 要求 RGB float），绕开颜色转换开销差异
    clip = core.std.BlankClip(width=args.w, height=args.h,
                              fpsnum=args.fps * 1000, fpsden=1001,
                              format=vs.RGBS, color=[128, 128, 128])

    w2, h2 = int(args.w * args.scale) // 2 * 2, int(args.h * args.scale) // 2 * 2
    if args.scale != 1.0:
        clip = core.resize.Spline16(clip, width=w2, height=h2)

    kw = {}
    if args.impl == 2:
        # v2 实现：模型内部 padding，动态分辨率
        kw["_implementation"] = 2
    else:
        # impl1：画面尺寸须被 mod 整除（lite=128, 其余=32），手动 pad，输出再裁回
        mod = 128 if "lite" in args.model else 32
        pw = (mod - w2 % mod) % mod if args.scale != 1.0 else (mod - args.w % mod) % mod
        ph = (mod - h2 % mod) % mod if args.scale != 1.0 else (mod - args.h % mod) % mod
        if pw or ph:
            clip = clip.std.AddBorders(right=pw, bottom=ph)
    clip = RIFE(clip, multi=2, model=model, backend=backend, **kw)

    if args.impl == 1 and (pw or ph):
        clip = clip.std.Crop(right=pw, bottom=ph)

    if args.scale != 1.0:
        clip = core.resize.Spline16(clip, width=args.w, height=args.h)

    # 预热（首次推理含 onnx 序列化/内核编译，不计入）
    t0 = time.perf_counter()
    clip.get_frame(0)
    warm = time.perf_counter() - t0

    t0 = time.perf_counter()
    for i in range(1, args.frames):
        clip.get_frame(i)
    dt = time.perf_counter() - t0

    fps = (args.frames - 1) / dt
    target = args.fps * 2 * 1.25
    tag = (f"w={args.w} h={args.h} src_fps={args.fps} model={args.model} "
           f"backend={args.backend} fp16={args.fp16} streams={args.num_streams} "
           f"scale={args.scale}")
    print(f"[bench] {tag}")
    print(f"[bench] 预热 {warm:.2f}s | 稳态吞吐 {fps:.1f} fps | "
          f"达标线 {target:.0f} fps | {'PASS' if fps >= target else 'FAIL'}")
    print(f"RESULT|{tag}|{fps:.2f}|{'PASS' if fps >= target else 'FAIL'}")


if __name__ == "__main__":
    main()
