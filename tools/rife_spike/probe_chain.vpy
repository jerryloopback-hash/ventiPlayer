import sys
import json
import ctypes
import numpy as np
import vapoursynth as vs

report = {"host_attr": getattr(sys, "venti_fg_probe_host", None)}
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
    report["fps"] = f"{fps.numerator}/{fps.denominator}"
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
    report["out_fps"] = f"{fps.numerator * 2}/{fps.denominator}"
else:
    report["out_fps"] = "assumed 48/1"
    out = core.std.AssumeFPS(out, fpsnum=48, fpsden=1)
out = out.resize.Bicubic(format=vs.YUV420P8, matrix_s="709")
out.set_output()
report["chain_ok"] = True

with open(r"D:/devWorkshopForCC/20260517-VentiPlayer/tools/rife_spike/probe_chain_result.json", "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, indent=1)
