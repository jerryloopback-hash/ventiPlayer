"""流式推理共享组件：增量 overlap-add 与带 carry 的分块重采样。

供 AudioPipeline 流式管线使用，让 Apollo/FlashSR 边解码边转写，
内存占用与音频时长无关。设计保证：与批量 enhance() 的输出逐样本一致——

- 窗口起点均为 hop 的整数倍，且 padding 方式与批量版相同；
- 接缝区（相邻两窗重叠的 fade 区）的相对权重与批量版完全一致
  （Apollo 互补 fade / FlashSR 仅对后窗施加 fade_in）；
- 仅被单一窗口覆盖的样本，其权重在 result/counter 归一化时抵消，
  因此这类区域 fade 是否施加不影响结果，可安全地"宁多勿少"。

取消模型推理由 infer_fn 内部（或上层 per-block 检查）负责，本类不感知。
"""

from typing import Callable, Optional

import numpy as np
import torch


class StreamingOLA:
    """增量 overlap-add：按 hop 对齐吞入输入块，产出已定稿的输出样本。

    Args:
        nch: 声道数
        window: 模型输入窗口长度（样本）
        hop: 相邻窗口前进量（样本），window - hop = fade 时为标准交叉淡化
        fade: 交叉淡化区长度（样本）
        infer_fn: callable(torch.Tensor (nch, window)) -> (nch, window)，
            单窗口推理（CPU 入、CPU 出 float32），内部负责 .to(device)
        fade_in_vec: (fade,) 张量，后窗头部权重（批量版同款）
        fade_out_vec: (fade,) 张量，前窗尾部权重；None 表示批量版不施加
            （FlashSR 的接缝是"前窗权重 1 + 后窗 fade_in"）
    """

    def __init__(self, nch: int, window: int, hop: int, fade: int,
                 infer_fn: Callable[[torch.Tensor], torch.Tensor],
                 fade_in_vec: torch.Tensor,
                 fade_out_vec: Optional[torch.Tensor]):
        self._nch, self._window, self._hop, self._fade = nch, window, hop, fade
        self._infer = infer_fn
        self._fade_in = fade_in_vec
        self._fade_out = fade_out_vec
        # _in: 待处理输入（覆盖全局输入位置 [_start, _start+L)）
        # _acc/_norm: 输出累加器（与 _in 同起点对齐）
        # 不变量：_start 既是下一窗口起点，也是已产出（emitted）位置
        self._in = torch.zeros(nch, 0)
        self._acc = torch.zeros(nch, 0)
        self._norm = torch.zeros(0)
        self._start = 0

    def process(self, block: np.ndarray, last: bool = False) -> Optional[np.ndarray]:
        """吞入一块输入 (nch, T)，返回本轮新定稿的输出 (nch, M)；无则 None。

        last=True 表示输入就此结束（允许处理不足一窗的尾部）。"""
        x = torch.from_numpy(np.ascontiguousarray(block, dtype=np.float32))
        self._in = torch.cat([self._in, x], dim=1)
        out_chunks: list[np.ndarray] = []

        while True:
            L = self._in.shape[1]
            if L <= 0 or (not last and L < self._window):
                break
            m = min(self._window, L)
            win = self._in[:, :m]
            if m < self._window:
                win = torch.nn.functional.pad(win, (0, self._window - m))

            w = torch.ones(self._window)
            if self._start > 0 and m > self._fade:
                w[: self._fade] = self._fade_in
            # fade_out 的批量版条件是"s+hop 之后还有真实输入"。非末块时无法
            # 预知后续，一律施加：若实际无后继窗，该区为单窗覆盖，归一化抵消。
            if (self._fade_out is not None and m > self._fade
                    and (not last or L > self._hop)):
                w[m - self._fade: m] = self._fade_out

            out = self._infer(win)  # (nch, window)

            need = self._window - self._acc.shape[1]
            if need > 0:
                self._acc = torch.cat(
                    [self._acc, torch.zeros(self._nch, need)], dim=1)
                self._norm = torch.cat([self._norm, torch.zeros(need)], dim=0)
            self._acc += out * w
            self._norm += w

            # 处理完窗口 s 后，输出 [s, s+hop) 已定稿（两个覆盖窗都已处理）；
            # 末窗（m <= hop，即剩余不足一 hop）则全部定稿到真实末尾 m。
            emit_n = m if (last and m <= self._hop) else self._hop
            seg = self._acc[:, :emit_n] / self._norm[:emit_n].clamp_(min=1e-8)
            out_chunks.append(seg.numpy())

            self._in = self._in[:, self._hop:]
            self._acc = self._acc[:, emit_n:]
            self._norm = self._norm[emit_n:]
            self._start += emit_n

        if not out_chunks:
            return None
        return np.concatenate(out_chunks, axis=1)


class StreamingResampler:
    """带 carry 的分块 polyphase 重采样，与整轨 resample_poly 逐样本一致。

    关键约束：resample_poly 的输出网格锚定在输入起点（输出 k ↔ 输入
    k*down/up）。分块调用若起点任意，网格相位会错开并逐块漂移。故本类把
    块边界与 carry 长度都对齐到 down 的整数倍 —— buf_start*up/down 恒为
    整数，分块网格与整轨网格严格重合。另外每次产出前留出 FIR 支撑域宽度
    的输入样本待下轮带完整上下文重算，消除块尾边缘伪影。"""

    def __init__(self, orig_sr: int, target_sr: int, nch: int):
        from math import gcd
        g = gcd(int(orig_sr), int(target_sr))
        self._up, self._down = target_sr // g, orig_sr // g
        self._nch = nch
        # scipy resample_poly 的 kaiser FIR 半长 = 10*max(up,down)，作用在
        # 上采样域；换算到输入域 ≈ half/up 个输入样本
        self._span = 10 * max(self._up, self._down) // self._up + 2
        # carry：≥ 2× 支撑域 + 最大尾差，且为 down 的整数倍（网格对齐的关键）
        self._carry_n = ((2 * self._span + self._down) // self._down) * self._down
        self._carry = np.zeros((nch, 0), dtype=np.float32)
        self._buf_start = 0  # 下一块 x 的起点（全局输入位置，down 的整数倍）
        self._in_end = 0     # 已消费到的全局输入位置（down 的整数倍，末次除外）
        self._arrived = 0    # 已送达的总输入样本数（含尚未消费的尾差）
        self._out_total = 0  # 已产出的全局输出样本数

    def process(self, block: np.ndarray, last: bool = False) -> np.ndarray:
        """重采样一块 (nch, T) 输入，返回本次新定稿的输出 (nch, M)。"""
        from scipy.signal import resample_poly

        x = (np.concatenate([self._carry, block], axis=1)
             if self._carry.shape[1] else block)
        buf_start = self._buf_start
        m = buf_start // self._down  # buf_start 恒为 down 倍数 → m*up 精确
        y = np.stack([
            resample_poly(np.ascontiguousarray(ch), self._up, self._down)
            .astype(np.float32)
            for ch in x
        ])

        in_avail = self._arrived + block.shape[1]
        if last:
            in_end = in_avail                      # 末次：全部消费
            t_end = -(-in_end * self._up // self._down)   # ceil：含最后样本
        else:
            in_end = in_avail // self._down * self._down  # 对齐到 down 倍数
            hold = min(self._span, in_end - self._in_end)
            t_end = (in_end - hold) * self._up // self._down
        t_end = max(t_end, self._out_total)  # 产出位置只前进不回退

        k0 = self._out_total - m * self._up
        k1 = t_end - m * self._up
        k0c, k1c = max(0, k0), min(y.shape[1], max(k0, k1))

        self._out_total += k1c - k0c
        self._in_end = in_end
        self._arrived = in_avail

        if last:
            self._carry = np.zeros((self._nch, 0), dtype=np.float32)
            return (y[:, k0c:k1c] if k1c > k0c
                    else np.zeros((self._nch, 0), dtype=np.float32))

        # 新 carry：从 in_end - carry_n 起到 x 末尾（含未消费尾差，保证不丢样本）。
        # 起点 = in_end - carry_n 是 down 的倍数 → 网格对齐保持。
        cstart = in_end - self._carry_n - buf_start
        if cstart > 0:
            self._carry = x[:, cstart:].copy()
            self._buf_start = in_end - self._carry_n
        else:
            # 消费前沿尚未越过 carry_n：整块留作 carry，起点不变
            self._carry = x.copy()
            self._buf_start = buf_start
        return (y[:, k0c:k1c] if k1c > k0c
                else np.zeros((self._nch, 0), dtype=np.float32))
