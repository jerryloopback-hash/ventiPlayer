"""Phase 0.3a 最小链路验证：libmpv + vf_vapoursynth（直通 vpy，无模型）。

只验证: mpv 进程内加载 vsscript/libvapoursynth 并执行 vpy 不崩、
estimated-vf-fps 正常。不涉及任何 ML 模型。
"""
import os
import sys
import time
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
VENV = SPIKE.parent.parent / ".venv312"
VS_DIR = VENV / "Lib" / "site-packages" / "vapoursynth"

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication([])
sys.path.insert(0, str(SPIKE.parent.parent))
from src.core.export.common import ensure_libmpv_on_path  # noqa: E402
ensure_libmpv_on_path()
print("[probe] PySide6 + libmpv 路径 OK")

os.environ["PATH"] = str(VS_DIR) + os.pathsep + os.environ["PATH"]

import mpv  # noqa: E402

vpy = SPIKE / "probe_trivial.vpy"
vpy.write_text(
    'clip = video_in\n'
    'clip = clip.std.Trim(first=0)\n'  # 纯直通（经 std 一次，验证 VS 管线）
    'clip.set_output()\n', encoding="utf-8")

errors, logs = [], []
player = mpv.MPV(vo="null", hwdec="no", idle="yes",
                 keep_open="yes", log_handler=lambda l, c, m:
                 (errors.append((l, c, m)) if l in ("error", "fatal")
                  else logs.append((l, c, m))),
                 loglevel="debug")
player.terminal = False

t0 = time.perf_counter()
# vf 列表解析器下反斜杠/盘符冒号会破坏子选项解析 —— 必须用正斜杠绝对路径
vpy_fwd = vpy.as_posix()
player["vf"] = f"vapoursynth=file={vpy_fwd}"
player["frames"] = 60
player.play(str(SPIKE / "test_360p24.mp4"))
deadline = time.time() + 45
vf_fps = None
while time.time() < deadline:
    try:
        pt = player.playback_time
        if pt is not None and pt > 1.5:
            try:
                vf_fps = player.estimated_vf_fps
            except Exception:
                pass
            break
    except Exception:
        pass
    time.sleep(0.1)
print(f"[probe] 耗时 {time.perf_counter()-t0:.1f}s, estimated-vf-fps={vf_fps}")
vs_logs = [l for l in logs + errors if "vapoursynth" in str(l).lower()]
print(f"[probe] VS 相关日志 {len(vs_logs)} 条:")
for l in vs_logs[:10]:
    print("   ", l)
print(f"[probe] 错误 {len(errors)} 条:")
for e in errors[:8]:
    print("   ", e)
player.terminate()
ok = not errors and vf_fps is not None and vf_fps > 20  # keep_open 下 EOF 判定改用 playback_time
print("PROBE_RESULT|", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
