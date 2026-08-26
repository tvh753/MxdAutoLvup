# -*- coding: utf-8 -*-
# @Time    : 26/8/26 20:03
# @Author  : yy
# @File    : region_selector.py
# @Software: MxdAutoLvup

"""框选工具：模板截取 / 血蓝条校准 / 检测区域设置"""
import tkinter as tk
import numpy as np
from PIL import Image, ImageTk

from gui.widgets import NeoButton
from gui.theme import BG, BORDER, TEXT, TEXT_DIM, FONT


class RegionSelector(tk.Toplevel):
    def __init__(self, master, frame_bgr, mode="template", on_ok=None, tip=None):
        super().__init__(master)
        self.frame = frame_bgr
        self.mode, self.on_ok = mode, on_ok
        self.sel = None            # (x, y, w, h) 原图坐标
        self._start = None

        title = {"template": "模板截取", "bar": "状态条校准", "region": "检测区域"}[mode]
        self.title(f"区域框选 · {title}")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()

        fh, fw = frame_bgr.shape[:2]
        sw, sh = master.winfo_screenwidth(), master.winfo_screenheight()
        self.scale = min(sw * 0.7 / fw, sh * 0.7 / fh, 1.0)
        cw, ch = max(320, int(fw * self.scale)), max(240, int(fh * self.scale))

        img = Image.fromarray(np.ascontiguousarray(frame_bgr[..., ::-1]))
        if self.scale < 1.0:
            img = img.resize((cw, ch))
        self._photo = ImageTk.PhotoImage(img)

        self.tip = tk.Label(self, text=tip or "按住鼠标左键拖拽框选，然后点击【确定】",
                            bg=BG, fg=TEXT_DIM, font=(FONT, 9))
        self.tip.pack(pady=(10, 4))

        self.canvas = tk.Canvas(self, width=cw, height=ch, bg="#000",
                                highlightthickness=1, highlightbackground=BORDER,
                                cursor="crosshair")
        self.canvas.pack(padx=12, pady=4)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        bar = tk.Frame(self, bg=BG)
        bar.pack(pady=8)
        NeoButton(bar, "✔ 确 定", command=self._ok, bg="#3ddc84").pack(side="left", padx=6)
        NeoButton(bar, "✖ 取 消", command=self.destroy, bg="#3a3f55", fg=TEXT).pack(side="left", padx=6)

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda e: self.destroy())

    def _orig(self, x, y):
        return int(round(x / self.scale)), int(round(y / self.scale))

    def _press(self, e):
        self._start = (e.x, e.y)
        self.canvas.delete("sel")

    def _motion(self, e):
        if not self._start:
            return
        self.canvas.delete("sel")
        x1, y1 = self._start
        self.canvas.create_rectangle(x1, y1, e.x, e.y, outline="#ffb020", width=2, tags="sel")

    def _release(self, e):
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
        self.tip.config(text=f"已选：({ox1}, {oy1})  {w} × {h}px", fg="#ffb020")

    def _ok(self):
        if not self.sel:
            return
        x, y, w, h = self.sel
        if self.mode == "bar":  # 只回传区域，颜色由 BarMonitor 自适配
            result = {"x": x, "y": y, "w": w, "h": h}
        elif self.mode == "region":
            result = [x, y, w, h]
        else:
            result = (x, y, w, h)
        cb, self.on_ok = self.on_ok, None
        self.destroy()
        if cb:
            cb(result)