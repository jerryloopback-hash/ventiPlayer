"""临时：测试 vf=vapoursynth 路径引号/转义形式。"""
import os
import sys
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication([])
sys.path.insert(0, ".")
from src.core.export.common import ensure_libmpv_on_path
ensure_libmpv_on_path()
import mpv

p = mpv.MPV(vo="null", idle="yes", terminal=False)
vpy = str(Path("tools/rife_spike/probe_trivial.vpy").resolve())
escaped = vpy.replace(":", "\\:")
tests = [
    ("quoted", f"vapoursynth=file='{vpy}'"),
    ("escaped", f"vapoursynth=file={escaped}"),
]
for tag, val in tests:
    try:
        p["vf"] = val
        print(f"OK  [{tag}]:", val[:70])
        p["vf"] = ""
    except Exception as e:
        print(f"FAIL[{tag}]:", val[:70], "->", e)
p.terminate()
