# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:05
# @Author  : yy
# @File    : main.py
# @Software: MxdAutoLvup

"""枫叶挂机控制台 · 程序入口

启动流程：
  1. 校验运行平台（仅支持 Windows，依赖 win32 API 与 DirectInput）；
  2. 开启高 DPI 感知，避免高分屏下界面/截图坐标错位；
  3. 导入主窗口 App 并进入 tkinter 消息循环（所有逻辑由后台线程驱动）。
"""
import sys
import ctypes


def enable_dpi_awareness():
    """启用 Windows 高 DPI 感知。

    - SetProcessDpiAwareness(2) 为「Per-Monitor DPI」：每个显示器单独缩放，
      保证窗口截图尺寸与实际显示尺寸一致（否则识别坐标会整体偏移）；
    - 旧系统不支持时回退到 SetProcessDPIAware()（系统级 DPI 缩放）；
    - 两者都失败则静默忽略（仅影响高分屏下的显示精度，不影响功能）。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


if __name__ == "__main__":
    # 项目强依赖 pywin32 / pydirectinput 等 Windows 专有 API，非 Windows 直接退出
    if sys.platform != "win32":
        print("本工具依赖 win32 API 与 DirectInput，仅支持 Windows。")
        sys.exit(1)

    enable_dpi_awareness()
    # 延迟导入：先完成 DPI 设置再初始化 tkinter 窗口
    from gui.main_window import App
    App().mainloop()