"""最小复现脚本：不走 GUI，直接加载 Apollo/FlashSR 并预热，复现 LLVM abort。

用法：.venv312/Scripts/python.exe tools/repro_warmup.py [apollo|flashsr|both] [重复次数]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.export.common import ensure_libmpv_on_path
ensure_libmpv_on_path()

import os
import tempfile

# 与 src/main.py 相同的 MIOpen 缓存设置
_ascii_base = str(Path(__file__).resolve().parent.parent)
_miopen_cache = str(Path(_ascii_base) / ".miopen_cache")
os.environ["MIOPEN_USER_DB_PATH"] = _miopen_cache
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = _miopen_cache
os.makedirs(_miopen_cache, exist_ok=True)
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
os.environ.setdefault("MIOPEN_LOG_LEVEL", "2")

from src.core.enhancer import Backend, DeviceInfo

which = sys.argv[1] if len(sys.argv) > 1 else "both"
repeat = int(sys.argv[2]) if len(sys.argv) > 2 else 1

info = DeviceInfo(Backend.ROCM, "AMD Radeon RX 9070", 16304)

for it in range(repeat):
    print(f"=== iteration {it + 1}/{repeat} ===", flush=True)
    if which in ("apollo", "both"):
        t0 = time.monotonic()
        from src.models.apollo_model import ApolloModel
        m = ApolloModel(info, use_fp16=True)
        print(f"[apollo] load+to(device) start...", flush=True)
        ok = m.load()
        print(f"[apollo] load={ok} in {time.monotonic() - t0:.1f}s", flush=True)
        if ok:
            import numpy as np
            t1 = time.monotonic()
            out, sr = m.enhance(np.zeros((2, 44100 * 2), dtype=np.float32), 44100)
            print(f"[apollo] enhance 2s ok in {time.monotonic() - t1:.1f}s, out_sr={sr}", flush=True)
            m.unload()
    if which in ("flashsr", "both"):
        t0 = time.monotonic()
        from src.models.flashsr_model import FlashSRModel
        m = FlashSRModel(info, use_fp16=True)
        print(f"[flashsr] load start...", flush=True)
        ok = m.load()
        print(f"[flashsr] load={ok} in {time.monotonic() - t0:.1f}s", flush=True)
        if ok:
            import numpy as np
            t1 = time.monotonic()
            out, sr = m.enhance(np.zeros((2, 48000 * 2), dtype=np.float32), 48000)
            print(f"[flashsr] enhance 2s ok in {time.monotonic() - t1:.1f}s, out_sr={sr}", flush=True)
            m.unload()
print("ALL DONE", flush=True)
