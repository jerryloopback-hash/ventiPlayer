"""RIFE 推理共享内核：实时 vpy 模板与导出烘焙共用的 YUV 域处理链（Phase 2 抽出）。

从 rife_service._VPY_TEMPLATE 原样抽出（三轮实测沉淀，数学逐行等价，勿改）：
  - 精确相位 420↔444 转换（exact_up2 + 偶数位下采样，往返零误差；F.interpolate
    的两种 align_corners 语义都不是 420 相位——False=半像素偏移、True=k(n-1)/(2n-1)
    网格，改回去会引入色度偏移）
  - 单一专用推理线程（torch/MIOpen handle 是 per-thread 的，实测新线程首推
    ~700ms；worker 终生复用。模块级 get_kernel 缓存使宿主与多次 vpy 求值共享
    同一 worker，避免每次换视频重付初始化）
  - YUV↔RGB 数学（BT.709/601/2020 × limited/full，矩阵/范围按帧元数据动态）
  - down 模式：用户侧降采样推理再回升（0.75 等非 2 幂档位官方 scale 不支持，
    必须走 down）

本模块只会在 torch 可用的上下文被导入（实时 vpy 求值 / 导出烘焙 pass）。
"""

import queue
import threading

import torch
import torch.nn.functional as F

# _Matrix 属性值 → (Kr, Kg, Kb)。1=BT.709, 5/6=BT.601, 9=BT.2020。
# 缺省落 709（与实时链一致：mpv 源无 _Matrix prop 时同样按 709 处理）。
MATRICES = {1: (0.2126, 0.7152, 0.0722), 5: (0.299, 0.587, 0.114),
            6: (0.299, 0.587, 0.114), 9: (0.2627, 0.6780, 0.0593)}

# 推理 pad 公式（Phase 0 实测对三版本统一适用）
PAD = 128


def _ranges(bits: int, full: bool):
    """归一化域的 Y/C 偏移与跨度。limited 8bit: 16/219；10bit: 64/876（C: 512/896）。"""
    maxv = float((1 << bits) - 1)
    if full:
        return 0.0, 1.0, 0.5, 1.0
    if bits <= 8:
        return 16.0 / 255, 219.0 / 255, 128.0 / 255, 224.0 / 255
    return 64.0 / maxv, 876.0 / maxv, 512.0 / maxv, 896.0 / maxv


def exact_up2(t):
    """420→444 精确相位 2x 双线性: 偶数位置=样本值, 奇数位置=邻均值。

    与下采样取偶数位置互逆（整条 YUV→RGB→YUV 往返零误差）。勿改用
    F.interpolate——其 align_corners 两种语义都不是 420 相位。
    """
    tp = F.pad(t[None, None], (0, 0, 0, 1), mode="replicate")[0, 0]
    rows = torch.empty(t.shape[0] * 2, t.shape[1], device=t.device, dtype=t.dtype)
    rows[0::2] = t
    rows[1::2] = (tp[:-1] + tp[1:]) / 2
    tp2 = F.pad(rows[None, None], (0, 1, 0, 0), mode="replicate")[0, 0]
    cols = torch.empty(rows.shape[0], t.shape[1] * 2, device=t.device, dtype=t.dtype)
    cols[:, 0::2] = rows
    cols[:, 1::2] = (tp2[:, :-1] + tp2[:, 1:]) / 2
    return cols


def planes_to_rgb(y, u, v, *, bits: int, full: bool, mtx, dt):
    """YUV 平面 GPU 张量（任意 uint）→ (1,3,H,W) RGB 张量（dt 精度）。"""
    maxv = float((1 << bits) - 1)
    y_off, y_span, c_mid, c_span = _ranges(bits, full)
    kr, kg, kb = mtx
    c_r, c_b = 2 * (1 - kb), 2 * (1 - kr)
    y01 = (y.float() / maxv - y_off) / y_span
    u01 = (u.float() / maxv - c_mid) / c_span
    v01 = (v.float() / maxv - c_mid) / c_span
    u_up = exact_up2(u01)
    v_up = exact_up2(v01)
    r = y01 + c_r * v_up
    b = y01 + c_b * u_up
    g = (y01 - kr * r - kb * b) / kg
    return torch.stack([r, g, b])[None].to(dt)


def rgb_to_planes(rgb, *, bits: int, full: bool, mtx):
    """(1或3维均可传入的) RGB 张量 → (y,u,v) numpy 平面（源位深，420 色度）。

    一次 .cpu() 同步取回三平面（多次同步各 ~5ms 是 Phase 1.5 实测主要开销）。
    """
    r, g, b = rgb[0], rgb[1], rgb[2]
    maxv = float((1 << bits) - 1)
    y_off, y_span, c_mid, c_span = _ranges(bits, full)
    kr, kg, kb = mtx
    c_r, c_b = 2 * (1 - kb), 2 * (1 - kr)
    y2 = kr * r + kg * g + kb * b
    cb = (b - y2) / c_b
    cr = (r - y2) / c_r
    dtype = torch.uint8 if bits <= 8 else torch.uint16
    h, w = r.shape
    flat = torch.cat([((y2 * y_span + y_off) * maxv).round().to(dtype).reshape(-1),
                      ((cb[::2, ::2] * c_span + c_mid) * maxv).round().to(dtype).reshape(-1),
                      ((cr[::2, ::2] * c_span + c_mid) * maxv).round().to(dtype).reshape(-1)])
    all_np = flat.cpu().numpy()
    y_np = all_np[:h * w].reshape(h, w)
    u_np = all_np[h * w:h * w + (h // 2) * (w // 2)].reshape(h // 2, w // 2)
    v_np = all_np[h * w + (h // 2) * (w // 2):].reshape(h // 2, w // 2)
    return y_np, u_np, v_np


def padded_shape(h: int, w: int, scale: float):
    """(h,w,scale) → (dh,dw,ph,pw)：down 模式先缩到 scale，再 pad 到 128 倍数。"""
    if scale < 1.0:
        dh, dw = int(h * scale) // 2 * 2, int(w * scale) // 2 * 2
    else:
        dh, dw = h, w
    ph = (PAD - dh % PAD) % PAD
    pw = (PAD - dw % PAD) % PAD
    return dh, dw, ph, pw


class MidpointInferencer:
    """单 worker 线程的中点帧推理器（MIOpen handle per-thread，线程终生复用）。"""

    def __init__(self, model, fp16: bool = True):
        self.model = model
        self.dt = torch.float16 if fp16 else torch.float32
        self.ts = torch.tensor([0.5], device="cuda", dtype=self.dt)
        self._q: "queue.Queue" = queue.Queue()
        self._th = threading.Thread(target=self._worker, daemon=True,
                                    name="rife-midpoint-infer")
        self._th.start()

    def _worker(self):
        while True:
            box = self._q.get()
            if box is None:
                return
            try:
                with torch.no_grad():
                    box["r"] = self.model.inference(box["a"], box["b"],
                                                    self.ts, scale=1.0)
            except BaseException as e:  # noqa: BLE001 — 异常原样回传等待方
                box["e"] = e
            box["ev"].set()

    def submit(self, a, b) -> dict:
        box = {"a": a, "b": b, "ev": threading.Event()}
        self._q.put(box)
        return box

    def infer(self, a, b):
        """同步推理一次；worker 内异常原样抛出。"""
        box = self.submit(a, b)
        box["ev"].wait()
        if "e" in box:
            raise box["e"]
        return box["r"]

    def warm(self, h: int, w: int, scale: float) -> None:
        """按分辨率做一次 dummy 推理：初始化 worker 的 MIOpen handle、
        触发新分辨率 kernel 编译落盘缓存（新配置可达分钟级，命中后毫秒级）。"""
        dh, dw, ph, pw = padded_shape(h, w, scale)
        t = torch.zeros(1, 3, dh + ph, dw + pw, device="cuda", dtype=self.dt)
        try:
            self.infer(t, t)
        finally:
            del t

    def midpoint(self, f0, f1, *, bits: int, full: bool, mtx,
                 scale: float, out_h: int, out_w: int):
        """一对 YUV 帧 (y,u,v) GPU uint 张量 → 中点帧 (y,u,v) numpy 平面。

        scale<1 走 down 模式（降采样推理再回升），与实时链语义一致。
        """
        rgb0 = planes_to_rgb(*f0, bits=bits, full=full, mtx=mtx, dt=self.dt)
        rgb1 = planes_to_rgb(*f1, bits=bits, full=full, mtx=mtx, dt=self.dt)
        dh, dw, ph, pw = padded_shape(out_h, out_w, scale)
        if scale < 1.0:
            a = F.pad(F.interpolate(rgb0, size=(dh, dw), mode="bilinear",
                                    align_corners=False), (0, pw, 0, ph))
            b = F.pad(F.interpolate(rgb1, size=(dh, dw), mode="bilinear",
                                    align_corners=False), (0, pw, 0, ph))
            o = self.infer(a, b)[:, :, :dh, :dw]
            o = F.interpolate(o.float(), size=(out_h, out_w), mode="bilinear",
                              align_corners=False)[0]
        else:
            a = F.pad(rgb0, (0, pw, 0, ph))
            b = F.pad(rgb1, (0, pw, 0, ph))
            o = self.infer(a, b)[0, :, :out_h, :out_w].float()
        return rgb_to_planes(o.clamp_(0.0, 1.0), bits=bits, full=full, mtx=mtx)


_KERNEL_CACHE: dict = {}
_KERNEL_LOCK = threading.Lock()


def get_kernel(model_version: str, fp16: bool = True) -> MidpointInferencer:
    """进程级内核缓存（(model, fp16) → 实例）。宿主/多次 vpy 求值/烘焙共享同一
    worker 线程与 MIOpen handle，推理请求在队列上自然串行化。"""
    key = (model_version, bool(fp16))
    with _KERNEL_LOCK:
        kernel = _KERNEL_CACHE.get(key)
        if kernel is None:
            from src.models.rife import get_model
            kernel = MidpointInferencer(get_model(model_version, fp16=fp16),
                                        fp16=fp16)
            _KERNEL_CACHE[key] = kernel
        return kernel
