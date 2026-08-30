"""Lossless Scaling（小黄鸭）外部程序控制器。

职责：管理 LosslessScaling.exe 进程的懒启动 + 通过全局快捷键开/关缩放。
不依赖 Qt，全部 OS 调用（subprocess / ctypes）都可被 mock，便于单元测试。

行为约定（已与用户确认）：
- 进程懒启动：仅在选中「小黄鸭」后端时 launch()，启动后常驻；切走只 stop_scaling()
  （发送快捷键关闭缩放），不杀进程。
- 进入全屏发送快捷键开启缩放，退出全屏再次发送关闭（小黄鸭需要全屏画面才能缩放）。
- VentiPlayer 退出时 terminate()：先关缩放，再 taskkill /F 杀掉所有 LosslessScaling.exe
  实例（用户接受这可能误杀其自行开的实例）。

快捷键须与 Lossless Scaling 内设置的缩放快捷键一致，默认 ctrl+alt+s。
"""

import subprocess
import sys
import time
from pathlib import Path

# 按键名 -> Windows 虚拟键码 (VK)。修饰键 + 常用单键。
_VK_MAP = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B, "super": 0x5B, "meta": 0x5B,
    "space": 0x20,
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09,
}
# 修饰键集合（用于排序：修饰键在前，主键在后）
_MODIFIERS = {0x11, 0x12, 0x10, 0x5B}

KEYEVENTF_KEYUP = 2
STARTF_USESHOWWINDOW = 0x00000001
SW_SHOWMINIMIZED = 2


class LosslessScalingController:
    """小黄鸭进程 + 快捷键控制器。"""

    def __init__(self, exe_path: str = "", hotkey: str = "ctrl+alt+s"):
        self._exe_path = exe_path or ""
        self._hotkey = hotkey or "ctrl+alt+s"
        self._proc = None
        self._scaling = False
        self._launched_by_us = False

    # ---- 配置 ----

    def update_config(self, exe_path: str, hotkey: str):
        """设置变更时更新路径与快捷键。"""
        self._exe_path = exe_path or ""
        self._hotkey = hotkey or "ctrl+alt+s"

    def is_configured(self) -> bool:
        return bool(self._exe_path)

    def exe_exists(self) -> bool:
        try:
            return Path(self._exe_path).is_file()
        except Exception:
            return False

    @property
    def is_scaling(self) -> bool:
        return self._scaling

    # ---- 进程 ----

    def launch(self) -> bool:
        """懒启动 LosslessScaling.exe（最小化窗口）。已在运行则直接返回 True。"""
        if not self.exe_exists():
            print("[小黄鸭] 可执行文件不存在，无法启动")
            return False
        # 我们启动的进程仍存活（poll() 为 None）则无需重启
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    return True
            except Exception:
                pass
        try:
            startupinfo = None
            creationflags = 0
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = SW_SHOWMINIMIZED
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            self._proc = subprocess.Popen(
                [self._exe_path],
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
            self._launched_by_us = True
            print(f"[小黄鸭] 已启动: {self._exe_path}")
            return True
        except Exception as e:
            print(f"[小黄鸭] 启动失败: {e}")
            return False

    def minimize_window(self) -> bool:
        """尽力最小化 LosslessScaling.exe 的窗口（按进程名匹配所有实例）。

        仅 Windows 有效，且**仅当 LS 未以管理员身份运行**时才成功：若 LS 以管理员
        权限运行（默认会弹 UAC 确认框），Windows 的 UIPI（界面权限隔离）会阻止非提权
        的本进程操作其窗口，此时静默失败返回 False。根治办法：在 LosslessScaling.exe
        属性→兼容性里关掉「以管理员身份运行此程序」。返回是否至少最小化了一个窗口。
        """
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            SW_MINIMIZE = 6
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            minimized = []

            def _proc_name(pid: int) -> str:
                h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if not h:
                    return ""  # 提权进程对非提权本进程拒绝访问 → UIPI，放弃
                try:
                    buf = ctypes.create_unicode_buffer(260)
                    size = wintypes.DWORD(260)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        return buf.value
                    return ""
                finally:
                    kernel32.CloseHandle(h)

            WNDENUMPROC = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def _cb(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                name = _proc_name(pid.value)
                if name and name.lower().endswith("losslessscaling.exe"):
                    user32.ShowWindow(hwnd, SW_MINIMIZE)
                    minimized.append(hwnd)
                return True

            user32.EnumWindows(WNDENUMPROC(_cb), 0)
            if minimized:
                print(f"[小黄鸭] 已最小化 {len(minimized)} 个 LS 窗口")
            return bool(minimized)
        except Exception as e:
            print(f"[小黄鸭] 最小化窗口失败(可忽略): {e}")
            return False

    # ---- 快捷键 ----

    def _parse_hotkey(self, hotkey: str) -> list[int]:
        """把 'ctrl+alt+s' 解析为有序 VK 列表（修饰键在前，主键在后）。未知 token 跳过。"""
        mods: list[int] = []
        keys: list[int] = []
        for raw in (hotkey or "").split("+"):
            token = raw.strip().lower()
            if not token:
                continue
            vk = None
            if token in _VK_MAP:
                vk = _VK_MAP[token]
            elif len(token) == 1 and token.isalpha():
                vk = ord(token.upper())
            elif len(token) == 1 and token.isdigit():
                vk = ord(token)
            elif len(token) >= 2 and token[0] == "f" and token[1:].isdigit():
                n = int(token[1:])
                if 1 <= n <= 12:
                    vk = 0x70 + (n - 1)
            if vk is None:
                print(f"[小黄鸭] 未知快捷键 token，已跳过: {raw}")
                continue
            (mods if vk in _MODIFIERS else keys).append(vk)
        return mods + keys

    def send_toggle_hotkey(self) -> bool:
        """模拟按下并释放快捷键（开/关缩放的同一个 toggle 键）。仅 Windows 有效。"""
        if sys.platform != "win32":
            print("[小黄鸭] 非 Windows 平台，无法发送快捷键")
            return False
        vks = self._parse_hotkey(self._hotkey)
        if not vks:
            print("[小黄鸭] 快捷键解析为空，放弃发送")
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # 顺序按下
            for vk in vks:
                user32.keybd_event(vk, 0, 0, 0)
                time.sleep(0.03)
            # 逆序释放
            for vk in reversed(vks):
                user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                time.sleep(0.03)
            return True
        except Exception as e:
            print(f"[小黄鸭] 发送快捷键失败: {e}")
            return False

    # ---- 缩放开/关 ----

    def start_scaling(self):
        """进入全屏调用：发送快捷键开启缩放。已在缩放则不重复发送。"""
        if self._scaling:
            return
        if self.send_toggle_hotkey():
            self._scaling = True
            print("[小黄鸭] 已开启缩放")

    def stop_scaling(self):
        """退出全屏调用：发送快捷键关闭缩放。无论发送是否成功都复位状态，避免卡死。"""
        if not self._scaling:
            return
        self.send_toggle_hotkey()
        self._scaling = False
        print("[小黄鸭] 已关闭缩放")

    # ---- 退出清理 ----

    def terminate(self):
        """VentiPlayer 退出时调用：先关缩放，再杀掉所有 LosslessScaling.exe 实例。"""
        if self._scaling:
            self.stop_scaling()
        # 先尝试结束我们自己启动的进程
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
            except Exception:
                pass
        # 再 taskkill 兜底所有实例（用户已确认接受可能误杀）
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/IM", "LosslessScaling.exe", "/F"],
                    capture_output=True,
                )
            except Exception as e:
                print(f"[小黄鸭] taskkill 失败(可忽略): {e}")
        self._proc = None
        self._scaling = False
        self._launched_by_us = False
