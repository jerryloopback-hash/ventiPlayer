"""生成 testsrc2 合成测试视频（h264 yuv420p），供 vf_vapoursynth 探针用。

用法: python gen_testvideo.py out.mp4 640 360 24 300   (w h fps 帧数)
"""
import sys

import av


def main(out, w, h, fps, frames):
    cont = av.open(out, "w")
    stream = cont.add_stream("libx264", rate=fps)
    stream.width, stream.height, stream.pix_fmt = w, h, "yuv420p"
    stream.options = {"preset": "ultrafast", "crf": "20"}
    for i in range(frames):
        img = cont.add_stream  # placeholder never used
        frame = av.VideoFrame(w, h, "yuv420p")
        # 用 testsrc2 滤镜直接产帧太绕；改为程序化生成移动物条纹画面
        import numpy as np
        y = np.zeros((h, w), dtype=np.uint8)
        x = (np.arange(w) + i * 8) % w
        y[:] = (np.tile(x, (h, 1))) % 256
        u = np.full((h // 2, w // 2), 128 + (i % 40), dtype=np.uint8)
        v = np.full((h // 2, w // 2), 128 - (i % 40), dtype=np.uint8)
        av_frame = av.VideoFrame.from_ndarray(
            __import__("numpy").stack([y, _up(u, h, w), _up(v, h, w)]),
            format="yuv444p")
        # yuv444p→yuv420p 转换交给 swscale
        frame = av_frame.reformat(width=w, height=h, format="yuv420p")
        for pkt in stream.encode(frame):
            cont.mux(pkt)
    for pkt in stream.encode():
        cont.mux(pkt)
    cont.close()
    print(f"gen: {out} {w}x{h}@{fps} x{frames}frames")


def _up(plane, h, w):
    import numpy as np
    return np.kron(plane, np.ones((2, 2), dtype=plane.dtype))[:h, :w]


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]),
         int(sys.argv[4]), int(sys.argv[5]))
