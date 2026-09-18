# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:06
# @Author  : yy
# @File    : window_capture.py
# @Software: MxdAutoLvup

"""截图方案（三种，接口一致，可互换）：

  ① WindowCapture      —— PrintWindow + PW_RENDERFULLCONTENT 后台抓 DirectX
                          同步阻塞，每次 100~200ms，FPS 上限 3~5
  ② FastWindowCapture  —— windows_capture 库异步截图
                          后台线程持续抓，主循环非阻塞读缓冲
                          单帧耗时 ~5ms，FPS 10~20（推荐）
  ③ VncCapture         —— vncdotool 连接 VNC 虚拟机
                          适合本机脚本 + 虚拟机游戏，网络往返 50~150ms

对外接口一致：bind / screenshot / is_foreground / bring_foreground / size
"""
import ctypes
import os
import threading
import time

import cv2
import numpy as np

try:
    import win32gui, win32ui
except ImportError:
    win32gui = win32ui = None

try:
    from vncdotool import api as vnc_api
except ImportError:
    vnc_api = None

try:
    from windows_capture import WindowsCapture, Frame, InternalCaptureControl
    HAS_WINCAP = True
except ImportError:
    HAS_WINCAP = False

try:
    import pydirectinput
    pydirectinput.PAUSE = 0.02
    pydirectinput.FAILSAFE = False
except ImportError:
    pydirectinput = None


PW_CLIENTONLY = 0x01
PW_RENDERFULLCONTENT = 0x02


# ================= 全局键盘抽象 =================
# action_controller.py 通过这两个函数下发按键，由 set_dispatch_mode 决定走哪条路

_dispatch_mode = "window"
_dispatch_vnc = None


def set_dispatch_mode(mode, vnc_controller=None):
    """切换按键下发方式

    mode: "window" → pydirectinput；"vnc" → VncCapture
    vnc_controller: VncCapture 实例（mode="vnc" 时必传）
    """
    global _dispatch_mode, _dispatch_vnc
    _dispatch_mode = mode
    _dispatch_vnc = vnc_controller


def press_key_down(key):
    if not key:
        return
    if _dispatch_mode == "vnc" and _dispatch_vnc is not None:
        try:
            _dispatch_vnc.key_down(key)
        except Exception:
            pass
    elif pydirectinput is not None:
        try:
            pydirectinput.keyDown(key)
        except Exception:
            pass


def press_key_up(key):
    if not key:
        return
    if _dispatch_mode == "vnc" and _dispatch_vnc is not None:
        try:
            _dispatch_vnc.key_up(key)
        except Exception:
            pass
    elif pydirectinput is not None:
        try:
            pydirectinput.keyUp(key)
        except Exception:
            pass


# ================= ① 原版：PrintWindow =================
class WindowCapture:
    """窗口句柄管理 + PrintWindow 后台截图（同步，慢）"""

    def __init__(self):
        self.hwnd = None
        self.window_title = ""
        self._w = self._h = 0

    @staticmethod
    def list_windows():
        if win32gui is None:
            return []
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
            win32gui.ShowWindow(self.hwnd, 9)
            win32gui.SetForegroundWindow(self.hwnd)
            return True
        except Exception:
            return False

    def screenshot(self):
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
            self.hwnd, save_dc.GetSafeHdc(),
            PW_CLIENTONLY | PW_RENDERFULLCONTENT)

        img = None
        if ok:
            buf = bmp.GetBitmapBits(True)
            img = np.ascontiguousarray(
                np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))[:, :, :3])

        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(self.hwnd, hwnd_dc)
        return img

    def click(self, x, y):
        """在窗口客户区相对坐标 (x, y) 点击鼠标左键（win32api 底层实现）"""
        if self.hwnd is None:
            return False
        try:
            import win32gui, win32api, win32con
            # 客户区坐标 → 屏幕绝对坐标
            cx, cy = win32gui.ClientToScreen(self.hwnd, (int(x), int(y)))
            # 窗口置前台（保证点击被游戏接收）
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception:
                pass
            time.sleep(0.05)
            # 移动鼠标到目标位置
            win32api.SetCursorPos((cx, cy))
            time.sleep(0.05)
            # 发 leftDown + leftUp
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return True
        except Exception as e:
            print(f"[click] 失败: {e}")
            return False

    def close(self):
        pass


# ================= ② 新版：windows_capture 异步 =================
class FastWindowCapture:
    """基于 windows_capture 库的异步截图

    ★ 关键：windows_capture 要求用装饰器 @cap.event 注册事件，
      且函数名必须是 on_frame_arrived / on_closed。
      （参考项目用的就是这种写法，我上一轮写错了）
    """

    def __init__(self, target_fps=15):
        if not HAS_WINCAP:
            raise ImportError(
                "FastWindowCapture 需要 windows-capture 库："
                "pip install windows-capture")
        self.hwnd = None
        self.window_title = ""
        self._frame = None
        self._frame_lock = threading.Lock()
        self._w = 0
        self._h = 0
        self._cap = None
        self._ctrl = None
        self._target_fps = max(5, int(target_fps))
        self._last_t = 0.0

    @staticmethod
    def list_windows():
        if win32gui is None:
            return []
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
        """按标题关键字模糊绑定窗口，并启动异步截图"""
        kw = (keyword or "").strip().lower()
        target = None
        for title, hwnd in self.list_windows():
            if kw in title.lower():
                target = (title, hwnd)
                break
        if target is None:
            print(f"[FastWindowCapture] 未找到窗口: {keyword}")
            return False
        self.window_title = target[0]
        self.hwnd = target[1]

        try:
            self._cap = WindowsCapture(window_name=self.window_title)

            # ★ 关键修复：用装饰器注册事件，函数名固定
            @self._cap.event
            def on_frame_arrived(frame, capture_control):
                self._on_frame_internal(frame)

            @self._cap.event
            def on_closed():
                print("[FastWindowCapture] 窗口关闭")

            self._ctrl = self._cap.start_free_threaded()
        except Exception as e:
            print(f"[FastWindowCapture] 启动失败: {e}")
            import traceback
            traceback.print_exc()
            self.hwnd = None
            return False

        # 等第一帧（最多 1.5 秒）
        for _ in range(30):
            with self._frame_lock:
                if self._frame is not None:
                    return True
            time.sleep(0.05)
        print("[FastWindowCapture] 超时未收到首帧")
        return False

    def _on_frame_internal(self, frame):
        now = time.time()
        if now - self._last_t < 1.0 / self._target_fps:
            return
        self._last_t = now
        try:
            # 直接从 BGRA 转 BGR 并拷贝一份（一步到位）
            bgra = frame.frame_buffer  # 不拷贝
            bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)  # 拷贝 + 转换一次完成
            with self._frame_lock:
                self._frame = bgr
                self._h, self._w = frame.height, frame.width
        except Exception:
            pass

    def screenshot(self):
        with self._frame_lock:
            return None if self._frame is None else self._frame.copy()

    @property
    def size(self):
        return self._w, self._h

    def is_foreground(self) -> bool:
        return True

    def bring_foreground(self) -> bool:
        try:
            win32gui.ShowWindow(self.hwnd, 9)
            win32gui.SetForegroundWindow(self.hwnd)
            return True
        except Exception:
            return False

    def click(self, x, y):
        """在窗口客户区相对坐标 (x, y) 点击鼠标左键（win32api 底层实现）"""
        if self.hwnd is None:
            return False
        try:
            import win32gui, win32api, win32con
            # 客户区坐标 → 屏幕绝对坐标
            cx, cy = win32gui.ClientToScreen(self.hwnd, (int(x), int(y)))
            # 窗口置前台（保证点击被游戏接收）
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception:
                pass
            time.sleep(0.05)
            # 移动鼠标到目标位置
            win32api.SetCursorPos((cx, cy))
            time.sleep(0.05)
            # 发 leftDown + leftUp
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return True
        except Exception as e:
            print(f"[click] 失败: {e}")
            return False

    def close(self):
        if self._ctrl is not None:
            try:
                self._ctrl.stop()
            except Exception:
                pass
            self._ctrl = None
        self.hwnd = None


# ================= ③ VNC =================
class VncCapture:
    """VNC 虚拟机截图（vncdotool 直连）"""

    def __init__(self, host="127.0.0.1", port=5900, password=None):
        if vnc_api is None:
            raise ImportError("VNC 模式需要先安装: pip install vncdotool")
        self.host = host
        self.port = int(port) if port else 5900
        self.password = password or None
        self.client = None
        self.hwnd = None
        self.window_title = f"VNC://{host}:{port}"
        self._w = self._h = 0
        import tempfile
        fd, self._tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)

    def bind(self, keyword=None) -> bool:
        host, port, pwd = self.host, self.port, self.password
        if keyword:
            kw = str(keyword).strip()
            if ":" in kw:
                h, p = kw.rsplit(":", 1)
                try:
                    host, port = h.strip(), int(p.strip())
                except ValueError:
                    pass
        try:
            if pwd:
                self.client = vnc_api.connect(f"{host}::{port}", password=pwd)
            else:
                self.client = vnc_api.connect(f"{host}::{port}")
        except Exception as e:
            print(f"[VncCapture] 连接失败 {host}:{port} → {e}")
            self.client = None
            return False
        if self.client is None:
            return False
        self.host, self.port = host, port
        self.window_title = f"VNC://{host}:{port}"
        self.hwnd = self.client
        self.screenshot()
        return True

    def screenshot(self):
        if self.client is None:
            return None
        try:
            self.client.captureScreen(self._tmp_path)
            scr = getattr(self.client, "screen", None)
            if scr is None:
                img = cv2.imread(self._tmp_path)
                if img is not None:
                    self._h, self._w = img.shape[:2]
                return img
            arr = np.array(scr)
            if arr.ndim == 2:
                bgr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
            else:
                bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            self._h, self._w = bgr.shape[:2]
            return bgr
        except Exception as e:
            print(f"[VncCapture] 截图异常: {e}")
            return None

    @property
    def size(self):
        return self._w, self._h

    def is_foreground(self):
        return True

    def bring_foreground(self):
        return True

    # ---- 供 VNC 控制器调用 ----
    def key_down(self, key):
        if self.client is None or not key:
            return
        try:
            self.client.keyDown(key)
        except Exception:
            pass

    def key_up(self, key):
        if self.client is None or not key:
            return
        try:
            self.client.keyUp(key)
        except Exception:
            pass

    def key_press(self, key, hold=0.03):
        if self.client is None or not key:
            return
        self.key_down(key)
        time.sleep(hold)
        self.key_up(key)
        time.sleep(0.02)

    def click(self, x, y):
        """在 VNC 客户区相对坐标 (x, y) 点击鼠标左键"""
        if self.client is None:
            return False
        try:
            self.client.mouseMove(int(x), int(y))
            time.sleep(0.03)
            self.client.mouseDown(1)
            time.sleep(0.05)
            self.client.mouseUp(1)
            return True
        except Exception as e:
            print(f"[VncCapture] click 失败: {e}")
            return False

    def close(self):
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self.hwnd = None
        if self._tmp_path and os.path.isfile(self._tmp_path):
            try:
                os.remove(self._tmp_path)
            except Exception:
                pass