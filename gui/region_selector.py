# -*- coding: utf-8 -*-
# @Time    : 26/8/26 20:03
# @Author  : yy
# @File    : region_selector.py
# @Software: MxdAutoLvup

"""框选工具：模板截取 / 血蓝条校准 / 检测区域设置

一个模态子窗口：把传入的画面帧按比例缩放到屏幕上，
用户拖拽鼠标框选，再用方向键微调。三种模式（mode）决定回传结果格式：
  template —— 模板截取 → (x, y, w, h)
  bar      —— 状态条校准 → {"x","y","w","h"}
  region   —— 检测区域 → [x, y, w, h]

键盘微调：
  · 方向键            → 整体移动 1px
  · Shift + 方向键    → 宽度/高度 ±1px
"""
import tkinter as tk
import numpy as np
from PIL import Image, ImageTk

from gui.widgets import NeoButton
from gui.theme import BG, BORDER, TEXT, TEXT_DIM, FONT


class RegionSelector(tk.Toplevel):
    """跨窗口区域框选工具（模态 Toplevel）"""

    def __init__(self, master, frame_bgr, mode="template", on_ok=None, tip=None):
        super().__init__(master)
        self.frame = frame_bgr
        self.mode, self.on_ok = mode, on_ok
        self.sel = None            # (x, y, w, h) 原图坐标（拖拽后由键盘微调）
        self._start = None         # 拖拽起点（画布坐标）

        title = {"template": "模板截取", "bar": "状态条校准", "region": "检测区域"}[mode]
        self.title(f"区域框选 · {title}")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()  # 模态

        # 按屏幕尺寸等比缩放画面
        fh, fw = frame_bgr.shape[:2]
        sw, sh = master.winfo_screenwidth(), master.winfo_screenheight()
        self.scale = min(sw * 0.7 / fw, sh * 0.7 / fh, 1.0)
        cw, ch = max(320, int(fw * self.scale)), max(240, int(fh * self.scale))

        img = Image.fromarray(np.ascontiguousarray(frame_bgr[..., ::-1]))
        if self.scale < 1.0:
            img = img.resize((cw, ch))
        self._photo = ImageTk.PhotoImage(img)

        # 顶部提示：拖拽 + 键盘微调说明
        default_tip = ("拖拽鼠标框选 → 方向键微调位置 → Shift+方向键微调大小\n"
                       "然后点【确定】")
        self.tip = tk.Label(self, text=tip or default_tip,
                            bg=BG, fg=TEXT_DIM, font=(FONT, 9),
                            justify="left")
        self.tip.pack(pady=(10, 4))

        self.canvas = tk.Canvas(self, width=cw, height=ch, bg="#000",
                                highlightthickness=1, highlightbackground=BORDER,
                                cursor="crosshair", takefocus=True)
        self.canvas.pack(padx=12, pady=4)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        bar = tk.Frame(self, bg=BG)
        bar.pack(pady=8)
        NeoButton(bar, "✔ 确 定", command=self._ok, bg="#3ddc84").pack(side="left", padx=6)
        NeoButton(bar, "✖ 取 消", command=self.destroy, bg="#3a3f55", fg=TEXT).pack(side="left", padx=6)

        # 鼠标事件
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)

        # ★ 键盘事件绑到 Toplevel，无论焦点在哪都能收到
        self.bind("<KeyPress>", self._on_key)
        self.bind("<Escape>", lambda e: self.destroy())

        # 让 Canvas 一上来就有焦点（方便直接方向键操作）
        self.canvas.focus_set()

    # ============ 坐标换算 ============
    def _orig(self, x, y):
        """画布坐标 → 原图坐标"""
        return int(round(x / self.scale)), int(round(y / self.scale))

    def _canvas(self, x, y):
        """原图坐标 → 画布坐标"""
        return x * self.scale, y * self.scale

    # ============ 鼠标事件 ============
    def _press(self, e):
        """按下：记录起点，清掉旧框"""
        self._start = (e.x, e.y)
        self.canvas.delete("sel")

    def _motion(self, e):
        """拖动：实时绘制金色选框（1px 线宽）"""
        if not self._start:
            return
        self.canvas.delete("sel")
        x1, y1 = self._start
        self.canvas.create_rectangle(x1, y1, e.x, e.y,
                                     outline="#ffb020", width=1, tags="sel")

    def _release(self, e):
        """松开：换算回原图坐标并保留框，供键盘微调"""
        if not self._start:
            return
        x1, y1 = self._start
        self._start = None
        ox1, oy1 = self._orig(min(x1, e.x), min(y1, e.y))
        ox2, oy2 = self._orig(max(x1, e.x), max(y1, e.y))
        w, h = ox2 - ox1, oy2 - oy1
        if w < 4 or h < 4:
            return
        self.sel = (ox1, oy1, w, h)
        self._draw_sel()

    # ============ 重绘框 ============
    def _draw_sel(self):
        """按 self.sel（原图坐标）重绘金色框，并刷新提示文字"""
        self.canvas.delete("sel")
        if not self.sel:
            return
        x, y, w, h = self.sel
        cx1, cy1 = self._canvas(x, y)
        cx2, cy2 = self._canvas(x + w, y + h)
        self.canvas.create_rectangle(cx1, cy1, cx2, cy2,
                                     outline="#ffb020", width=1, tags="sel")
        self.tip.config(
            text=f"已选：({x}, {y})  {w} × {h}px\n"
                 f"方向键微调位置 · Shift+方向键微调大小",
            fg="#ffb020")

    # ============ 键盘微调 ============
    def _on_key(self, e):
        """方向键微调：无 Shift 移动位置，Shift+方向键改大小

        tkinter 的 e.state 位掩码：
          0x0001 = Shift
          0x0004 = Control
        """
        # 拖拽中忽略
        if self._start is not None:
            return
        if not self.sel:
            return

        key = e.keysym
        if key not in ("Left", "Right", "Up", "Down"):
            return

        # 判断 Shift：true = 改大小，false = 移动
        shift = bool(e.state & 0x0001)
        x, y, w, h = self.sel

        if shift:
            # Shift+方向键 → 改大小
            if key == "Left":
                w = max(1, w - 1)
            elif key == "Right":
                w = w + 1
            elif key == "Up":
                h = max(1, h - 1)
            elif key == "Down":
                h = h + 1
        else:
            # 方向键 → 整体移动
            if key == "Left":
                x -= 1
            elif key == "Right":
                x += 1
            elif key == "Up":
                y -= 1
            elif key == "Down":
                y += 1

        self.sel = (x, y, w, h)
        self._draw_sel()

    # ============ 确定 ============
    def _ok(self):
        """确定：按模式格式化结果并通过 on_ok 回调返回"""
        if not self.sel:
            return
        x, y, w, h = self.sel
        if self.mode == "bar":
            result = {"x": x, "y": y, "w": w, "h": h}
        elif self.mode == "region":
            result = [x, y, w, h]
        else:
            result = (x, y, w, h)
        cb, self.on_ok = self.on_ok, None
        self.destroy()
        if cb:
            cb(result)