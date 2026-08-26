# -*- coding: utf-8 -*-
# @Time    : 26/8/26 21:02
# @Author  : yy
# @File    : route_editor.py
# @Software: MxdAutoLvup

"""巡逻路线编辑器：在放大的小地图上点击布点"""
import tkinter as tk
import numpy as np
from PIL import Image, ImageTk
import cv2

from gui.widgets import NeoButton
from gui.theme import BG, BORDER, TEXT, TEXT_DIM, FONT, ACCENT


class RouteEditor(tk.Toplevel):
    MODES = [("pingpong", "往返（到头折返）"), ("loop", "循环（首尾相连）")]

    def __init__(self, master, minimap_bgr, waypoints=None, mode="pingpong",
                 player_dot=None, on_ok=None):
        super().__init__(master)
        self.title("🗺 巡逻路线编辑器")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.on_ok = on_ok
        self.pts = [list(p) for p in (waypoints or [])]

        h, w = minimap_bgr.shape[:2]
        self.scale = max(1.0, min(560 / w, 420 / h, 3.0))   # 放大便于精准点击
        cw, ch = int(w * self.scale), int(h * self.scale)
        img = cv2.cvtColor(minimap_bgr, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (cw, ch), interpolation=cv2.INTER_NEAREST)
        self._photo = ImageTk.PhotoImage(Image.fromarray(img))

        tk.Label(self, text="左键：按顺序添加路点 ｜ 右键：撤销上一个 ｜ 沿怪物密集处布点",
                 bg=BG, fg=TEXT_DIM, font=(FONT, 9)).pack(pady=(10, 4))
        self.canvas = tk.Canvas(self, width=cw, height=ch, bg="#000",
                                highlightthickness=1, highlightbackground=BORDER,
                                cursor="crosshair")
        self.canvas.pack(padx=12)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self.canvas.bind("<Button-1>", self._add)
        self.canvas.bind("<Button-3>", self._undo)

        if player_dot:                                       # 标记玩家当前位置
            px, py = player_dot[0] * self.scale, player_dot[1] * self.scale
            self.canvas.create_oval(px - 6, py - 6, px + 6, py + 6,
                                    outline="#3ddc84", width=2, tags="dot")
            self.canvas.create_text(px, py - 12, text="玩家", fill="#3ddc84",
                                    font=(FONT, 8, "bold"), tags="dot")

        modebox = tk.Frame(self, bg=BG)
        modebox.pack(pady=(8, 0))
        tk.Label(modebox, text="路线模式：", bg=BG, fg=TEXT, font=(FONT, 9)).pack(side="left")
        self._mode_var = tk.StringVar(value=mode if mode in ("pingpong", "loop") else "pingpong")
        for val, txt in self.MODES:
            tk.Radiobutton(modebox, text=txt, value=val, variable=self._mode_var,
                           bg=BG, fg=TEXT_DIM, selectcolor="#141722",
                           activebackground=BG, activeforeground=TEXT,
                           font=(FONT, 9)).pack(side="left", padx=8)

        self.count_lbl = tk.Label(self, bg=BG, fg=ACCENT, font=(FONT, 9))
        self.count_lbl.pack(pady=(4, 0))

        bar = tk.Frame(self, bg=BG)
        bar.pack(pady=8)
        NeoButton(bar, "✔ 保存路线", command=self._ok, bg="#3ddc84").pack(side="left", padx=6)
        NeoButton(bar, "↩ 撤销", command=self._undo, bg="#3a3f55", fg=TEXT).pack(side="left", padx=6)
        NeoButton(bar, "🗑 清空", command=lambda: (self.pts.clear(), self._redraw()),
                  bg="#3a3f55", fg=TEXT).pack(side="left", padx=6)
        NeoButton(bar, "✖ 取消", command=self.destroy, bg="#3a3f55", fg=TEXT).pack(side="left", padx=6)

        self._redraw()
        self.bind("<Escape>", lambda e: self.destroy())

    def _add(self, e):
        self.pts.append([int(e.x / self.scale), int(e.y / self.scale)])
        self._redraw()

    def _undo(self, _=None):
        if self.pts:
            self.pts.pop()
        self._redraw()

    def _redraw(self):
        self.canvas.delete("route")
        for i, (x, y) in enumerate(self.pts):
            cx, cy = x * self.scale, y * self.scale
            if i:
                px, py = self.pts[i - 1]
                self.canvas.create_line(px * self.scale, py * self.scale, cx, cy,
                                        fill="#ffb020", width=2, tags="route")
            self.canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5,
                                    fill="#ffb020", outline="#fff", tags="route")
            self.canvas.create_text(cx, cy, text=str(i + 1), fill="#16181f",
                                    font=(FONT, 8, "bold"), tags="route")
        if self._mode_var.get() == "loop" and len(self.pts) > 2:
            x0, y0 = self.pts[0]
            xn, yn = self.pts[-1]
            self.canvas.create_line(xn * self.scale, yn * self.scale,
                                    x0 * self.scale, y0 * self.scale,
                                    fill="#ffb020", width=2, dash=(4, 3), tags="route")
        n = len(self.pts)
        self.count_lbl.config(text=f"已布 {n} 个路点" + ("" if n >= 2 else "（至少 2 个才能巡逻）"))

    def _ok(self):
        if len(self.pts) < 2:
            self.count_lbl.config(text="⚠ 至少需要 2 个路点", fg="#ff6b6b")
            return
        cb, self.on_ok = self.on_ok, None
        result = {"waypoints": [list(map(int, p)) for p in self.pts],
                  "mode": self._mode_var.get()}
        self.destroy()
        if cb:
            cb(result)