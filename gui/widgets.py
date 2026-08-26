"""自绘组件：NeoButton（圆角按钮）/ Bar（资源条）/ KeyEntry（按键捕获）

⚠ 子类化 tkinter 组件时，禁止把实例属性命名为 tkinter 保留字段：
  _w / _name / tk / master / children / widgetName / _tclCommands /
  _last_child_ids —— 覆盖后组件的 Tcl 身份失效，所有调用立即报
  invalid command name。
"""
import tkinter as tk
from tkinter import font as tkfont

from gui.theme import (PANEL, PANEL_2, BORDER, TEXT, ACCENT, FONT, MONO)


def round_rect(cv, x1, y1, x2, y2, r=8, **kw):
    """用平滑多边形近似圆角矩形"""
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def _shift(c, amt):
    """十六进制颜色明暗偏移"""
    r = min(255, max(0, int(c[1:3], 16) + amt))
    g = min(255, max(0, int(c[3:5], 16) + amt))
    b = min(255, max(0, int(c[5:7], 16) + amt))
    return f"#{r:02x}{g:02x}{b:02x}"


class NeoButton(tk.Canvas):
    """圆角按钮：悬浮高亮 + 按压反馈

    修复记录：点击回调若销毁了按钮所在窗口（如 RegionSelector 的
    「确定」按钮 destroy 整个 Toplevel），先前排定的 after 恢复回调
    会在已销毁组件上重绘 → TclError: invalid command name。
    对策：恢复回调带 winfo_exists 守卫 + destroy 时取消挂起任务。
    """

    def __init__(self, master, text="", command=None, bg=ACCENT, fg="#16181f",
                 font=(FONT, 10, "bold"), padx=14, pady=7, radius=8, **kw):
        f = tkfont.Font(font=font)
        super().__init__(master, width=f.measure(text) + padx * 2,
                         height=f.metrics("linespace") + pady * 2,
                         highlightthickness=0, bd=0, **kw)
        self._text, self._cmd = text, command
        self._bg, self._fg, self._font, self._radius = bg, fg, font, radius
        self._hover, self._press = _shift(bg, 25), _shift(bg, -25)
        self._after_id = None
        self._draw(self._bg)
        self.bind("<Enter>", lambda e: self._draw(self._hover))
        self.bind("<Leave>", lambda e: self._draw(self._bg))
        self.bind("<Button-1>", self._on_click)

    def _on_click(self, _):
        self._draw(self._press)
        if self._cmd:
            try:
                self._cmd()
            finally:
                self._after_id = self.after(110, self._restore)

    def _restore(self):
        self._after_id = None
        try:
            if self.winfo_exists():  # ← 窗口已销毁则静默跳过
                self._draw(self._hover)
        except tk.TclError:
            pass  # 解释器正在关闭

    def destroy(self):
        if self._after_id is not None:  # ← 主动销毁时取消挂起任务
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        super().destroy()

    def _draw(self, bg):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        round_rect(self, 1, 1, w - 1, h - 1, self._radius, fill=bg, outline="")
        self.create_text(w / 2, h / 2, text=self._text, fill=self._fg, font=self._font)


class Bar(tk.Canvas):
    """HP / MP 资源进度条

    修复记录：原实现 `self._w, self._h = width, height` 覆盖了 tkinter
    内部属性 _w（组件 Tcl 路径），导致 invalid command name "375"。
    现改为 _bw/_bh，并支持随窗口拉伸自适应重绘。
    """

    def __init__(self, master, label="", color="#ff5262", width=280, height=26, **kw):
        super().__init__(master, width=width, height=height, bg=PANEL,
                         highlightthickness=1, highlightbackground=BORDER, **kw)
        self._bw, self._bh = width, height  # ✅ 避开保留名 _w / _h
        self._label, self._color = label, color
        self._val = -1
        self.bind("<Configure>", self._on_configure)
        self._render()

    def set(self, pct):
        self._val = pct
        self._render()

    def _on_configure(self, e):
        """窗口尺寸变化时同步缓存并重绘"""
        if e.width > 20 and abs(e.width - self._bw) > 1:
            self._bw, self._bh = e.width, max(20, e.height)
            self._render()

    def _render(self):
        self.delete("all")
        w, h = self._bw, self._bh
        round_rect(self, 1, 1, w - 2, h - 2, 5, fill="#0a0c12", outline="")
        pct = self._val if (self._val and self._val > 0) else 0
        fw = int((w - 8) * min(100, pct) / 100)
        if fw > 8:
            round_rect(self, 4, 4, 4 + fw, h - 4, 4, fill=self._color, outline="")
        self.create_text(12, h / 2, anchor="w", text=self._label,
                         fill="#fff", font=(FONT, 9, "bold"))
        txt = f"{self._val:.0f}%" if self._val >= 0 else "--"
        self.create_text(w - 10, h / 2, anchor="e", text=txt,
                         fill="#fff", font=(MONO, 9, "bold"))


class KeyEntry(tk.Entry):
    """点击进入捕获态 → 按任意键写入 pydirectinput 键名（Esc 取消）"""
    KEYMAP = {
        "alt_l": "alt", "alt_r": "alt", "control_l": "ctrl", "control_r": "ctrl",
        "shift_l": "shift", "shift_r": "shift", "return": "enter", "escape": "esc",
        "page_up": "pageup", "page_down": "pagedown", "caps_lock": "capslock",
        "quote": "'", "slash": "/", "semicolon": ";", "backslash": "\\",
        "comma": ",", "period": ".", "minus": "-", "equal": "=",
        "bracketleft": "[", "bracketright": "]", "grave": "`", "num_lock": "numlock",
        "left": "left", "right": "right", "up": "up", "down": "down",
    }

    def __init__(self, master, value="", on_change=None, width=9, **kw):
        self.var = tk.StringVar(value=(value or "-"))
        super().__init__(master, textvariable=self.var, width=width, justify="center",
                         bg=PANEL_2, fg=ACCENT, relief="flat", cursor="arrow",
                         insertbackground=TEXT, font=(MONO, 10, "bold"),
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, **kw)
        self._on_change = on_change
        self._capturing = False
        self._backup = ""
        self.bind("<Button-1>", self._on_click)
        self.bind("<Key>", self._on_key)

    def _on_click(self, _):
        self.focus_set()
        if not self._capturing:
            self._capturing = True
            self._backup = self.var.get()
            self.var.set("按键…")
            self.configure(fg="#7dd3fc", bg="#12203a")
        return "break"

    def _on_key(self, e):
        if not self._capturing:
            return "break"
        ks = e.keysym
        name = self._backup if ks == "Escape" else \
            self.KEYMAP.get(ks, ks.lower() if len(ks) == 1 else ks.lower())
        self.var.set(name or "-")
        self._capturing = False
        self.configure(fg=ACCENT, bg=PANEL_2)
        if self._on_change:
            self._on_change(name)
        return "break"