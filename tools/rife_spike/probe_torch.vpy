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
