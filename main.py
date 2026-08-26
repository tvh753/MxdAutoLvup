# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:05
# @Author  : yy
# @File    : main.py
# @Software: MxdAutoLvup

"""枫叶挂机控制台 · 入口"""
import sys
import ctypes


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


if __name__ == "__main__":
    if sys.platform != "win32":
        print("本工具依赖 win32 API 与 DirectInput，仅支持 Windows。")
        sys.exit(1)

    enable_dpi_awareness()
    from gui.main_window import App
    App().mainloop()