"""RIFE 真插帧（torch ROCm）推理包：模型工厂 + 权重路径。

结构（改编自 Practical-RIFE，inference-only，见各文件头注释）:
    warplayer.py            backwarp（含 fp16 dtype 缓存补丁）
    v4_25_lite/             官方 v4.25 lite 权重结构（32x 金字塔，pad 128）
    v4_25/                  官方 v4.25 权重结构（16x 金字塔）
    v4_26/                  官方 v4.26 权重结构（16x 金字塔）

权重文件: ~/.ventiplayer/models/rife/<version>/train_log/flownet.pkl
    由 download_models.py 下载（每份 ~24 MB）。

模型实例按 (version, fp16) 进程级缓存 —— vpy 在 mpv 的 VSScript 内与宿主共享
同一个 Python interpreter（Phase 1 探针实测），宿主后台线程预热后 vpy 直接
复用本模块缓存，零重复加载。
"""
import importlib
from pathlib import Path

MODELS_DIR = Path.home() / ".ventiplayer" / "models" / "rife"
# 界面可选的模型版本（顺序即面板下拉顺序）
VERSIONS = ["v4_25_lite", "v4_25", "v4_26"]

_cache: dict = {}


def weights_dir(version: str) -> Path:
    """返回指定版本的官方 train_log 权重目录（含 flownet.pkl）。"""
    return MODELS_DIR / version / "train_log"


def weights_exist(version: str) -> bool:
    return (weights_dir(version) / "flownet.pkl").is_file()


def get_model(version: str, fp16: bool = False):
    """加载（或返回缓存的）RIFE Model 实例。

    首次加载: torch.load flownet.pkl → 上卡 → fp16 时整网 half()。
    必须在后台线程调用（torch.load + .cuda() 需数秒）。
    """
    key = (version, bool(fp16))
    if key in _cache:
        return _cache[key]

    wdir = weights_dir(version)
    pkl = wdir / "flownet.pkl"
    if not pkl.is_file():
        raise FileNotFoundError(f"RIFE 权重缺失: {pkl}（请运行 download_models.py）")

    mod = importlib.import_module(f"src.models.rife.{version}.RIFE_HDv3")
    model = mod.Model()
    model.load_model(str(wdir), -1)
    model.eval()
    if fp16:
        model.flownet.half()
    _cache[key] = model
    return model
