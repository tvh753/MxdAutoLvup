# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:32
# @Author  : yy
# @File    : route_painter.py
# @Software: MxdAutoLvup

# -*- coding: utf-8 -*-
"""颜色路线绘制器：在小地图快照上用颜色画路径（颜色=指令）"""
import tkinter as tk
import numpy as np
from PIL import Image, ImageTk
import cv2
from gui.widgets import NeoButton
from gui.theme import BG, BORDER, TEXT, TEXT_DIM, FONT, ACCENT, PANEL_2
from core.color_route import RAW_CODES


class RoutePainter(tk.Toplevel):
    PEN_SIZES = [("细", 1), ("中", 2), ("粗", 4)]

    def __init__(self, master, base_bgr, route_bgr=None, on_ok=None):
        super().__init__(master)
        self.title("🎨 颜色路线绘制器 · 颜色即指令")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.on_ok = on_ok
        h, w = base_bgr.shape[:2]
        self._base = base_bgr.copy()
        self.route = np.zeros((h, w, 3), np.uint8) \
            if route_bgr is None else route_bgr.copy()
        self._strokes = []  # [(bgr色, 点列, 笔宽)] 撤销栈
        self._drawing = None
        self._pen_w = 2
        self._color = tuple(RAW_CODES[0][0][::-1])  # 默认红色(BGR)
        self._cur_sw = None
        self.scale = max(1.0, min(720 / w, 520 / h, 4.0))
        self.cw, self.ch = int(w * self.scale), int(h * self.scale)
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=8, pady=(10, 4))
        tk.Label(top, text="选颜色 → 左键拖动画线 ｜ 右键撤销上一笔 ｜ "
                           "平台画横线·绳子画竖线覆盖全长·终点画黄点",
                 bg=BG, fg=TEXT_DIM, font=(FONT, 9)).pack(side="left")
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=8, pady=4)
        # ---- 左侧色板 ----
        tools = tk.Frame(main, bg=BG)
        tools.pack(side="left", fill="y", padx=(0, 8))
        self._swatches = []
        for rgb, hh, v, act, nm in RAW_CODES:
            hexc = "#%02x%02x%02x" % rgb
            c = tk.Canvas(tools, width=122, height=30, bg=PANEL_2,
                          highlightthickness=1, highlightbackground=BORDER,
                          cursor="hand2")
            c.create_rectangle(4, 5, 24, 25, fill=hexc, outline="#fff")
            icon = {"jump": "🦘", "teleport": "✨", "goal": "🏁",
                    "stop": "⏸"}.get(act, "🚶")
            c.create_text(30, 15, anchor="w", text=f"{icon} {nm}",
                          fill=TEXT, font=(FONT, 9))
            c.pack(pady=1)
            c.bind("<Button-1>",
                   lambda e, c=c, rgb=rgb: self._pick(c, tuple(rgb[::-1])))
            self._swatches.append(c)
        eraser = tk.Canvas(tools, width=122, height=30, bg=PANEL_2,
                           highlightthickness=1, highlightbackground=BORDER,
                           cursor="hand2")
        eraser.create_text(14, 15, text="⌫", fill=TEXT, font=(FONT, 10))
        eraser.create_text(34, 15, anchor="w", text="橡皮擦",
                           fill=TEXT, font=(FONT, 9))
        eraser.pack(pady=1)
        eraser.bind("<Button-1>", lambda e: self._pick(eraser, None))
        self._pick(self._swatches[0], self._color)
        pw = tk.Frame(tools, bg=BG);
        pw.pack(pady=6)
        tk.Label(pw, text="笔宽", bg=BG, fg=TEXT_DIM, font=(FONT, 9)).pack(side="left")
        self._pen_var = tk.IntVar(value=2)
        for txt, val in self.PEN_SIZES:
            tk.Radiobutton(pw, text=txt, value=val, variable=self._pen_var,
                           bg=BG, fg=TEXT_DIM, selectcolor="#141722",
                           activebackground=BG, font=(FONT, 8),
                           command=lambda v=val: setattr(self, "_pen_w", v)
                           ).pack(side="left")
        # ---- 画布 ----
        self.canvas = tk.Canvas(main, width=self.cw, height=self.ch, bg="#000",
                                highlightthickness=1, highlightbackground=BORDER,
                                cursor="crosshair")
        self.canvas.pack(side="left")
        self.canvas.bind("<Button-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.canvas.bind("<Button-3>", lambda e: self._undo())
        self._render()
        bar = tk.Frame(self, bg=BG)
        bar.pack(pady=8)
        NeoButton(bar, "✔ 保存路线", command=self._ok, bg="#3ddc84").pack(side="left", padx=4)
        NeoButton(bar, "↩ 撤销", command=self._undo, bg="#3a3f55", fg=TEXT).pack(side="left", padx=4)
        NeoButton(bar, "🗑 清空", command=self._clear, bg="#3a3f55", fg=TEXT).pack(side="left", padx=4)
        NeoButton(bar, "✖ 取消", command=self.destroy, bg="#3a3f55", fg=TEXT).pack(side="left", padx=4)
        self.bind("<Escape>", lambda e: self.destroy())

    # ---------- 色板 ----------
    def _pick(self, swatch, bgr_color):
        self._color = bgr_color
        if self._cur_sw is not None:
            self._cur_sw.config(highlightbackground=BORDER)
        self._cur_sw = swatch
        swatch.config(highlightbackground=ACCENT)

    # ---------- 画布 ----------
    def _to_orig(self, cx, cy):
        return (int(cx / self.scale), int(cy / self.scale))

    def _render(self):
        show = self._base.copy()
        nz = self.route.max(2) > 40
        show[nz] = self.route[nz]
        img = cv2.cvtColor(show, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (self.cw, self.ch),
                         interpolation=cv2.INTER_NEAREST)
        self._photo = ImageTk.PhotoImage(Image.fromarray(img))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def _stroke_color(self):
        return self._color if self._color else (0, 0, 0)

    def _down(self, e):
        p = self._to_orig(e.x, e.y)
        cv2.circle(self.route, p, self._pen_w // 2 + 1, self._stroke_color(), -1)
        self._drawing = [p]
        self._render()

    def _drag(self, e):
        if not self._drawing:
            return
        p = self._to_orig(e.x, e.y)
        q = self._drawing[-1]
        if p == q:
            return
        cv2.line(self.route, q, p, self._stroke_color(), self._pen_w)
        self._drawing.append(p)
        self._render()

    def _up(self, _):
        if self._drawing:
            self._strokes.append((self._stroke_color(),
                                  list(self._drawing), self._pen_w))
        self._drawing = None

    def _undo(self):
        if not self._strokes:
            return
        self._strokes.pop()
        self.route[:, :] = 0
        for color, pts, w in self._strokes:
            cv2.circle(self.route, pts[0], w // 2 + 1, color, -1)
            for a, b in zip(pts, pts[1:]):
                cv2.line(self.route, a, b, color, w)
        self._render()

    def _clear(self):
        self._strokes = []
        self.route[:, :] = 0
        self._render()

    def _ok(self):
        if not self._strokes and self.route.max() == 0:
            return
        cb, self.on_ok = self.on_ok, None
        result = self.route.copy()
        self.destroy()
        if cb:
            cb(result)