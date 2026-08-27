# -*- coding: utf-8 -*-
# @Time    : 26/8/26 20:03
# @Author  : yy
# @File    : main_window.py
# @Software: MxdAutoLvup

"""枫叶挂机控制台 · 主界面"""
import os, time, queue
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
import cv2
from PIL import Image, ImageTk

from gui.theme import *
from gui.widgets import NeoButton, Bar, KeyEntry, ScrollFrame
from gui.region_selector import RegionSelector
# from gui.route_editor import RouteEditor
# from core.config_manager import ConfigManager, TEMPLATE_DIR, ROOT
from core.bot_engine import BotEngine, Mode
from core.window_capture import WindowCapture
from gui.route_painter import RoutePainter
from core.map_manager import MapManager
from core.config_manager import ConfigManager, TEMPLATE_DIR, ROOT
from core.imio import imwrite_u

class App(tk.Tk):
    PREVIEW_W, PREVIEW_H = 760, 430
    KEY_ROWS = [
        ("普通攻击", "attack"), ("技能1", "skill1"), ("技能2", "skill2"), ("技能3", "skill3"),
        ("红药", "hp_potion"), ("蓝药", "mp_potion"), ("拾取", "pickup"),
        ("左移", "move_left"), ("右移", "move_right"), ("跳跃", "jump"),
        ("上(抓绳)", "up"), ("下(下绳)", "down"), ("传送(法师)", "teleport"),
    ]

    def __init__(self):
        super().__init__()
        self.title("枫叶挂机控制台 · MapleStory Auto Level Up")
        self.configure(bg=BG)
        self.geometry("1280x800")
        self.minsize(1180, 740)

        self.cfg_mgr = ConfigManager()
        self.cfg = self.cfg_mgr.cfg
        self.log_queue = queue.Queue()
        self._pv_photo = None
        self._pill_state = None

        self.engine = BotEngine(self.cfg, lambda m, lv="info": self.log_queue.put(
            (time.strftime("%H:%M:%S"), m, lv)))
        self.maps = MapManager(ROOT)
        self._minimap_snap = None  # 当前小地图底图（录制）
        self._route_img = None  # 当前颜色路线层
        self.engine.start()

        self._build_style()
        self._build_layout()
        self._poll_status()
        self._poll_log()
        self.bind("<F8>", self.toggle_run)
        self.bind("<F9>", self.toggle_pause)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log("控制台就绪：① 绑定窗口 → ② 校准血蓝条/框选模板 → ③ 检查按键 → ④ ▶ 启动", "ok")

    # ================= 样式 =================
    def _build_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, bordercolor=BORDER,
                    troughcolor="#0a0c12", fieldbackground=PANEL_2,
                    lightcolor=PANEL_2, darkcolor=PANEL_2)
        s.configure("TNotebook", background=PANEL, borderwidth=0, tabmargins=(6, 6, 6, 0))
        s.configure("TNotebook.Tab", background=PANEL_2, foreground=TEXT_DIM,
                    padding=(14, 7), font=(FONT, 10))
        s.map("TNotebook.Tab", background=[("selected", ACCENT)],
              foreground=[("selected", "#16181f")])
        s.configure("TScale", background=PANEL_2)
        s.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2,
                    foreground=TEXT, arrowcolor=TEXT)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL_2)],
              foreground=[("readonly", TEXT)])
        self.option_add("*TCombobox*Listbox*Background", PANEL_2)
        self.option_add("*TCombobox*Listbox*Foreground", TEXT)
        self.option_add("*TCombobox*Listbox*selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox*selectForeground", "#16181f")

    # ================= 布局 =================
    def _build_layout(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(header, text="🍁 枫叶挂机控制台", font=(FONT, 16, "bold"),
                 bg=BG, fg=TEXT).pack(side="left")
        tk.Label(header, text="  窗口截图 · 模板识别 · 自动战斗 · 资源监控",
                 font=(FONT, 9), bg=BG, fg=TEXT_DIM).pack(side="left", pady=(8, 0))
        self._build_pill(header)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=PANEL, width=352)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        left.pack_propagate(False)
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_left(left)
        self._build_right(right)

    def _build_pill(self, parent):
        f = tk.Frame(parent, bg=PANEL_2, padx=12, pady=5)
        f.pack(side="right")
        self.pill_dot = tk.Canvas(f, width=10, height=10, bg=PANEL_2, highlightthickness=0)
        self.pill_dot.pack(side="left")
        self.pill_lbl = tk.Label(f, text="待机", bg=PANEL_2, fg=TEXT_DIM,
                                 font=(FONT, 10, "bold"))
        self.pill_lbl.pack(side="left", padx=(6, 0))

    def _section(self, parent, title):
        box = tk.Frame(parent, bg=PANEL_2, padx=10, pady=8,
                       highlightthickness=1, highlightbackground=BORDER)
        tk.Label(box, text=title, bg=PANEL_2, fg=ACCENT,
                 font=(FONT, 10, "bold")).pack(anchor="w")
        body = tk.Frame(box, bg=PANEL_2)
        body.pack(fill="x", pady=(6, 0))
        return box, body

    # ---------- 左侧：配置面板 ----------
    def _build_left(self, left):
        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        t1 = ScrollFrame(nb, bg=PANEL);
        nb.add(t1, text=" 🎯 目标 ")
        t2 = ScrollFrame(nb, bg=PANEL);
        nb.add(t2, text=" ⌨ 按键 ")
        t3 = ScrollFrame(nb, bg=PANEL);
        nb.add(t3, text=" ⚙ 参数 ")
        self._build_target_tab(t1.content)  # ← 传入 .content
        self._build_key_tab(t2.content)
        self._build_param_tab(t3.content)

        ctrl = tk.Frame(left, bg=PANEL)
        ctrl.pack(fill="x", padx=8, pady=(0, 4))
        self.btn_start = NeoButton(ctrl, "▶ 启动挂机", command=self.start_bot,
                                   bg=GREEN, padx=22)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 6))
        NeoButton(ctrl, "⏹ 停止", command=self.stop_bot, bg=RED, fg="#fff",
                  padx=18).pack(side="left", padx=(0, 6))
        NeoButton(ctrl, "⏸", command=self.toggle_pause, bg="#3a3f55", fg=TEXT,
                  padx=12).pack(side="left")
        tk.Label(left, text="F8 启动/停止 · F9 暂停/恢复（控制台聚焦时生效）",
                 bg=PANEL, fg=TEXT_DIM, font=(FONT, 8)).pack(pady=(0, 8))

    def _build_target_tab(self, tab):
        # 窗口绑定
        box, body = self._section(tab, "🪟 窗口绑定")
        box.pack(fill="x", padx=8, pady=(8, 4))
        row = tk.Frame(body, bg=PANEL_2);
        row.pack(fill="x")
        self.win_combo = ttk.Combobox(row, state="readonly")
        self.win_combo.pack(side="left", fill="x", expand=True)
        NeoButton(row, "⟳", command=self.refresh_windows, bg=PANEL, fg=TEXT,
                  padx=9).pack(side="left", padx=(6, 0))
        NeoButton(row, "绑定", command=self.bind_window, padx=10).pack(side="left", padx=(6, 0))
        self.win_label = tk.Label(body, text="未绑定", fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 9))
        self.win_label.pack(anchor="w", pady=(4, 0))
        self.refresh_windows()

        # 模板
        box, body = self._section(tab, "👾 目标模板（怪物 / 玩家）")
        box.pack(fill="both", expand=True, padx=8, pady=4)
        lw = tk.Frame(body, bg=PANEL_2)
        lw.pack(fill="both", expand=True)
        self.tpl_listbox = tk.Listbox(lw, bg="#141722", fg=TEXT, height=6,
                                      selectbackground=ACCENT, selectforeground="#16181f",
                                      highlightthickness=1, highlightbackground=BORDER,
                                      relief="flat", font=(FONT, 9), activestyle="none")
        sb = tk.Scrollbar(lw, command=self.tpl_listbox.yview)
        self.tpl_listbox.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tpl_listbox.pack(side="left", fill="both", expand=True)
        row = tk.Frame(body, bg=PANEL_2);
        row.pack(fill="x", pady=(6, 0))
        NeoButton(row, "📷 框选怪物模板", command=self.add_monster_template).pack(side="left")
        NeoButton(row, "🗑 删除", command=self.remove_template, bg="#3a3f55",
                  fg=TEXT).pack(side="left", padx=(6, 0))
        row2 = tk.Frame(body, bg=PANEL_2);
        row2.pack(fill="x", pady=(6, 0))
        NeoButton(row2, "🧍 框选玩家模板", command=self.add_player_template,
                  bg="#2f6f4f", fg=TEXT).pack(side="left")
        NeoButton(row2, "清除玩家", command=self.clear_player, bg="#3a3f55",
                  fg=TEXT).pack(side="left", padx=(6, 0))
        self.player_label = tk.Label(body, text="玩家模板：未设置", fg=TEXT_DIM,
                                     bg=PANEL_2, font=(FONT, 9))
        self.player_label.pack(anchor="w", pady=(2, 0))
        self.refresh_tpl_list()

        # 状态条 & 检测区域
        box, body = self._section(tab, "❤ 状态条校准（红HP · 蓝MP · 黄EXP）")
        box.pack(fill="x", padx=8, pady=(4, 8))
        row = tk.Frame(body, bg=PANEL_2);
        row.pack(fill="x")
        NeoButton(row, "🎯 HP条", command=lambda: self.calibrate_bar("hp"),
                  bg=HP, fg="#fff").pack(side="left")
        NeoButton(row, "🎯 MP条", command=lambda: self.calibrate_bar("mp"),
                  bg=MP, fg="#fff").pack(side="left", padx=(6, 0))
        NeoButton(row, "🎯 EXP条", command=lambda: self.calibrate_bar("exp"),
                  bg="#ffd23e", fg="#16181f").pack(side="left", padx=(6, 0))
        row2 = tk.Frame(body, bg=PANEL_2);
        row2.pack(fill="x", pady=(6, 0))
        NeoButton(row2, "🧭 检测区域", command=self.calibrate_region,
                  bg="#3a3f55", fg=TEXT).pack(side="left")
        NeoButton(row2, "⛶ 全屏", command=self.clear_region,
                  bg="#3a3f55", fg=TEXT).pack(side="left", padx=(6, 0))
        tk.Label(body, text="提示：满血/满蓝时完整框选整条状态条（含空槽），颜色自动识别",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8)).pack(anchor="w", pady=(4, 0))

        # 地图包 & 颜色路线
        box, body = self._section(tab, "🗺 地图包 · 颜色路线（录制一次，处处复用）")
        box.pack(fill="x", padx=8, pady=(4, 8))
        row = tk.Frame(body, bg=PANEL_2); row.pack(fill="x")
        self.maps_combo = ttk.Combobox(row, state="readonly", width=8)
        self.maps_combo.pack(side="left")
        NeoButton(row, "⟳", command=self.refresh_maps, bg=PANEL, fg=TEXT,
                  padx=8, font=(FONT, 9)).pack(side="left", padx=(3, 0))
        NeoButton(row, "加载", command=self.load_map_pack, padx=9,
                  font=(FONT, 9)).pack(side="left", padx=(3, 0))
        NeoButton(row, "保存", command=self.save_map_pack, padx=9,
                  font=(FONT, 9), bg="#2f6f4f", fg=TEXT).pack(side="left", padx=(3, 0))
        NeoButton(row, "删", command=self.delete_map_pack, padx=9,
                  font=(FONT, 9), bg="#3a3f55", fg=TEXT).pack(side="left", padx=(3, 0))
        row2 = tk.Frame(body, bg=PANEL_2); row2.pack(fill="x", pady=(5, 0))
        NeoButton(row2, "🧭 校准小地图", command=self.calibrate_minimap,
                  padx=8, font=(FONT, 9)).pack(side="left")
        NeoButton(row2, "📷 录制小地图", command=self.record_minimap,
                  padx=8, font=(FONT, 9)).pack(side="left", padx=(4, 0))
        row3 = tk.Frame(body, bg=PANEL_2); row3.pack(fill="x", pady=(5, 0))
        NeoButton(row3, "🎨 绘制路线", command=self.paint_route, padx=8,
                  font=(FONT, 9), bg="#2f6f4f", fg=TEXT).pack(side="left")
        row4 = tk.Frame(body, bg=PANEL_2); row4.pack(fill="x", pady=(5, 0))
        self.patrol_var = tk.BooleanVar(
            value=self.cfg.get("patrol", {}).get("enabled", False))
        tk.Checkbutton(row4, text="启用路线巡逻", variable=self.patrol_var,
                       bg=PANEL_2, fg=TEXT, selectcolor="#141722",
                       activebackground=PANEL_2, activeforeground=TEXT,
                       font=(FONT, 9),
                       command=lambda: self._toggle_patrol(self.patrol_var)).pack(side="left")
        tk.Label(row4, text="（红左走·蓝右走·灰上爬绳，Shift画直线）",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8)).pack(side="left", padx=(4, 0))
        self.patrol_label = tk.Label(body, text="", fg=TEXT_DIM, bg=PANEL_2,
                                     font=(FONT, 9))
        self.patrol_label.pack(anchor="w", pady=(4, 0))
        self.refresh_maps()
        self.refresh_patrol_label()

    def _build_key_tab(self, tab):
        box, body = self._section(tab, "⌨ 按键映射（点击输入框 → 按键盘按键）")
        box.pack(fill="x", padx=8, pady=8)
        self._key_entries = {}
        for i, (label, key) in enumerate(self.KEY_ROWS):
            r, c = divmod(i, 2)
            cell = tk.Frame(body, bg=PANEL_2)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 14), pady=5)
            tk.Label(cell, text=label, bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                     width=6, anchor="w").pack(side="left")
            ent = KeyEntry(cell, value=self.cfg["keys"].get(key, ""),
                           on_change=lambda v, k=key: self._set_key(k, v))
            ent.pack(side="left")
            self._key_entries[key] = ent
        tk.Label(body, text="点击输入框→按键绑定；Backspace 清空（技能默认空=不使用技能）",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8)).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _build_param_tab(self, tab):
        box, body = self._section(tab, "🎚 识别与策略参数")
        box.pack(fill="x", padx=8, pady=8)
        def slider(label, frm, to, key, fmt="{:.0f}"):
            row = tk.Frame(body, bg=PANEL_2);
            row.pack(fill="x", pady=4)
            tk.Label(row, text=label, bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                     width=9, anchor="w").pack(side="left")
            vl = tk.Label(row, text=fmt.format(self.cfg["thresholds"][key]),
                          bg=PANEL_2, fg=ACCENT, font=(MONO, 9, "bold"), width=6)
            vl.pack(side="right")
            sc = ttk.Scale(row, from_=frm, to=to, value=self.cfg["thresholds"][key],
                           command=lambda v: (self.cfg["thresholds"].__setitem__(
                               key, float(v)), vl.config(text=fmt.format(float(v)))))
            sc.pack(side="left", fill="x", expand=True, padx=(4, 8))
            sc.bind("<ButtonRelease-1>", lambda e: self.cfg_mgr.save())
        slider("匹配阈值", 0.50, 0.98, "match", "{:.2f}")
        slider("红药阈值%", 10, 90, "hp_potion")
        slider("蓝药阈值%", 10, 90, "mp_potion")
        slider("攻击距离px", 40, 400, "attack_range")
        slider("技能范围px", 80, 500, "skill_range")
        slider("追击距离px", 60, 500, "chase_range")
        slider("偏离容差px", 10, 60, "off_route_tol")
        slider("拾取间隔s", 0.1, 1.5, "pickup_interval")
        slider("喝药冷却s", 0.5, 5, "potion_cooldown", "{:.1f}")
        slider("巡逻换向s", 1, 8, "roam_interval", "{:.1f}")

        box2, body2 = self._section(tab, "🛡 行为开关")
        box2.pack(fill="x", padx=8, pady=(0, 8))
        for key, label in [("use_skill_rotation", "技能轮换输出（攻击/技能循环）"),
                           ("jump_while_roam", "巡逻时随机跳跃"),
                           ("stop_on_low_hp", "血量过低自动停机保护"),
                           ("pause_on_unfocus", "游戏失焦时暂停按键（推荐开启）"),
                           ("loot_enabled", "边走边自动拾取（需配置拾取按键）"),]:
            var = tk.BooleanVar(value=self.cfg["options"].get(key, False))
            tk.Checkbutton(body2, text=label, variable=var, bg=PANEL_2, fg=TEXT,
                           selectcolor="#141722", activebackground=PANEL_2,
                           activeforeground=TEXT, font=(FONT, 9), anchor="w",
                           command=lambda k=key, v=var: (self.cfg["options"].__setitem__(
                               k, v.get()), self.cfg_mgr.save())).pack(fill="x", pady=1)

        box3, body3 = self._section(tab, "🗺 巡逻微调")
        box3.pack(fill="x", padx=8, pady=(0, 8))
        def pslider(label, frm, to, key, default, fmt="{:.0f}"):
            p = self.cfg.setdefault("patrol", {})
            row = tk.Frame(body3, bg=PANEL_2);
            row.pack(fill="x", pady=3)
            tk.Label(row, text=label, bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                     width=9, anchor="w").pack(side="left")
            vl = tk.Label(row, text=fmt.format(p.get(key, default)),
                          bg=PANEL_2, fg=ACCENT, font=(MONO, 9, "bold"), width=6)
            vl.pack(side="right")
            sc = ttk.Scale(row, from_=frm, to=to, value=p.get(key, default),
                           command=lambda v: (p.__setitem__(key, round(float(v), 2)),
                                              vl.config(text=fmt.format(float(v)))))
            sc.pack(side="left", fill="x", expand=True, padx=(4, 8))
            sc.bind("<ButtonRelease-1>", lambda e: (self.cfg_mgr.save(),
                                                    self.engine.reload_runtime()))
        pslider("搜索半径", 4, 25, "search_range", 10)
        pslider("抓绳容差", 2, 10, "grab_tol", 4)
        pslider("玩家点面积", 12, 120, "dot_max_area", 40)
        pslider("追击时限s", 1.0, 10.0, "max_chase_time", 4.0, "{:.1f}")

        box4, body4 = self._section(tab, "⏱ 挂机时长（到点休息，自动循环）")
        box4.pack(fill="x", padx=8, pady=(0, 8))
        sch = self.cfg.setdefault("schedule", {})
        svar = tk.BooleanVar(value=sch.get("enabled", False))
        tk.Checkbutton(body4, text="启用定时休息（实际时长随机 ±3 分钟）", variable=svar,
                       bg=PANEL_2, fg=TEXT, selectcolor="#141722",
                       activebackground=PANEL_2, activeforeground=TEXT,
                       font=(FONT, 9), anchor="w",
                       command=lambda: (sch.__setitem__("enabled", svar.get()),
                                        self.cfg_mgr.save())).pack(anchor="w")
        row = tk.Frame(body4, bg=PANEL_2);
        row.pack(fill="x", pady=3)
        tk.Label(row, text="挂机时长", bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                 width=9, anchor="w").pack(side="left")
        vl = tk.Label(row, text=f"{sch.get('duration_min', 60)}分", bg=PANEL_2,
                      fg=ACCENT, font=(MONO, 9, "bold"), width=7)
        vl.pack(side="right")
        sc = ttk.Scale(row, from_=15, to=240, value=sch.get("duration_min", 60),
                       command=lambda v: (sch.__setitem__("duration_min", int(float(v))),
                                          vl.config(text=f"{int(float(v))}分")))
        sc.pack(side="left", fill="x", expand=True, padx=(4, 8))
        sc.bind("<ButtonRelease-1>", lambda e: self.cfg_mgr.save())
        tk.Label(body4, text="到点后走到路线上的停止标记(浅绿)休息 5-10 分钟再继续；"
                             "无停止标记则原地休息。休息期间血蓝监控照常运行",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8)).pack(anchor="w", pady=(2, 0))

    # ---------- 右侧：预览 + 状态 + 日志 ----------
    def _build_right(self, right):
        card = tk.Frame(right, bg=PANEL, padx=12, pady=10,
                        highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x")
        head = tk.Frame(card, bg=PANEL);
        head.pack(fill="x")
        tk.Label(head, text="📡 实时识别画面", bg=PANEL, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(side="left")
        self.info_label = tk.Label(head, text="", bg=PANEL, fg=TEXT_DIM, font=(MONO, 9))
        self.info_label.pack(side="right")

        self.preview_canvas = tk.Canvas(card, width=self.PREVIEW_W, height=self.PREVIEW_H,
                                        bg="#0a0c12", highlightthickness=1,
                                        highlightbackground=BORDER)
        self.preview_canvas.pack(fill="x", pady=(8, 0))
        self._draw_placeholder()

        res = tk.Frame(card, bg=PANEL);
        res.pack(fill="x", pady=(10, 0))
        bw = int((self.PREVIEW_W - 20) / 3)
        self.hp_bar_w = Bar(res, "HP", HP, width=bw)
        self.hp_bar_w.pack(side="left")
        self.mp_bar_w = Bar(res, "MP", MP, width=bw)
        self.mp_bar_w.pack(side="left", padx=(10, 0))
        self.exp_bar_w = Bar(res, "EXP", "#ffd23e", width=bw)
        self.exp_bar_w.pack(side="left", padx=(10, 0))

        chips = tk.Frame(card, bg=PANEL);
        chips.pack(fill="x", pady=(10, 0))
        self.chip_fps = self._chip(chips, "FPS", "0")
        self.chip_mon = self._chip(chips, "目标", "0")
        self.chip_act = self._chip(chips, "动作", "-")
        self.chip_mode = self._chip(chips, "模式", "待机")

        logcard = tk.Frame(right, bg=PANEL, padx=12, pady=10,
                           highlightthickness=1, highlightbackground=BORDER)
        logcard.pack(fill="both", expand=True, pady=(12, 0))
        tk.Label(logcard, text="📜 运行日志", bg=PANEL, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(anchor="w")
        self.log_text = tk.Text(logcard, bg="#0d0f16", fg="#b9bfd4", relief="flat",
                                font=(MONO, 9), state="disabled")
        ysb = tk.Scrollbar(logcard, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))
        for tag, color in (("info", "#9fb3c8"), ("warn", "#ffc247"),
                           ("error", "#ff6b6b"), ("ok", "#3ddc84")):
            self.log_text.tag_config(tag, foreground=color)

    def _chip(self, parent, name, value):
        f = tk.Frame(parent, bg=PANEL_2, padx=10, pady=5)
        f.pack(side="left", padx=(0, 10))
        tk.Label(f, text=name, bg=PANEL_2, fg=TEXT_DIM, font=(FONT, 8)).pack(side="left")
        lbl = tk.Label(f, text=value, bg=PANEL_2, fg=ACCENT, font=(MONO, 10, "bold"))
        lbl.pack(side="left", padx=(6, 0))
        return lbl

    def _draw_placeholder(self):
        c = self.preview_canvas
        c.delete("all")
        c.create_text(self.PREVIEW_W / 2, self.PREVIEW_H / 2, text="未绑定窗口",
                      fill="#3a4159", font=(FONT, 13))
        c.create_text(self.PREVIEW_W / 2, self.PREVIEW_H / 2 + 26,
                      text="绑定窗口后自动显示实时识别标注画面",
                      fill="#2a3049", font=(FONT, 9))

    # ================= 业务动作 =================
    def refresh_windows(self):
        self.win_combo["values"] = [t for t, _ in WindowCapture.list_windows()]
        saved = self.cfg.get("window_title", "")
        for i, t in enumerate(self.win_combo["values"]):
            if saved and saved.lower() in t.lower():
                self.win_combo.current(i)
                break

    def bind_window(self):
        title = self.win_combo.get()
        if not title:
            messagebox.showwarning("提示", "请先选择游戏窗口", parent=self)
            return
        if self.engine.bind_window(title):
            self.cfg["window_title"] = title
            self.cfg_mgr.save()
            self.engine.set_mode(Mode.PREVIEW)
            self.win_label.config(text=f"✅ {self.engine.capture.window_title}", fg=GREEN)
            self.log(f"窗口绑定成功：{self.engine.capture.window_title}", "ok")
        else:
            self.win_label.config(text="❌ 绑定失败", fg=RED)
            self.log("窗口绑定失败", "error")

    def _grab_frame(self):
        if not self.engine.window_bound():
            messagebox.showwarning("提示", "请先绑定游戏窗口", parent=self)
            return None
            # ⚠ numpy 数组禁止用 or / and / if 判断真值，必须显式 is None
        frame = self.engine.capture.screenshot()  # 优先实时截图（框选需要当前画面）
        if frame is None:
            frame = self.engine.latest_frame()  # 回退：引擎缓存帧
        if frame is None:
            messagebox.showerror("错误", "截图失败：请确认游戏窗口未最小化", parent=self)
        return frame

    def add_monster_template(self):
        frame = self._grab_frame()
        if frame is None:
            return

        def ok(rect):
            x, y, w, h = rect
            img = frame[y:y + h, x:x + w]
            default = self._next_monster_name()
            name = simpledialog.askstring(
                "怪物命名", "怪物名称（用于地图包绑定与日志显示）：",
                initialvalue=default, parent=self)
            name = (name or "").strip() or default
            try:
                path = self._save_monster_img(name, img)
            except Exception as e:
                self.log(f"怪物模板保存失败: {e}", "error")
                return
            self.cfg.setdefault("monster_templates", []).append(
                {"name": name, "path": path})
            self.cfg_mgr.save()
            self.engine.reload_runtime()
            self.refresh_tpl_list()
            where = f"地图包「{self._active_pack()}」" if self._active_pack() \
                else "公共暂存区（存为地图包时并入）"
            self.log(f"怪物模板[{name}]已保存 → {where}", "ok")

        RegionSelector(self, frame, mode="region", on_ok=ok,
                       tip="框选怪物本体（含特征部位，避开血条数字）")

    def _next_monster_name(self):
        names = {t.get("name", "") for t in self.cfg.get("monster_templates", [])}
        i = 1
        while f"怪物{i}" in names:
            i += 1
        return f"怪物{i}"

    def _active_pack(self):
        name = self.cfg.get("patrol", {}).get("current_map", "")
        return name if name in self.maps.list_maps() else ""

    def _save_monster_img(self, name, img):
        """激活地图包 → 存进包内；否则存公共暂存区"""
        pack = self._active_pack()
        if pack:
            return self.maps.add_monster(pack, name, img)
        d = os.path.join(TEMPLATE_DIR, "monsters")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{name}_{int(time.time() * 1000) % 100000}.png")
        if not imwrite_u(p, img):
            raise IOError(p)
        return p

    def add_player_template(self):
        frame = self._grab_frame()
        if frame is None:
            return

        def ok(rect):
            x, y, w, h = rect
            try:
                path = self.maps.save_player(frame[y:y + h, x:x + w])  # 全局唯一
            except Exception as e:
                self.log(f"玩家模板保存失败: {e}", "error")
                return
            self.cfg["player_template"] = {"name": "玩家", "path": path}
            self.cfg_mgr.save()
            self.engine.reload_runtime()
            self.refresh_tpl_list()
            self.log(f"玩家模板已更新（全局共用，所有地图生效）", "ok")

        RegionSelector(self, frame, mode="region", on_ok=ok,
                       tip="框选角色本体（站直、无遮挡、背景简洁处）")

    def clear_player(self):
        self.cfg["player_template"] = None
        if self.maps.player_exists():
            try:
                os.remove(self.maps.player_path)
            except OSError:
                pass
        self.cfg_mgr.save()
        self.engine.reload_runtime()
        self.refresh_tpl_list()
        self.log("玩家模板已清除（全局）", "warn")

    def remove_template(self):
        sel = self.tpl_listbox.curselection()
        if not sel:
            return
        removed = self.cfg["monster_templates"].pop(sel[0])
        self.cfg_mgr.save();
        self.engine.reload_runtime();
        self.refresh_tpl_list()
        self.log(f"已删除模板「{removed['name']}」", "warn")

    def refresh_tpl_list(self):
        self.tpl_listbox.delete(0, "end")
        for item in self.cfg["monster_templates"]:
            self.tpl_listbox.insert("end", f"👾 {item['name']}")
        pt = self.cfg.get("player_template")
        self.player_label.config(
            text=f"玩家模板：{pt['name']}" if pt else "玩家模板：未设置（将原地输出攻击）",
            fg=TEXT if pt else TEXT_DIM)

    def calibrate_bar(self, which):
        frame = self._grab_frame()
        if frame is None:
            return

        def ok(res):
            self.cfg[f"{which}_bar"].update(res)  # 仅 x/y/w/h
            self.cfg_mgr.save()
            self.engine.reload_runtime()
            h = self.engine.calibrate_bar_color(which, frame)
            self.log(f"{which.upper()}条校准完成 区域=({res['x']},{res['y']}) "
                     f"{res['w']}×{res['h']}"
                     + (f" · 实测色相H={h}，区间已自适配" if h is not None
                        else " · 未检出彩色像素，使用内置预设"), "ok")

        RegionSelector(self, frame, mode="bar", on_ok=ok,
                       tip="请在满血/满蓝状态下完整框选整条（红=HP 蓝=MP 黄=EXP，含空槽部分）")

    def calibrate_region(self):
        frame = self._grab_frame()
        if frame is None:
            return

        def ok(rect):
            self.cfg["detect_region"] = list(rect)
            self.cfg_mgr.save()
            self.log(f"检测区域已设置: {rect}", "ok")

        RegionSelector(self, frame, mode="region", on_ok=ok,
                       tip="框选怪物经常出没的区域，可显著提升识别速度")

    # ---------- 巡逻路线 ----------
    def _patrol_cfg(self):
        return self.cfg.setdefault("patrol", {})

    def calibrate_minimap(self):
        frame = self._grab_frame()
        if frame is None:
            return

        def ok(rect):
            x, y, w, h = rect
            p = self._patrol_cfg()
            p["minimap"] = {"x": x, "y": y, "w": w, "h": h}
            self.cfg_mgr.save()
            self.engine.reload_runtime()  # 先让导航拿到新区域
            self.log("正在采样小地图玩家点颜色…", "info")
            frames = self.engine.grab_frames(6, 0.12)  # 多帧对抗黄点闪烁
            color = self.engine.sample_dot_color(frames)
            if color:
                p["player_dot_color"] = color
                self.cfg_mgr.save()
                self.engine.reload_runtime()
            self.refresh_patrol_label()
            self.log(f"小地图校准完成 ({w}×{h})"
                     + (f" · 玩家点颜色已采样 {color}" if color
                        else " · 未检出黄点，使用默认黄色，可稍后重新校准"), "ok")
            self.record_minimap()  # 校准后顺手录制底图

        RegionSelector(self, frame, mode="region", on_ok=ok,
                       tip="框选小地图的【地图区域】（不含标题文字，尽量贴紧边界）")

    # ---------- 地图包 / 颜色路线 ----------
    def refresh_maps(self):
        maps = self.maps.list_maps()
        self.maps_combo["values"] = maps
        cur = self.cfg.get("patrol", {}).get("current_map", "")
        if cur in maps:
            self.maps_combo.set(cur)
        elif maps:
            self.maps_combo.current(0)

    def record_minimap(self):
        mm = self.cfg.get("patrol", {}).get("minimap", {})
        if mm.get("w", 0) < 5:
            messagebox.showwarning("提示", "请先「校准小地图」", parent=self)
            return
        frame = self._grab_frame()
        if frame is None:
            return
        self._minimap_snap = frame[mm["y"]:mm["y"] + mm["h"],
                             mm["x"]:mm["x"] + mm["w"]].copy()
        if self._route_img is not None and \
                self._route_img.shape != self._minimap_snap.shape:
            self._route_img = None  # 尺寸变了，旧路线作废

        self.engine.set_nav_base(self._minimap_snap)
        pack = self._active_pack()
        if pack:
            self.maps.save_minimap(pack, self._minimap_snap)
            self.log(f"小地图底图已录制并写入地图包「{pack}」"
                     f"({mm['w']}×{mm['h']})", "ok")
        else:
            self.log(f"小地图底图已录制 ({mm['w']}×{mm['h']})，可「绘制颜色路线」", "ok")

    def paint_route(self):
        if self._minimap_snap is None:  # 尝试从当前地图包取底图
            name = self.cfg.get("patrol", {}).get("current_map", "")
            if name:
                self._minimap_snap = self.maps.load_minimap(name)
        if self._minimap_snap is None:
            messagebox.showwarning("提示", "请先「录制小地图」", parent=self)
            return

        def ok(route_img):
            self._route_img = route_img
            name = self.cfg.get("patrol", {}).get("current_map", "")
            if name:  # 已关联地图包 → 直接落盘
                rp = os.path.join(self.maps.maps_dir, name, "route.png")
                self.maps.save_route(name, route_img)
                self.cfg.setdefault("patrol", {})["route_path"] = rp
                self.cfg_mgr.save()
                self.engine.load_route(route_img, path_tag=rp)
                if self._minimap_snap is not None:
                    self.engine.set_nav_base(self._minimap_snap)
                self.log(f"路线已保存到地图包「{name}」", "ok")
            else:
                self.engine.load_route(route_img)
                self.log("路线已生效（存为地图包后可持久复用）", "ok")
            self.refresh_patrol_label()

        RoutePainter(self, self._minimap_snap, self._route_img, on_ok=ok)

    def save_map_pack(self):
        name = self.maps_combo.get().strip()
        if not name:
            name = (simpledialog.askstring("地图包命名", "地图名称（如：蘑菇山）：",
                                           parent=self) or "").strip()
        if not name:
            return
        if name in self.maps.list_maps() and not messagebox.askyesno(
                "覆盖确认", f"地图包「{name}」已存在，覆盖保存？", parent=self):
            return
        mm = self.cfg.get("patrol", {}).get("minimap", {})
        if mm.get("w", 0) < 5:
            messagebox.showwarning("提示", "请先「🧭 校准小地图」再保存地图包",
                                   parent=self)
            return
        try:
            self.maps.save(name, self.cfg, self._minimap_snap, self._route_img,
                           grab_fn=lambda: self._grab_frame())  # 缺底图自动补拍
        except Exception as e:
            self.log(f"地图包保存失败: {e}", "error")
            messagebox.showerror("错误", f"地图包保存失败：{e}", parent=self)
            return
        # 回填：底图可能被自动补拍；路线可能来自包内旧图
        self._minimap_snap = self.maps.load_minimap(name) or self._minimap_snap
        if self._route_img is None:
            self._route_img = self.maps.load_route(name)
        if self._minimap_snap is not None:
            self.engine.set_nav_base(self._minimap_snap)
        p = self.cfg.setdefault("patrol", {})
        p["current_map"] = name
        p["route_path"] = os.path.join(self.maps.maps_dir, name, "route.png")
        p["enabled"] = True
        self.cfg_mgr.save()
        self.engine.reload_runtime()
        self.refresh_maps()
        self.refresh_patrol_label()
        self.patrol_var.set(True)
        self.log(f"🗺 地图包「{name}」已保存：绑定怪物 "
                 f"{len(self.cfg.get('monster_templates', []))} 个 · "
                 f"底图{'✓' if self._minimap_snap is not None else '✗(未绑定窗口无法补拍)'} · "
                 f"路线{'✓' if self._route_img is not None else '✗'}，巡逻已启用", "ok")

    def load_map_pack(self):
        name = self.maps_combo.get()
        if not name:
            messagebox.showwarning("提示", "请先选择地图包", parent=self)
            return
        self.engine.invalidate_route_cache()
        ok, missing = self.maps.load(name, self.cfg)
        if not ok:
            messagebox.showerror("错误", "地图包加载失败（profile.json 缺失）",
                                 parent=self)
            return
        self._minimap_snap = self.maps.load_minimap(name)
        self._route_img = self.maps.load_route(name)
        # 自愈：底图缺失 → 窗口已绑定且 ROI 有效则现场补拍写回包
        if self._minimap_snap is None:
            mm = self.cfg.get("patrol", {}).get("minimap", {})
            frame = self._grab_frame() if mm.get("w", 0) > 4 else None
            if frame is not None:
                self._minimap_snap = frame[mm["y"]:mm["y"] + mm["h"],
                                     mm["x"]:mm["x"] + mm["w"]].copy()
                self.maps.save_minimap(name, self._minimap_snap)
                self.log("小地图底图缺失，已现场自动补拍并写入地图包", "ok")
            else:
                self.log("底图缺失且无法自动补拍（未绑定窗口？），请绑定后"
                         "点「📷 录制小地图」再「💾 保存」", "warn")
        self.cfg_mgr.save()
        self.engine.reload_runtime()
        if self._minimap_snap is not None:
            self.engine.set_nav_base(self._minimap_snap)
        # 同步 UI 控件
        for k, ent in self._key_entries.items():
            ent.var.set(self.cfg["keys"].get(k, "-"))
        self.patrol_var.set(self.cfg.get("patrol", {}).get("enabled", False))
        self.refresh_tpl_list()
        self.refresh_maps()
        self.refresh_patrol_label()
        self.log(f"📦 地图包「{name}」已加载：绑定怪物 "
                 f"{len(self.cfg.get('monster_templates', []))} 个 · "
                 f"底图{'✓' if self._minimap_snap is not None else '✗'} · "
                 f"路线{'✓' if self._route_img is not None else '✗'} · "
                 f"玩家模板{'✓' if self.cfg.get('player_template') else '✗'}", "ok")
        if self._route_img is None:
            self.log("路线图缺失：请「🎨 绘制路线」，画完自动存入本地图包", "warn")
        # 自动绑定窗口
        wt = self.cfg.get("window_title", "")
        if wt and not self.engine.window_bound():
            if self.engine.bind_window(wt):
                self.engine.set_mode(Mode.PREVIEW)
                self.win_label.config(text=f"✅ {self.engine.capture.window_title}",
                                      fg=GREEN)

    def delete_map_pack(self):
        name = self.maps_combo.get()
        if not name or not messagebox.askyesno(
                "删除确认", f"确定删除地图包「{name}」？", parent=self):
            return
        self.maps.delete(name)
        if self.cfg.get("patrol", {}).get("current_map") == name:
            self.cfg["patrol"]["current_map"] = ""
            self.cfg["patrol"]["route_path"] = ""
            self.cfg_mgr.save()
            self.engine.reload_runtime()
        self.refresh_maps()
        self.refresh_patrol_label()
        self.log(f"地图包「{name}」已删除", "warn")

    def refresh_patrol_label(self):
        p = self.cfg.get("patrol", {})
        mm = p.get("minimap", {})
        parts = [f"地图: {p.get('current_map') or '未关联'}",
                 "小地图 ✓" if mm.get("w", 0) > 4 else "小地图 ✗",
                 "路线 ✓" if self.engine.route_nav.ready else "路线 ✗",
                 "已启用" if p.get("enabled") else "未启用"]
        ok = mm.get("w", 0) > 4 and self.engine.route_nav.ready and p.get("enabled")
        self.patrol_label.config(text=" · ".join(parts),
                                 fg=TEXT if ok else TEXT_DIM)

    def _toggle_patrol(self, var):
        self._patrol_cfg()["enabled"] = var.get()
        self.cfg_mgr.save()
        self.engine.reload_runtime()
        self.refresh_patrol_label()
        self.log(f"路线巡逻 {'启用' if var.get() else '停用'}", "info")

    def clear_region(self):
        self.cfg["detect_region"] = None
        self.cfg_mgr.save()
        self.log("检测区域已恢复全屏", "info")

    def _set_key(self, key, value):
        v = "" if (not value or value == "-") else value
        self.cfg["keys"][key] = v
        self.cfg_mgr.save()
        self.log(f"按键 [{key}] → {v or '(空)'}", "info")

    def start_bot(self, _e=None):
        if not self.engine.window_bound():
            messagebox.showwarning("提示", "请先绑定游戏窗口", parent=self);
            return
        if not self.cfg["monster_templates"]:
            messagebox.showwarning("提示", "请至少框选一个怪物模板", parent=self);
            return
        if self.cfg.get("patrol", {}).get("enabled") and not self.engine.route_nav.ready:
            self.log("⚠ 已勾选巡逻但颜色路线未就绪（校准小地图→绘制路线），将退回左右找怪", "warn")
        if not self.cfg["hp_bar"].get("w"):
            self.log("⚠ 尚未校准HP条，血量监控将不可用", "warn")
        self.cfg_mgr.save();
        self.engine.reload_runtime()
        self.engine.set_mode(Mode.RUNNING)
        self.log("🚀 挂机启动！按键将发送到游戏窗口，请勿最小化游戏", "ok")

    def stop_bot(self, _e=None):
        self.engine.set_mode(Mode.PREVIEW)
        self.log("⏹ 已停止战斗，保持监控", "warn")

    def toggle_run(self, _e=None):
        self.stop_bot() if self.engine.mode == Mode.RUNNING else self.start_bot()

    def toggle_pause(self, _e=None):
        if self.engine.mode == Mode.RUNNING:
            self.engine.set_mode(Mode.PAUSED);
            self.log("已暂停", "warn")
        elif self.engine.mode == Mode.PAUSED:
            self.engine.set_mode(Mode.RUNNING);
            self.log("恢复运行", "ok")

    def log(self, msg, lv="info"):
        self.log_queue.put((time.strftime("%H:%M:%S"), msg, lv))

    # ================= 轮询刷新 =================
    def _poll_status(self):
        ann = None
        try:
            ann = self.engine.preview_queue.get_nowait()
        except queue.Empty:
            pass
        if ann is not None:
            self._show_preview(ann)

        st = self.engine.status
        self.hp_bar_w.set(st["hp"]);
        self.mp_bar_w.set(st["mp"])
        self.exp_bar_w.set(st["exp"])
        self.chip_fps.config(text=str(st["fps"]))
        self.chip_mon.config(text=str(st["monsters"]))
        self.chip_act.config(text=str(st["action"]))
        self.chip_mode.config(text={"idle": "待机", "preview": "监控",
                                    "running": "运行", "paused": "暂停"}[st["mode"]])
        conf = {"idle": ("待机", "#6b7280"), "preview": ("监控中", ACCENT),
                "running": ("运行中", GREEN), "paused": ("已暂停", "#ffb020")}[st["mode"]]
        if self._pill_state != conf:
            self._pill_state = conf
            self.pill_dot.delete("all")
            self.pill_dot.create_oval(1, 1, 9, 9, fill=conf[1], outline="")
            self.pill_lbl.config(text=conf[0], fg=conf[1])
        if self.engine.window_bound():
            w, h = self.engine.capture.size
            self.info_label.config(text=f"{w}×{h}px · 模板 {len(self.engine.detector.templates)} 个")
        self.after(80, self._poll_status)

    def _show_preview(self, ann):
        img = cv2.cvtColor(ann, cv2.COLOR_BGR2RGB)
        h, w = img.shape[:2]
        s = min(self.PREVIEW_W / w, self.PREVIEW_H / h)
        nw, nh = max(1, int(w * s)), max(1, int(h * s))
        img = cv2.resize(img, (nw, nh))
        self._pv_photo = ImageTk.PhotoImage(Image.fromarray(img))
        c = self.preview_canvas
        c.delete("all")
        c.create_rectangle(0, 0, self.PREVIEW_W, self.PREVIEW_H, fill="#0a0c12", outline="")
        c.create_image((self.PREVIEW_W - nw) // 2, (self.PREVIEW_H - nh) // 2,
                       anchor="nw", image=self._pv_photo)

    def _poll_log(self):
        changed = False
        while True:
            try:
                ts, msg, lv = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.config(state="normal")
            self.log_text.insert("end", f"[{ts}] ", lv)
            self.log_text.insert("end", f"{msg}\n", lv)
            self.log_text.see("end")
            changed = True
        if changed:
            self.log_text.config(state="disabled")
        self.after(250, self._poll_log)

    def _on_close(self):
        self.engine.shutdown()
        self.cfg_mgr.save()
        self.destroy()
