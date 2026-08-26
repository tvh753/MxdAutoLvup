# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:06
# @Author  : yy
# @File    : window_capture.py
# @Software: MxdAutoLvup

"""PrintWindow + PW_RENDERFULLCONTENT，可后台抓取 DirectX 渲染的游戏画面"""
import ctypes
import numpy as np

try:
    import win32gui, win32ui
except ImportError as e:
    raise ImportError("请先安装: pip install pywin32") from e

PW_CLIENTONLY = 0x01
PW_RENDERFULLCONTENT = 0x02


class WindowCapture:
    def __init__(self):
        self.hwnd = None
        self.window_title = ""
        self._w = self._h = 0

    @staticmethod
    def list_windows():
        result = []
        def _cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd)
                if t:
                    result.append((t, hwnd))
            return True
        win32gui.EnumWindows(_cb, None)
        return result

    def bind(self, keyword: str) -> bool:
        kw = keyword.strip().lower()
        for title, hwnd in self.list_windows():
            if kw in title.lower():
                self.hwnd, self.window_title = hwnd, title
                self._refresh_size()
                return True
        return False

    def _refresh_size(self):
        l, t, r, b = win32gui.GetClientRect(self.hwnd)
        self._w, self._h = r - l, b - t

    @property
    def size(self):
        return self._w, self._h

    def is_foreground(self) -> bool:
        try:
            return win32gui.GetForegroundWindow() == self.hwnd
        except Exception:
            return True

    def bring_foreground(self) -> bool:
        try:
            win32gui.ShowWindow(self.hwnd, 9)      # SW_RESTORE
            win32gui.SetForegroundWindow(self.hwnd)
            return True
        except Exception:
            return False

    def screenshot(self):
        """返回 BGR ndarray，失败返回 None"""
        if not self.hwnd:
            return None
        self._refresh_size()
        w, h = self._w, self._h
        if w <= 0 or h <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(self.hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)

        ok = ctypes.windll.user32.PrintWindow(
            self.hwnd, save_dc.GetSafeHdc(), PW_CLIENTONLY | PW_RENDERFULLCONTENT)

        img = None
        if ok:
            buf = bmp.GetBitmapBits(True)
            img = np.ascontiguousarray(
                np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))[:, :, :3])

        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC(); mfc_dc.DeleteDC()
        win32gui.ReleaseDC(self.hwnd, hwnd_dc)
        return img