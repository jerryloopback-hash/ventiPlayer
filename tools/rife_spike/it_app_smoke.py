"""Phase 1 app 级冒烟：offscreen 构造 MainWindow，验证 UI 接线与 RIFE 集成。"""
import os, sys
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["MIOPEN_USER_DB_PATH"] = str(REPO / ".miopen_cache")
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = str(REPO / ".miopen_cache")

# 模拟 src/main.py 的关键环境
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"
from src.main import _install_robust_logging  # noqa: E402
_install_robust_logging()

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from src.gui.main_window import MainWindow

app = QApplication([])
win = MainWindow()

# 校验 1: 面板 caps 含 rife_torch 且可选
combo = win._video_enhance_panel._fg_backend
rife_item = combo.model().item(2)
print(f"[smoke] 后端下拉第3项: {combo.itemText(2)} enabled={rife_item.isEnabled()}")
assert rife_item.isEnabled(), "RIFE 项应可用（权重已在位）"

# 校验 2: service 就位
assert win._rife_service is not None
assert hasattr(win, "_on_rife_prime_done")

# 校验 3: 面板 rife 参数页默认值
panel = win._video_enhance_panel
print(f"[smoke] rife 模型默认={panel._rife_model.currentData()} "
      f"档位默认={panel._rife_scale.currentData()}")
assert panel._rife_model.currentData() == "v4_25_lite"
assert panel._rife_scale.currentData() == 0.75

# 初始化真实 mpv（offscreen winId 可用；normally QTimer.singleShot(0) 触发）
win._player_widget.init_mpv(audio_exclusive=False)
print("[smoke] mpv initialized:", win._player_widget._player is not None)

# 校验 4: 信号触发链 —— 模拟用户选 RIFE 后端（未勾选总开关不发应用，只验证 emit 参数）
captured = {}
panel.frame_gen_changed.connect(lambda en, p: captured.update(en=en, p=p))
panel._fg_backend.setCurrentIndex(2)
panel._enable_fg.setChecked(True)
print(f"[smoke] frame_gen_changed: enabled={captured.get('en')} params={captured.get('p')}")
assert captured.get("en") is True
assert captured.get("p", {}).get("backend") == "rife-torch"
assert captured.get("p", {}).get("model") == "v4_25_lite"
assert captured.get("p", {}).get("scale") == 0.75

# _on_frame_gen_changed 已被触发（无视频时只记录意图，不 prime）
st = win._framegen_state
print(f"[smoke] framegen_state: {st}")
assert st.get("backend") == "rife-torch"
assert win._rife_params.get("backend") == "rife-torch"

# 校验 5: PATH 注入后 VS_DIR 可被找到（main.py 在真实启动时注入；这里直接查 venv）
import importlib.util
assert importlib.util.find_spec("vapoursynth") is not None
print("[smoke] vapoursynth find_spec OK")

win._player_widget.destroy()
win.close()
print("APP_SMOKE_PASS")
os._exit(0)
