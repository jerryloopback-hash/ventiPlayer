"""RIFE 真插帧服务（torch ROCm 推理 + VapourSynth vpy 帧倍增链）。

架构（Phase 0 spike 定案，详见 tools/rife_spike/report_torch_matrix.md）:
  mpv 的 vf_vapoursynth 在宿主进程内用同一个 python312.dll 评估 vpy —— vpy 与
  宿主共享 interpreter（Phase 1 探针实测 sys 属性互通），因此:
    1. 宿主后台线程 prime(): 构建模型 + MIOpen 预热 → src.models.rife 模块级缓存
    2. vpy 直接 `from src.models.rife import get_model` 复用缓存，零重复加载

  帧倍增链（probe_vpy_chain 验证）:
    RGBS → Trim(first=1) 时间平移 → StackHorizontal 帧对 [F_i | F_i+1]
    → ModifyFrame(torch RIFE 求中点，写回左半) → Crop 半宽
    → Interleave(原帧, 中点) → AssumeFPS x2 → 转回 YUV420P8/10

已实测约束（勿改回）:
  - vf 子选项 file= 且路径必须正斜杠（反斜杠破坏 mpv vf 列表解析）
  - mpv 源 clip.fps=0/1、num_frames=0x7FFFFFF 哨兵 → fps 由宿主 container-fps
    写入 vpy 字面量；长度不修补（尾帧插值对自然落 EOF）
  - 脚本 init 期禁止 get_frame(0)（mpv 报 frame-during-init 且回黑帧）→ 矩阵固定 709
  - RGBS 三平面按 get_stride(p)//4 步长读写（帧内存有 stride 对齐）
  - pad 公式 max(128, 128/scale)：down 模式推理用 scale=1.0 → 三版本统一 pad 128
  - MIOpen 新分辨率配置首次编译可能耗时（磁盘缓存命中后毫秒级）→ prime 按当前
    视频分辨率预热；换视频后由 video_output_changed 驱动重新 prime
"""

import logging
import threading
from fractions import Fraction
from pathlib import Path
from string import Template

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# vpy 模板：$MODEL/$FP16/$MODE/$SCALE/$FPS_NUM/$FPS_DEN/$REPO 由生成时注入。
# 逻辑保持与 probe_vpy_chain.py 一致（该探针已在真机验证整链）。
_VPY_TEMPLATE = Template('''\
# VentiPlayer RIFE 真插帧 —— 由 src/core/rife_service.py 生成，勿手改
# 配置: model=$MODEL fp16=$FP16 mode=$MODE scale=$SCALE out_fps=$FPS_NUM/$FPS_DEN
import sys
_REPO = "$REPO"
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
import ctypes
import numpy as np
import torch
import torch.nn.functional as F
import vapoursynth as vs

from src.models.rife import get_model

ERRLOG = r"$ERRLOG"
MODEL = "$MODEL"
FP16 = $FP16
MODE = "$MODE"          # down: 用户侧降采样后推理再回升; native: 原分辨率直推
SCALE = $SCALE
FPS_NUM = $FPS_NUM
FPS_DEN = $FPS_DEN

model = get_model(MODEL, fp16=bool(FP16))   # 宿主 prime 后直接命中缓存
DT = torch.float16 if FP16 else torch.float32
TS = torch.tensor([0.5], device="cuda", dtype=DT)

core = vs.core
src = video_in
H, W = src.height, src.width
if src.format.color_family == vs.RGB:
    rgb = src.resize.Bicubic(format=vs.RGBS)
else:
    # init 期不可 get_frame(0) 读 _Matrix（mpv 会回黑帧警告），HD 源统一按 709
    rgb = src.resize.Bicubic(format=vs.RGBS, matrix_in_s="709")

# 帧对: pair[i] = [F_i | F_i+1]。mpv 源 num_frames 为哨兵值，不做长度修补；
# 尾帧对的 F_i+1 请求自然落 EOF，不缺帧不崩溃（probe_vpy_chain 验证）。
shifted = rgb.std.Trim(first=1)
pair = core.std.StackHorizontal(clips=[rgb, shifted])

PAD = 128
if MODE == "down":
    DH = int(H * SCALE) // 2 * 2
    DW = int(W * SCALE) // 2 * 2
else:
    DH, DW = H, W
PH = (PAD - DH % PAD) % PAD
PW = (PAD - DW % PAD) % PAD

def _half(f, x0, x1):
    """读帧三平面列区间 [x0:x1] → (3, h, w) cpu fp32 tensor（含 stride 处理）。"""
    h, w = f.height, x1 - x0
    planes = []
    for p in range(3):
        stride = f.get_stride(p) // 4
        ptr = ctypes.cast(f.get_read_ptr(p), ctypes.POINTER(ctypes.c_float))
        row = np.ctypeslib.as_array(ptr, shape=(h, stride))
        planes.append(torch.from_numpy(np.ascontiguousarray(row[:, x0:x1])))
    return torch.stack(planes)

def rife_cb(n, f):
    # VS/mpv 日志通道会截断深层 traceback，回调异常必须自行落盘才能定位根因
    try:
        with torch.no_grad():
            t0 = _half(f, 0, f.width // 2).cuda().to(DT)
            t1 = _half(f, f.width // 2, f.width).cuda().to(DT)
            if MODE == "down":
                a = F.interpolate(t0[None], size=(DH, DW), mode="bilinear",
                                  align_corners=False)
                b = F.interpolate(t1[None], size=(DH, DW), mode="bilinear",
                                  align_corners=False)
                a = F.pad(a, (0, PW, 0, PH))
                b = F.pad(b, (0, PW, 0, PH))
                out = model.inference(a, b, TS, scale=1.0)
                out = out[:, :, :DH, :DW]
                out = F.interpolate(out.float(), size=(H, W), mode="bilinear",
                                    align_corners=False)[0]
            else:
                a = F.pad(t0[None], (0, PW, 0, PH))
                b = F.pad(t1[None], (0, PW, 0, PH))
                out = model.inference(a, b, TS, scale=1.0)[0, :, :H, :W].float()
            mid = out.clamp_(0.0, 1.0).cpu().numpy()
    except BaseException:
        # VS/mpv 日志通道截断深层 traceback，异常必须自行落盘/打 stderr 才能定位
        try:
            import traceback as _tb
            _msg = _tb.format_exc()
        except BaseException:
            _msg = "<format_exc failed>"
        try:
            with open(ERRLOG, "a", encoding="utf-8") as _fh:
                _fh.write(f"===== frame {n} =====\\n" + _msg[-4000:] + "\\n")
        except BaseException:
            pass
        try:
            print("RIFE_CB_ERROR\\n" + _msg, file=sys.stderr, flush=True)
        except BaseException:
            pass
        raise
    nf = f.copy()
    for p in range(3):
        stride = nf.get_stride(p) // 4
        ptr = ctypes.cast(nf.get_write_ptr(p), ctypes.POINTER(ctypes.c_float))
        dst = np.ctypeslib.as_array(ptr, shape=(H, stride))
        dst[:, :W] = mid[p]
    return nf

mid2w = pair.std.ModifyFrame(pair, rife_cb)
mid = mid2w.std.Crop(right=W)
out = core.std.Interleave(clips=[rgb, mid])
out = core.std.AssumeFPS(out, fpsnum=FPS_NUM * 2, fpsden=FPS_DEN)
BITS = src.format.bits_per_sample
out = out.resize.Bicubic(format=vs.YUV420P8 if BITS <= 8 else vs.YUV420P10,
                         matrix_s="709")
out.set_output()
''')


def fps_to_fraction(src_fps: float) -> tuple[int, int]:
    """容器帧率 → 有理数（23.976 → 24000/1001，24.0 → 24/1）。"""
    frac = Fraction(float(src_fps)).limit_denominator(1001)
    return frac.numerator, frac.denominator


class RifeFrameGenService:
    """RIFE 真插帧编排：vpy 生成 + 后台预热（模型构建/MIOpen 编译）。

    线程模型: prime() 阻塞式（调用方放后台线程）；write_vpy() 纯文本写入。
    模型实例缓存在 src.models.rife 模块级（vpy 与宿主共享 interpreter，直接复用）。
    """

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = Path(config_dir) if config_dir else (Path.home() / ".ventiplayer")
        self.runtime_dir = self.config_dir / "runtime"
        self._prime_lock = threading.Lock()

    # ---- vpy 生成 ----

    def write_vpy(self, model_version: str, scale: float, src_fps: float,
                  fp16: bool = True) -> Path:
        """按当前配置与源帧率生成 vpy，返回路径（供 vf=vapoursynth=file=... 使用）。

        vpy 内部分辨率/pad 从源 clip 动态读取，同一文件对不同分辨率视频自适应；
        fps 是字面量，源帧率变化时需重新生成（playback 侧由 video_output_changed 驱动）。
        """
        if not src_fps or src_fps <= 0:
            raise ValueError(f"无效的源帧率: {src_fps}")
        fps_num, fps_den = fps_to_fraction(src_fps)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        vpy = self.runtime_dir / "rife_fg.vpy"
        vpy.write_text(_VPY_TEMPLATE.substitute(
            REPO=_REPO_ROOT.as_posix(),
            ERRLOG=(self.runtime_dir / "rife_fg_error.log").as_posix(),
            MODEL=model_version,
            FP16=1 if fp16 else 0,
            MODE="down" if scale < 1.0 else "native",
            SCALE=repr(float(scale)),
            FPS_NUM=fps_num,
            FPS_DEN=fps_den,
        ), encoding="utf-8")
        logger.info("RIFE vpy 已生成: %s (model=%s scale=%s fps=%s/%s)",
                    vpy, model_version, scale, fps_num, fps_den)
        return vpy

    @staticmethod
    def vf_arg(vpy_path: Path) -> str:
        """vf=vapoursynth 的属性值。

        路径用 mpv 的 %n% 长度前缀转义（n 按 UTF-8 字节数）：vf 子选项以 ':' 分隔，
        "file=C:/..." 的盘符冒号会被切掉（实测 file 值只剩 "/Users/..."，再相对
        cwd 盘符解析，跨盘必挂）。%n% 强制解析器按长度读取，盘符保留。
        """
        s = Path(vpy_path).as_posix()
        n = len(s.encode("utf-8"))
        return f"vapoursynth=file=%{n}%{s}"

    # ---- 预热 ----

    def prime(self, model_version: str, scale: float, width: int, height: int,
              fp16: bool = True) -> None:
        """构建模型并按视频实际配置预热一次（MIOpen 编译落盘缓存）。

        阻塞数秒（权重已缓存+MIOpen 命中）到数十秒（新分辨率配置编译）。
        必须在后台线程调用。失败抛异常，由调用方转降级。
        """
        with self._prime_lock:
            import torch
            import torch.nn.functional as F

            from src.models.rife import get_model

            model = get_model(model_version, fp16=fp16)
            if width <= 0 or height <= 0:
                return  # 无视频时只保证模型就绪，尺寸预热留给首次评估

            dt = torch.float16 if fp16 else torch.float32
            if scale < 1.0:
                dh, dw = int(height * scale) // 2 * 2, int(width * scale) // 2 * 2
            else:
                dh, dw = height, width
            ph = (128 - dh % 128) % 128
            pw = (128 - dw % 128) % 128
            a = torch.rand(1, 3, dh, dw, device="cuda", dtype=dt)
            if ph or pw:
                a = F.pad(a, (0, pw, 0, ph))
            ts = torch.tensor([0.5], device="cuda", dtype=dt)
            with torch.no_grad():
                model.inference(a, a, ts, scale=1.0)
            torch.cuda.synchronize()
            logger.info("RIFE prime 完成: %s scale=%s %dx%d (padded %dx%d)",
                        model_version, scale, width, height, dw + pw, dh + ph)
