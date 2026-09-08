# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:06
# @Author  : yy
# @File    : window_capture.py
# @Software: MxdAutoLvup

"""PrintWindow + PW_RENDERFULLCONTENT，可后台抓取 DirectX 渲染的游戏画面

为什么不用 pyautogui/PIL 全屏截图：
  1. 全屏截图要求游戏在前台，鼠标一挪过去游戏就失焦暂停；
  2. 冒险岛怀旧服使用 DirectX 渲染，普通 GDI 位图抓不到画面。
PrintWindow 加 PW_RENDERFULLCONTENT 标志可以请求窗口渲染全部内容，
对 DirectX 游戏有较好兼容性，且不要求窗口处于前台。

输出格式：BGR ndarray（与 OpenCV 直接兼容）。
"""
import ctypes
import numpy as np

try:
    import win32gui, win32ui
except ImportError as e:
    raise ImportError("请先安装: pip install pywin32") from e

PW_CLIENTONLY = 0x01          # 只抓客户区（不含标题栏边框）
PW_RENDERFULLCONTENT = 0x02   # 让 DirectX 等渲染全部内容（关键！）


class WindowCapture:
    """窗口句柄管理 + PrintWindow 后台截图"""

    def __init__(self):
        self.hwnd = None          # 绑定的窗口句柄
        self.window_title = ""    # 绑定时记录的真实窗口标题
        self._w = self._h = 0     # 客户区尺寸缓存

    @staticmethod
    def list_windows():
        """枚举当前所有可见窗口，返回 [(标题, hwnd), ...]（供 GUI 下拉选择）"""
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
        """按标题关键字模糊绑定窗口；成功返回 True"""
        kw = keyword.strip().lower()
        for title, hwnd in self.list_windows():
            if kw in title.lower():
                self.hwnd, self.window_title = hwnd, title
                self._refresh_size()
                return True
        return False

    def _refresh_size(self):
        """刷新客户区尺寸缓存（窗口被拖动/缩放后坐标会变）"""
        l, t, r, b = win32gui.GetClientRect(self.hwnd)
        self._w, self._h = r - l, b - t

    @property
    def size(self):
        """客户区宽高 (w, h)"""
        return self._w, self._h

    def is_foreground(self) -> bool:
        """游戏窗口是否为当前前台窗口（失焦保护判断用）"""
        try:
            return win32gui.GetForegroundWindow() == self.hwnd
        except Exception:
            return True  # 查询失败时按“在焦点”处理，避免误暂停

    def bring_foreground(self) -> bool:
        """把游戏窗口带到前台（启动挂机时调用，保证按键送达）"""
        try:
            win32gui.ShowWindow(self.hwnd, 9)      # SW_RESTORE：从最小化恢复
            win32gui.SetForegroundWindow(self.hwnd)
            return True
        except Exception:
            return False

    def screenshot(self):
        """后台抓取游戏画面，返回 BGR ndarray，失败返回 None

        核心步骤：
          1. 取得窗口 DC → 创建兼容内存 DC 和位图；
          2. PrintWindow 把窗口内容画到内存位图（含 DirectX 内容）；
          3. 从位图读回原始像素 → 去掉 Alpha 通道 → BGR 数组。
        """
        if not self.hwnd:
            return None
        self._refresh_size()
        w, h = self._w, self._h
        if w <= 0 or h <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(self.hwnd)          # 窗口设备上下文
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)       # 转 MFC DC 包装
        save_dc = mfc_dc.CreateCompatibleDC()              # 兼容内存 DC
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)           # 与窗口同尺寸位图
        save_dc.SelectObject(bmp)

        # 关键调用：PW_CLIENTONLY|PW_RENDERFULLCONTENT 才能抓到 DirectX 内容
        ok = ctypes.windll.user32.PrintWindow(
            self.hwnd, save_dc.GetSafeHdc(), PW_CLIENTONLY | PW_RENDERFULLCONTENT)

        img = None
        if ok:
            buf = bmp.GetBitmapBits(True)                  # 读回 BGRA 像素
            img = np.ascontiguousarray(
                np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))[:, :, :3])

        # 释放 GDI 资源（泄漏会导致截图逐渐变慢/黑屏）
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC(); mfc_dc.DeleteDC()
        win32gui.ReleaseDC(self.hwnd, hwnd_dc)
        return img