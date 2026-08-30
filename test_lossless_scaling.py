"""LosslessScalingController 状态机单元测试（mock subprocess + ctypes，无需真实 LS）。

运行：.venv312/Scripts/python.exe test_lossless_scaling.py
"""

import sys
import types
import unittest
from unittest import mock

# 让 import src.core.lossless_scaling 可用
sys.path.insert(0, ".")

from src.core.lossless_scaling import LosslessScalingController


class TestLosslessScalingController(unittest.TestCase):

    def _make(self, exe="C:/x/LosslessScaling.exe", hotkey="ctrl+alt+s"):
        return LosslessScalingController(exe, hotkey)

    def test_parse_hotkey_ctrl_alt_s(self):
        c = self._make()
        self.assertEqual(c._parse_hotkey("ctrl+alt+s"), [0x11, 0x12, 0x53])

    def test_parse_hotkey_function_and_digit(self):
        c = self._make()
        # f5 -> 0x74; 修饰键在前
        self.assertEqual(c._parse_hotkey("shift+f5"), [0x10, 0x74])
        self.assertEqual(c._parse_hotkey("ctrl+1"), [0x11, ord("1")])

    def test_launch_returns_true_when_exe_exists(self):
        c = self._make()
        with mock.patch.object(c, "exe_exists", return_value=True), \
             mock.patch("src.core.lossless_scaling.subprocess.Popen") as popen:
            popen.return_value = mock.MagicMock()
            self.assertTrue(c.launch())
            popen.assert_called_once()

    def test_launch_returns_false_when_exe_missing(self):
        c = self._make()
        with mock.patch.object(c, "exe_exists", return_value=False):
            self.assertFalse(c.launch())

    def test_start_scaling_sends_once_and_sets_flag(self):
        c = self._make()
        with mock.patch.object(c, "send_toggle_hotkey", return_value=True) as send:
            c.start_scaling()
            self.assertTrue(c.is_scaling)
            send.assert_called_once()
            # 二次 start 不应重复发送
            c.start_scaling()
            send.assert_called_once()

    def test_stop_scaling_sends_and_clears(self):
        c = self._make()
        with mock.patch.object(c, "send_toggle_hotkey", return_value=True) as send:
            c.start_scaling()
            send.reset_mock()
            c.stop_scaling()
            self.assertFalse(c.is_scaling)
            send.assert_called_once()
            # 再次 stop 不应发送
            c.stop_scaling()
            send.assert_called_once()

    def test_terminate_stops_scaling_and_taskkill(self):
        c = self._make()
        with mock.patch.object(c, "send_toggle_hotkey", return_value=True):
            c.start_scaling()
        self.assertTrue(c.is_scaling)
        with mock.patch("src.core.lossless_scaling.sys.platform", "win32"), \
             mock.patch("src.core.lossless_scaling.subprocess.run") as run, \
             mock.patch.object(c, "send_toggle_hotkey", return_value=True):
            c.terminate()
            self.assertFalse(c.is_scaling)
            run.assert_called_once()
            args = run.call_args[0][0]
            self.assertIn("taskkill", args)
            self.assertIn("LosslessScaling.exe", args)

    def test_send_toggle_hotkey_presses_and_releases(self):
        c = self._make()
        fake_user32 = mock.MagicMock()
        fake_windll = types.SimpleNamespace(user32=fake_user32)
        fake_ctypes = types.SimpleNamespace(windll=fake_windll)
        with mock.patch("src.core.lossless_scaling.sys.platform", "win32"), \
             mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
             mock.patch("src.core.lossless_scaling.time.sleep", return_value=None):
            ok = c.send_toggle_hotkey()
            self.assertTrue(ok)
            # 3 个键 -> 3 次按下 + 3 次释放 = 6 次 keybd_event
            self.assertEqual(fake_user32.keybd_event.call_count, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
