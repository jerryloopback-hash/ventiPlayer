"""应用入口：日志免疫层、异常钩子、运行环境变量（MIOpen/ROCm/HF 镜像）、
splash + MainWindow 装配。"""
import sys
import os
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def _install_robust_logging():
    """给宿主日志装一个基础的根 handler，并套上对“坏 handler”的免疫层。

    历史背景：早期内置 SVP/RIFE 后端时，mpv 内嵌的 VapourSynth VSScript 会往根
    logger 挂一个有 bug 的 handler，任何 logging 调用都会被反噬崩溃。真插帧后端
    已移除，但这层免疫保留 —— 任何第三方（mpv 事件循环、模型库）往根 logger 挂的
    坏 handler 都不应让宿主 logging 调用崩溃。
    """
    logging.raiseExceptions = False  # 静默 handler 内部错误，不再往 stderr 喷栈

    root = logging.getLogger()
    if not any(getattr(h, "_venti_base", False) for h in root.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
        h._venti_base = True
        root.addHandler(h)
    root.setLevel(logging.INFO)

    if not getattr(logging.Logger, "_venti_safe_callhandlers", False):
        _orig_call_handlers = logging.Logger.callHandlers

        def _safe_call_handlers(self, record):
            try:
                _orig_call_handlers(self, record)
            except Exception:
                # 坏 handler（如 VS 日志桥）不得让 logging 调用崩溃
                pass

        logging.Logger.callHandlers = _safe_call_handlers
        logging.Logger._venti_safe_callhandlers = True


_install_robust_logging()


def _setup_exception_hooks():
    """Install global exception hooks so background thread crashes are logged."""
    def _sys_excepthook(exc_type, exc_value, exc_tb):
        if exc_type is KeyboardInterrupt:
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    def _threading_excepthook(args):
        if args.exc_type is SystemExit:
            return
        logger.critical(
            f"Unhandled exception in thread '{args.thread.name if args.thread else '?'}'",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_excepthook
    threading.excepthook = _threading_excepthook


_setup_exception_hooks()

# libmpv-2.dll lives in project root
_project_root = str(Path(__file__).resolve().parent.parent)
os.environ["PATH"] = _project_root + os.pathsep + os.environ.get("PATH", "")


os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

# ROCm MIOpen 缓存路径：MIOpen 的 SQLite 缓存无法打开含非 ASCII 字符的路径
# （本机用户名为中文，C:\Users\洛\... 会让 conv 等算子报 miopenStatusInternalError）。
# tempfile.gettempdir() 通常落在 C:\Users\<用户名>\AppData\Local\Temp，仍含中文，
# 因此优先用 ASCII 的项目根目录；万一项目根也非 ASCII，再退回系统临时目录。
import tempfile as _tempfile
_ascii_base = _project_root if _project_root.isascii() else _tempfile.gettempdir()
_miopen_cache = str(Path(_ascii_base) / ".miopen_cache")
# 用显式赋值而非 setdefault：start.bat 可能已经设过一个非 ASCII 的旧值，
# 这里必须覆盖它，否则缓存路径仍然指向中文目录导致 GPU 算子失败。
os.environ["MIOPEN_USER_DB_PATH"] = _miopen_cache
os.environ["MIOPEN_CUSTOM_CACHE_DIR"] = _miopen_cache
os.makedirs(_miopen_cache, exist_ok=True)

# MIOpen conv tuning: FAST find mode skips the exhaustive solver search that
# floods the log with "GemmFwdRest workspace ... ptr 0x0" warnings and makes the
# first inference extremely slow; LOG_LEVEL=2 hides those harmless warnings.
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
os.environ.setdefault("MIOPEN_LOG_LEVEL", "2")

# 禁用 hipBLASLt 的 addmm/matmul 路径（回落经典 BLAS）。
# 根因实锤（log/ventiplayer.log + tools/repro_warmup.py 复现）：RX 9070
# (gfx1201) + Windows ROCm 上，模型上卡的首个 hipBLASLt matmul 会触发
# "LLVM ERROR: Can't get available size" 原生 abort（Python 拦不住，进程
# 直接死 → 增强加载阶段偶发闪退）。禁用后加载+推理稳定（fp16 亦然）。
os.environ["DISABLE_ADDMM_CUDA_LT"] = "1"

# PyTorch memory management for 16GB VRAM
os.environ.setdefault("PYTORCH_ALLOC_CONF", "expandable_segments:True")
os.environ.setdefault("PYTORCH_HIP_ALLOC_CONF", "garbage_collection_threshold:0.6,max_split_size_mb:128")

# HuggingFace mirror for China mainland
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Suppress timm deprecation warning
import warnings
warnings.filterwarnings("ignore", message="Importing from.*is deprecated.*timm.layers",
                        category=FutureWarning)

from PySide6.QtWidgets import QApplication, QSplashScreen
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QFont, QColor, QPainter

from src.gui.main_window import MainWindow


def _create_splash() -> QSplashScreen:
    pixmap = QPixmap(420, 200)
    pixmap.fill(QColor(30, 30, 30))
    painter = QPainter(pixmap)
    painter.setPen(QColor(220, 220, 220))
    font = QFont("Segoe UI", 22, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "VentiPlayer")
    font2 = QFont("Segoe UI", 10)
    painter.setFont(font2)
    painter.setPen(QColor(160, 160, 160))
    painter.drawText(
        pixmap.rect().adjusted(0, 60, 0, 0),
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
        "正在检测推理后端...",
    )
    painter.end()
    splash = QSplashScreen(pixmap)
    splash.setWindowFlags(
        Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
    )
    return splash


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("VentiPlayer")
    app.setOrganizationName("VentiPlayer")

    splash = _create_splash()
    splash.show()
    app.processEvents()

    # Create main window hidden — it will do backend detection in background thread
    window = MainWindow()
    # Don't show yet — wait for backend detection to complete

    def _on_backend_ready():
        splash.close()
        window.showMaximized()

    window.backend_ready.connect(_on_backend_ready)

    # Safety timeout: if detection hangs for 30s, show window anyway
    def _timeout():
        if not window.isVisible():
            splash.close()
            window.showMaximized()

    timeout_timer = QTimer()
    timeout_timer.setSingleShot(True)
    timeout_timer.timeout.connect(_timeout)
    timeout_timer.start(30000)
    app._timeout_timer = timeout_timer
    app._main_window = window

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
