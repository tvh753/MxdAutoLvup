# -*- coding: utf-8 -*-
# @Time    : 26/8/26 20:03
# @Author  : yy
# @File    : main_window.py
# @Software: MxdAutoLvup

"""枫叶挂机控制台 · 主界面（App 类，tkinter 单窗口）

布局概览：
  ┌──────────────────────────────────────────────────┐
  │  标题栏 · 状态胶囊（待机/监控/运行/暂停）           │
  ├──────────────────────────┬───────────────────────┤
  │ 左侧 Notebook 三页签      │ 右侧：实时识别预览画面  │
  │  🎯 目标   ⌨ 按键   ⚙ 参数│      HP/MP/EXP 进度条  │
  │  底部：启动/停止/暂停按钮  │      FPS/目标/动作/模式 │
  │                          │      运行日志          │
  └──────────────────────────┴───────────────────────┘

线程模型（关键！）：
  tkinter 主线程（本类）只负责界面刷新与事件回调；
  引擎 BotEngine 是独立后台线程。
  数据单向流动：引擎 →(queue)→ GUI（预览帧 / 日志），
  GUI →(方法调用)→ 引擎（配置 / 模式切换）。
  因此界面永远流畅，不会因为截图/识别耗时而卡住。

刷新机制：after() 轮询（80ms 状态 + 250ms 日志），
不阻塞主循环，两个队列空了立即返回。
"""
import os, time, queue
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
import cv2
from PIL import Image, ImageTk

from core.hotkey_manager import HotkeyManager
from gui.theme import *
from gui.widgets import NeoButton, Bar, KeyEntry, ScrollFrame
from gui.region_selector import RegionSelector
# from gui.route_editor import RouteEditor
# from core.config_manager import ConfigManager, TEMPLATE_DIR, ROOT
from core.bot_engine import BotEngine, Mode
from core.window_capture import WindowCapture, VncCapture, FastWindowCapture
from gui.route_painter import RoutePainter
from core.map_manager import MapManager
from core.config_manager import ConfigManager, TEMPLATE_DIR, ROOT
from core.imio import imread_u, imwrite_u
from core.map_config import (
    load_map_entries, find_by_name, find_by_package, MapConfigError,
)

class App(tk.Tk):
    """枫叶挂机控制台主窗口（tk.Tk 单例）

    职责：组织界面 → 转发用户操作到引擎 → 轮询刷新引擎状态。
    状态来源全部是 self.engine.status 与 self.engine.preview_queue，
    不做任何重复识别计算。
    """
    PREVIEW_W, PREVIEW_H = 760, 430  # 预览画布尺寸（px）
    KEY_ROWS = [  # 按键页签：显示名 ↔ config["keys"] 的键名
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

        # 配置（ConfigManager 持有 cfg 引用，GUI/引擎共用同一份）
        self.cfg_mgr = ConfigManager()
        self.cfg = self.cfg_mgr.cfg
        self.log_queue = queue.Queue()      # GUI 侧日志队列
        self._pv_photo = None               # 预览图缓存（防被 GC 回收）
        self._pill_state = None             # 状态胶囊缓存（变化才重绘）

        # 引擎：日志回调包装成 (时间戳, 消息, 级别) 入队，GUI 轮询取出
        def _log_fn(m, lv="info"):
            # print(f"[{time.strftime('%H:%M:%S')}][{lv}] {m}")
            self.log_queue.put((time.strftime("%H:%M:%S"), m, lv))

        self.engine = BotEngine(self.cfg, _log_fn)

        self.maps = MapManager(ROOT)
        self._map_entries = []      # config_map.yaml 缓存
        self._minimap_snap = None   # 当前小地图底图（录制）
        self._route_imgs = []
        self._route_img = None

        # v27: 多路线 —— _route_imgs 是列表（长度 ≥1），
        #       _route_img 保留作为"当前/最后一条"，兼容部分旧代码
        self._route_imgs = []
        self._route_img = None
        self.engine.start()  # 后台线程立刻启动（IDLE 模式空转等待）

        # 热键管理器需在 _build_layout 之前初始化（UI 会引用）
        self.hotkey_mgr = HotkeyManager(self)

        # 小地图录制
        self._recorder = None
        self._rec_dialog = None
        self._recorder_pack = ""
        self._recorder_target_w = 0

        self._build_style()
        self._build_layout()
        self._poll_status()   # 启动状态轮询
        self._poll_log()      # 启动日志轮询
        # self.bind("<F8>", self.toggle_run)
        # self.bind("<F9>", self.toggle_pause)
        # self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_hotkeys()
        self.protocol("WM_CLOSE_WINDOW", self._on_close)
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
        # tk.Label(left, text="F8 启动/停止 · F9 暂停/恢复（控制台聚焦时生效）",
        #          bg=PANEL, fg=TEXT_DIM, font=(FONT, 8)).pack(pady=(0, 8))
        self._hotkey_label = tk.Label(left, text="", bg=PANEL, fg=TEXT_DIM, font=(FONT, 8))
        self._hotkey_label.pack(pady=(0, 8))
        self._update_hotkey_label()

    def _build_target_tab(self, tab):
        # ============ 抓屏模式选择 ============
        mode_box, mode_body = self._section(tab, "🖥 抓屏模式")
        mode_box.pack(fill="x", padx=8, pady=(8, 4))
        self.capture_mode_var = tk.StringVar(
            value=self.cfg.get("capture_mode", "window"))

        def _on_mode_change():
            mode = self.capture_mode_var.get()
            self.cfg["capture_mode"] = mode
            self.cfg_mgr.save()
            # ★ 关键：通知引擎重建 capture / controller / move 三元组，
            #   否则切回 window 模式时还挂着 VncCapture，点"绑定"会误走 VNC。
            try:
                self.engine.switch_capture_mode(mode)
            except Exception as e:
                self.log(f"切换抓屏模式失败: {e}", "error")
            self._refresh_capture_ui()
            # 断开旧连接 & 重置状态标签
            if mode == "window":
                self.win_label.config(text="未绑定", fg=TEXT_DIM)
                self.log("抓屏模式：本机游戏（窗口句柄）", "info")
            else:
                self.vnc_label.config(text="未连接", fg=TEXT_DIM)
                self.log("抓屏模式：虚拟机游戏（VNC）", "info")

        mr = tk.Frame(mode_body, bg=PANEL_2); mr.pack(fill="x")
        tk.Radiobutton(mr, text="🖥 本机游戏", variable=self.capture_mode_var,
                       value="window", bg=PANEL_2, fg=TEXT, selectcolor="#141722",
                       activebackground=PANEL_2, activeforeground=TEXT,
                       font=(FONT, 9), command=_on_mode_change).pack(side="left")
        tk.Radiobutton(mr, text="☁ 虚拟机游戏(VNC)", variable=self.capture_mode_var,
                       value="vnc", bg=PANEL_2, fg=TEXT, selectcolor="#141722",
                       activebackground=PANEL_2, activeforeground=TEXT,
                       font=(FONT, 9), command=_on_mode_change).pack(
            side="left", padx=(14, 0))

        # ============ 切换容器（用 grid 叠放两个面板）============
        # 关键：两个 box 放在同一个 grid cell，切换时用 grid_remove/grid，
        # 比 pack/pack_forget 在 ScrollFrame 内更稳定，不会出现"框了但看不见"。
        bind_holder = tk.Frame(tab, bg=PANEL)
        bind_holder.pack(fill="x", padx=0, pady=0)
        bind_holder.columnconfigure(0, weight=1)

        # ---- 本机模式：窗口绑定 ----
        self.win_bind_box, body = self._section(bind_holder, "🪟 窗口绑定")
        self.win_bind_box.grid(row=0, column=0, sticky="ew", padx=8, pady=(4, 4))
        # 第一行：ComboBox + 刷新按钮
        #   ComboBox 用 expand=True 吃掉剩余宽度，⟳ 按钮固定在右侧
        row1 = tk.Frame(body, bg=PANEL_2)
        row1.pack(fill="x")
        self.win_combo = ttk.Combobox(row1, state="readonly")
        self.win_combo.pack(side="left", fill="x", expand=True)
        NeoButton(row1, "⟳", command=self.refresh_windows, bg=PANEL, fg=TEXT,
                  padx=9).pack(side="left", padx=(6, 0))
        # 第二行：绑定 / 解绑
        #   拆分到下一行避免横向控件过挤
        row2 = tk.Frame(body, bg=PANEL_2)
        row2.pack(fill="x", pady=(6, 0))
        NeoButton(row2, "绑定", command=self.bind_window, padx=14).pack(side="left")
        NeoButton(row2, "解绑", command=self.unbind_window,
                  bg="#3a3f55", fg=TEXT, padx=14).pack(side="left", padx=(6, 0))

        self.win_label = tk.Label(body, text="未绑定", fg=TEXT_DIM,
                                  bg=PANEL_2, font=(FONT, 9))
        self.win_label.pack(anchor="w", pady=(4, 0))
        self.refresh_windows()

        # ---- VNC 模式：连接配置 ----
        self.vnc_bind_box, vnc_body = self._section(bind_holder,
                                                    "☁ VNC 连接（虚拟机）")
        self.vnc_bind_box.grid(row=0, column=0, sticky="ew", padx=8, pady=(4, 4))
        vnc_cfg = self.cfg.setdefault("vnc", {})
        r1 = tk.Frame(vnc_body, bg=PANEL_2); r1.pack(fill="x", pady=2)
        tk.Label(r1, text="主机IP", bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                 width=8, anchor="w").pack(side="left")
        self.vnc_host_entry = tk.Entry(r1, width=16, font=(MONO, 10))
        self.vnc_host_entry.insert(0, vnc_cfg.get("host", "127.0.0.1"))
        self.vnc_host_entry.pack(side="left")
        self.vnc_host_entry.bind("<KeyRelease>", lambda e: self._save_vnc_cfg())

        r2 = tk.Frame(vnc_body, bg=PANEL_2); r2.pack(fill="x", pady=2)
        tk.Label(r2, text="端口", bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                 width=8, anchor="w").pack(side="left")
        self.vnc_port_entry = tk.Entry(r2, width=16, font=(MONO, 10))
        self.vnc_port_entry.insert(0, str(vnc_cfg.get("port", 5900)))
        self.vnc_port_entry.pack(side="left")
        self.vnc_port_entry.bind("<KeyRelease>", lambda e: self._save_vnc_cfg())

        r3 = tk.Frame(vnc_body, bg=PANEL_2); r3.pack(fill="x", pady=2)
        tk.Label(r3, text="密码", bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                 width=8, anchor="w").pack(side="left")
        self.vnc_pwd_entry = tk.Entry(r3, width=16, font=(MONO, 10))
        self.vnc_pwd_entry.insert(0, vnc_cfg.get("password", ""))
        self.vnc_pwd_entry.pack(side="left")
        self.vnc_pwd_entry.bind("<KeyRelease>", lambda e: self._save_vnc_cfg())
        tk.Label(vnc_body, text="（无密码留空即可）", fg=TEXT_DIM, bg=PANEL_2,
                 font=(FONT, 8)).pack(anchor="w")

        r4 = tk.Frame(vnc_body, bg=PANEL_2); r4.pack(fill="x", pady=(6, 0))
        NeoButton(r4, "🔌 连接 VNC", command=self.bind_vnc, padx=12).pack(side="left")
        NeoButton(r4, "🔌 断开", command=self.unbind_vnc, bg="#3a3f55",
                  fg=TEXT, padx=10).pack(side="left", padx=(6, 0))
        self.vnc_label = tk.Label(vnc_body, text="未连接", fg=TEXT_DIM,
                                  bg=PANEL_2, font=(FONT, 9))
        self.vnc_label.pack(anchor="w", pady=(4, 0))
        tk.Label(vnc_body,
                 text="多虚拟机预留：后续多线程多开时，可在 config.json 里配 vnc.multi",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8)).pack(anchor="w", pady=(2, 0))

        # 首次渲染：根据配置显示对应面板
        self._refresh_capture_ui(force=True)

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
        # 第一行：ComboBox + 刷新 + 新增
        row = tk.Frame(body, bg=PANEL_2)
        row.pack(fill="x")
        self.maps_combo = ttk.Combobox(row, state="readonly")
        self.maps_combo.pack(side="left", fill="x", expand=True)
        NeoButton(row, "⟳", command=self.refresh_maps, bg=PANEL, fg=TEXT,
                  padx=9, font=(FONT, 9)).pack(side="left", padx=(3, 0))
        NeoButton(row, "新增", command=self.create_map_pack, padx=9,
                  font=(FONT, 9), bg="#2f6f8f", fg=TEXT).pack(side="left", padx=(3, 0))
        # 第二行：加载 + 保存 + 删
        row2 = tk.Frame(body, bg=PANEL_2)
        row2.pack(fill="x", pady=(6, 0))
        NeoButton(row2, "加载", command=self.load_map_pack, padx=14,
                  font=(FONT, 9)).pack(side="left")
        NeoButton(row2, "保存", command=self.save_map_pack, padx=14,
                  font=(FONT, 9), bg="#2f6f4f", fg=TEXT).pack(side="left", padx=(6, 0))
        NeoButton(row2, "删除", command=self.delete_map_pack, padx=14,
                  font=(FONT, 9), bg="#3a3f55", fg=TEXT).pack(side="left", padx=(6, 0))
        row2 = tk.Frame(body, bg=PANEL_2); row2.pack(fill="x", pady=(5, 0))
        NeoButton(row2, "🧭 校准小地图", command=self.calibrate_minimap,
                  padx=8, font=(FONT, 9)).pack(side="left")
        NeoButton(row2, "📷 录制小地图", command=self.record_minimap,
                  padx=8, font=(FONT, 9)).pack(side="left", padx=(4, 0))
        row3 = tk.Frame(body, bg=PANEL_2); row3.pack(fill="x", pady=(5, 0))
        NeoButton(row3, "🎨 绘制路线", command=self.paint_route, padx=8,
                  font=(FONT, 9), bg="#2f6f4f", fg=TEXT).pack(side="left")
        # v27: 新增 —— 追加一条路线（不清空已有的）
        NeoButton(row3, "➕ 新增路线", command=self.paint_route_new, padx=8,
                  font=(FONT, 9), bg="#2f6f4f", fg=TEXT).pack(side="left", padx=(4, 0))
        # v27: 显示当前路线数
        self.route_count_label = tk.Label(row3, text="路线: 0 条",
                                          bg=PANEL_2, fg=TEXT_DIM,
                                          font=(FONT, 8))
        self.route_count_label.pack(side="left", padx=(8, 0))
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
        # ============ 地图坐标偏移微调 ============
        # 作用：小地图 → map 换算后，再叠加一个固定偏移
        #   · X 正值 → 位置右移；负值 → 左移
        #   · Y 正值 → 位置下移；负值 → 上移
        # 每次改动立即写入 config 并热重载 route_nav（实时生效，便于调试）
        row_off = tk.Frame(body, bg=PANEL_2)
        row_off.pack(fill="x", pady=(5, 0))
        tk.Label(row_off, text="坐标偏移", bg=PANEL_2, fg=TEXT,
                 font=(FONT, 9)).pack(side="left")

        p_off = self.cfg.setdefault("patrol", {})
        cur_off = p_off.get("map_offset", [0, 0])
        if not isinstance(cur_off, list) or len(cur_off) != 2:
            cur_off = [0, 0]

        self._off_x_entry = tk.Entry(row_off, width=6, font=(MONO, 10))
        self._off_x_entry.insert(0, str(cur_off[0]))
        self._off_x_entry.pack(side="left", padx=(4, 0))
        self._off_x_entry.bind("<KeyRelease>",
                               lambda e: self._apply_map_offset())

        tk.Label(row_off, text="X ", bg=PANEL_2, fg=TEXT_DIM,
                 font=(FONT, 8)).pack(side="left")

        self._off_y_entry = tk.Entry(row_off, width=6, font=(MONO, 10))
        self._off_y_entry.insert(0, str(cur_off[1]))
        self._off_y_entry.pack(side="left")
        self._off_y_entry.bind("<KeyRelease>",
                               lambda e: self._apply_map_offset())

        tk.Label(row_off, text="Y   (正=右下 负=左上)",
                 bg=PANEL_2, fg=TEXT_DIM, font=(FONT, 8)).pack(
            side="left", padx=(4, 0))
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

        # v20: 攻击方式选择（普攻 / 技能随机）
        box2, body2 = self._section(tab, "⚔ 攻击方式")
        box2.pack(fill="x", padx=8, pady=(0, 8))
        row = tk.Frame(body2, bg=PANEL_2)
        row.pack(fill="x")
        tk.Label(row, text="攻击方式", bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                 width=8, anchor="w").pack(side="left")
        self.attack_mode_var = tk.StringVar(
            value=self.cfg["keys"].get("attack_mode", "normal"))
        self.attack_mode_combo = ttk.Combobox(
            row, state="readonly", width=12,
            values=("普通攻击", "技能1-3随机"),
            textvariable=self.attack_mode_var)
        self.attack_mode_combo.pack(side="left")
        # 显示值→实际值映射
        self.attack_mode_map = {"普通攻击": "normal", "技能1-3随机": "skill"}
        self.attack_mode_var.set(
            "普通攻击" if self.attack_mode_var.get() == "normal" else "技能1-3随机")
        self.attack_mode_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: (self.cfg["keys"].__setitem__(
                "attack_mode", self.attack_mode_map.get(
                    self.attack_mode_var.get(), "normal")),
                self.cfg_mgr.save()))
        tk.Label(body2, text="选「普通攻击」使用攻击键；选「技能1-3随机」从已配置的技能键中随机选用",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8)).pack(anchor="w", pady=(4, 0))

        box3, body3 = self._section(tab, "⌨ 控制台热键")
        box3.pack(fill="x", padx=8, pady=(0, 8))
        hk_cfg = self.cfg.setdefault("hotkeys", {})
        r1 = tk.Frame(body3, bg=PANEL_2);
        r1.pack(fill="x", pady=2)
        tk.Label(r1, text="启动/停止", bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                 width=10, anchor="w").pack(side="left")
        self._hk_start_entry = KeyEntry(r1, value=hk_cfg.get("start_stop", "F8"),
                                        on_change=lambda v: self._set_hotkey("start_stop", v))
        self._hk_start_entry.pack(side="left")
        r2 = tk.Frame(body3, bg=PANEL_2)
        r2.pack(fill="x", pady=2)
        tk.Label(r2, text="暂停/继续", bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                 width=10, anchor="w").pack(side="left")
        self._hk_pause_entry = KeyEntry(r2, value=hk_cfg.get("pause_resume", "F9"),
                                        on_change=lambda v: self._set_hotkey("pause_resume", v))
        self._hk_pause_entry.pack(side="left")
        # ★ 启用全局热键的勾选框
        #   要点：Checkbutton 不支持自动换行，长文本会被容器裁掉导致
        #   勾选框都看不见。加 wraplength 让文本自动折行到第二行，
        #   保证勾选框完整显示。
        self._hk_global_var = tk.BooleanVar(value=hk_cfg.get("global_enabled", False))
        tk.Checkbutton(
            body3,
            text="启用全局热键（任意窗口焦点下生效，需 pynput 库）",
            variable=self._hk_global_var,
            bg=PANEL_2, fg=TEXT, selectcolor="#141722",
            activebackground=PANEL_2, activeforeground=TEXT,
            font=(FONT, 9),
            anchor="w", justify="left",
            wraplength=270,          # ← 关键：让文字自动折行
            command=lambda: self._set_hotkey_global(),
        ).pack(fill="x", pady=4)
        tk.Label(body3, text="点击输入框→按键绑定；支持 F1-F12 或组合键如 ctrl+f8",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8),
                 wraplength=270, justify="left").pack(anchor="w", pady=(4, 0))
        # ============ 定时按键（宠物药 + 5 BUFF）============
        box4, body4 = self._section(tab, "🐾 定时按键（宠物药 / BUFF）")
        box4.pack(fill="x", padx=8, pady=(0, 8))

        # 每行：[标签] [按键输入] [间隔输入] 秒
        timer_rows = [
            ("宠物药", "pet_potion", 600),
            ("BUFF1", "buff1", 180),
            ("BUFF2", "buff2", 180),
            ("BUFF3", "buff3", 180),
            ("BUFF4", "buff4", 180),
            ("BUFF5", "buff5", 180),
        ]
        self._timer_entries = {}
        timers_cfg = self.cfg.setdefault("timers", {})
        for label, key, default in timer_rows:
            r = tk.Frame(body4, bg=PANEL_2)
            r.pack(fill="x", pady=3)
            tk.Label(r, text=label, bg=PANEL_2, fg=TEXT, font=(FONT, 9),
                     width=6, anchor="w").pack(side="left")

            # 按键输入（复用 KeyEntry，自动处理键盘捕获）
            ent_key = KeyEntry(r, value=self.cfg["keys"].get(key, ""),
                               on_change=lambda v, k=key: self._set_key(k, v))
            ent_key.pack(side="left")

            # 间隔输入
            tk.Label(r, text="  间隔", bg=PANEL_2, fg=TEXT_DIM,
                     font=(FONT, 9)).pack(side="left")
            ent_itv = tk.Entry(r, width=6, font=(MONO, 10))
            ent_itv.insert(0, str(timers_cfg.get(key, default)))
            ent_itv.pack(side="left")
            ent_itv.bind("<KeyRelease>",
                         lambda e, k=key, w=ent_itv: self._set_timer(k, w))
            tk.Label(r, text="秒", bg=PANEL_2, fg=TEXT_DIM,
                     font=(FONT, 9)).pack(side="left")

        tk.Label(body4, text="按键留空 = 该功能禁用；间隔 ≥ 1 秒",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8)).pack(anchor="w", pady=(6, 0))

        # ============ 保存按键配置到地图包 ============
        box_save, body_save = self._section(tab, "💾 保存按键到地图包")
        box_save.pack(fill="x", padx=8, pady=(0, 8))

        NeoButton(body_save, "💾 写入当前地图包",
                  command=self.save_keys_to_pack,
                  bg="#2f6f5f", fg=TEXT).pack(anchor="w")
        tk.Label(body_save,
                 text="把当前面板的按键 + 攻击方式 + 定时按键，写入 "
                      "maps/<地图包>/profile.json，加载地图包时自动恢复",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8),
                 wraplength=270, justify="left").pack(anchor="w", pady=(4, 0))



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
        slider("攻击框底偏移", -60, 120, "attack_bottom_offset")
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
                           ("loot_enabled", "边走边自动拾取（需配置拾取按键）"),
                           ("preview_enabled", "显示实时识别画面（关闭可省 CPU，多开建议关）"),
                           ("nav_panel_enabled", "显示 NAV 小地图面板（关闭可再省一点）")]:
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
            row = tk.Frame(body3, bg=PANEL_2)
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
        pslider("搜索半径", 5, 25, "search_range", 10)
        pslider("抓绳容差", 2, 10, "grab_tol", 4)
        pslider("玩家点面积", 12, 120, "dot_max_area", 40)
        pslider("追击时限s", 1.0, 10.0, "max_chase_time", 3.0, "{:.1f}")

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

        # ===== 定时下线/上线 =====
        box5, body5 = self._section(tab, "🔔 定时下线 / 上线")
        box5.pack(fill="x", padx=8, pady=(0, 8))
        sch = self.cfg.setdefault("schedule", {})

        # 下线设置
        row_l = tk.Frame(body5, bg=PANEL_2); row_l.pack(fill="x", pady=2)
        lout = tk.BooleanVar(value=sch.get("logout_enabled", False))
        tk.Checkbutton(row_l, text="定时下线", variable=lout, bg=PANEL_2, fg=TEXT,
                       selectcolor="#141722", activebackground=PANEL_2,
                       activeforeground=TEXT, font=(FONT, 9), anchor="w",
                       command=lambda: (sch.__setitem__("logout_enabled", lout.get()),
                                        self.cfg_mgr.save())).pack(side="left")
        tk.Label(row_l, text="时间", bg=PANEL_2, fg=TEXT, font=(FONT, 9)).pack(side="left", padx=(8, 2))
        lout_time = tk.Entry(row_l, width=6, font=(MONO, 10))
        lout_time.insert(0, sch.get("logout_time", "23:00"))
        lout_time.pack(side="left")
        lout_time.bind("<KeyRelease>", lambda e: (sch.__setitem__("logout_time", lout_time.get()),
                                                  self.cfg_mgr.save()))
        tk.Label(row_l, text="(到点+随机3~8分，esc→↑→enter)",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8)).pack(side="left", padx=6)

        # 上线设置
        row_i = tk.Frame(body5, bg=PANEL_2); row_i.pack(fill="x", pady=2)
        lin = tk.BooleanVar(value=sch.get("login_enabled", False))
        tk.Checkbutton(row_i, text="定时上线", variable=lin, bg=PANEL_2, fg=TEXT,
                       selectcolor="#141722", activebackground=PANEL_2,
                       activeforeground=TEXT, font=(FONT, 9), anchor="w",
                       command=lambda: (sch.__setitem__("login_enabled", lin.get()),
                                        self.cfg_mgr.save())).pack(side="left")
        tk.Label(row_i, text="时间", bg=PANEL_2, fg=TEXT, font=(FONT, 9)).pack(side="left", padx=(8, 2))
        lin_time = tk.Entry(row_i, width=6, font=(MONO, 10))
        lin_time.insert(0, sch.get("login_time", "08:00"))
        lin_time.pack(side="left")
        lin_time.bind("<KeyRelease>", lambda e: (sch.__setitem__("login_time", lin_time.get()),
                                                 self.cfg_mgr.save()))
        tk.Label(row_i, text="(到点+随机3~8分，按enter，识别到玩家后开始)",
                 fg=TEXT_DIM, bg=PANEL_2, font=(FONT, 8)).pack(side="left", padx=6)

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
        """刷新窗口下拉列表，并默认选中上次绑定的窗口"""
        self.win_combo["values"] = [t for t, _ in WindowCapture.list_windows()]
        saved = self.cfg.get("window_title", "")
        for i, t in enumerate(self.win_combo["values"]):
            if saved and saved.lower() in t.lower():
                self.win_combo.current(i)
                break

    def create_map_pack(self):
        """创建空地图包（同时写入 config_map.yaml，下拉立刻可见）"""
        name = (simpledialog.askstring(
            "新建地图包",
            "输入地图包名称（字母/数字/下划线/中文）：\n"
            "创建后需手动把 map.png 放到该目录",
            parent=self) or "").strip()
        if not name:
            return
        import re
        if not re.match(r'^[\w\u4e00-\u9fa5]+$', name):
            messagebox.showerror("错误",
                                 "名称只能含字母、数字、下划线、中文", parent=self)
            return

        try:
            ok = self.maps.create(name)     # ★ create 内部已写 yaml
        except Exception as e:
            messagebox.showerror("错误", f"创建失败: {e}", parent=self)
            return
        if not ok:
            messagebox.showwarning("提示", f"地图包「{name}」已存在", parent=self)
            return

        pack_dir = os.path.join(self.maps.maps_dir, name)
        self.refresh_maps()                 # ★ 刷新下拉列表（读 yaml）
        self.maps_combo.set(name)           # 自动选中新建的
        self.log(f"📦 地图包「{name}」已创建：{pack_dir}", "ok")
        messagebox.showinfo(
            "创建成功",
            f"地图包「{name}」已创建。\n\n"
            f"请把完整地图放到：\n{pack_dir}\\map.png\n\n"
            f"然后：加载 → 校准小地图 → 绘制路线 → 保存",
            parent=self)

    def bind_window(self):
        """绑定所选游戏窗口 → 进入预览监控模式"""
        if self.cfg.get("capture_mode") == "vnc":
            messagebox.showinfo(
                "提示",
                "当前是【虚拟机游戏(VNC)】模式，请点「🔌 连接 VNC」，"
                "切到【本机游戏】才会绑定窗口",
                parent=self)
            return
        # 防御：确保引擎侧 capture 是 Window 系（Fast 或旧版都行）
        if not isinstance(self.engine.capture, (WindowCapture, FastWindowCapture)):
            self.engine.switch_capture_mode("window")
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

    def unbind_window(self):
        """解绑当前游戏窗口"""
        if not self.engine.window_bound():
            self.log("当前未绑定窗口", "info")
            return
        if not messagebox.askyesno("确认", "确定解绑当前游戏窗口？",
                                   parent=self):
            return
        if self.engine.unbind_window():
            self.win_label.config(text="未绑定", fg=TEXT_DIM)
            self.log("窗口已解绑，可重新绑定", "ok")
        else:
            self.log("解绑失败", "error")

    def _save_vnc_cfg(self):
        """VNC 表单 → config 并落盘"""
        vnc_cfg = self.cfg.setdefault("vnc", {})
        vnc_cfg["host"] = self.vnc_host_entry.get().strip() or "127.0.0.1"
        try:
            vnc_cfg["port"] = int(self.vnc_port_entry.get().strip())
        except (ValueError, AttributeError):
            vnc_cfg["port"] = 5900
        vnc_cfg["password"] = self.vnc_pwd_entry.get()
        self.cfg_mgr.save()

    def _refresh_capture_ui(self, force=False):
        """按 capture_mode 显示对应面板（grid_remove / grid 切换）"""
        mode = self.cfg.get("capture_mode", "window")
        if not force and getattr(self, "_capture_ui_shown", None) == mode:
            return
        self._capture_ui_shown = mode
        if mode == "vnc":
            self.win_bind_box.grid_remove()
            self.vnc_bind_box.grid()
        else:
            self.vnc_bind_box.grid_remove()
            self.win_bind_box.grid()

    def bind_vnc(self):
        """连接 VNC 虚拟机"""
        self._save_vnc_cfg()
        # 防御：确保引擎侧 capture 是 VncCapture
        if not isinstance(self.engine.capture, VncCapture):
            self.engine.switch_capture_mode("vnc")
        v = self.cfg.get("vnc", {})
        host = v.get("host", "127.0.0.1")
        port = v.get("port", 5900)
        pwd = v.get("password") or None
        ok = self.engine.switch_capture_mode("vnc", host=host, port=port,
                                             password=pwd)
        # 上面那次只重建，现在真正触发连接
        kw = f"{host}:{port}"
        if self.engine.capture.bind(kw):
            self.engine.reload_runtime()
            self.engine.set_mode(Mode.PREVIEW)
            self.vnc_label.config(text=f"✅ 已连接 {host}:{port}", fg=GREEN)
            self.log(f"VNC 已连接：{host}:{port}，"
                     f"分辨率 {self.engine.capture.size}", "ok")
        else:
            self.vnc_label.config(text=f"❌ 连接失败 {host}:{port}", fg=RED)
            self.log(f"VNC 连接失败：{host}:{port}"
                     f"（检查服务是否开启/端口/密码）", "error")

    def unbind_vnc(self):
        """断开 VNC 连接"""
        try:
            self.engine._close_capture()
        except Exception:
            pass
        self.vnc_label.config(text="未连接", fg=TEXT_DIM)
        self.log("VNC 连接已断开", "warn")

    def _grab_frame(self):
        """取一帧画面：优先实时截图（框选/录制需要当前画面），失败回退引擎缓存帧"""
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
        """框选怪物本体 → 命名 → 存进地图包（或公共暂存区）→ 热重载"""
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

    def calibrate_offset(self):
        """手动标定 offset（站在地图上能识别的位置点按钮）"""
        if not self.engine.window_bound():
            messagebox.showwarning("提示", "请先绑定游戏窗口", parent=self)
            return
        name = self.cfg.get("patrol", {}).get("current_map", "")
        if not name:
            messagebox.showwarning("提示", "请先加载地图包", parent=self)
            return
        result = self.engine.calibrate_offset()
        if result is None:
            messagebox.showerror(
                "标定失败",
                "可能原因：\n"
                "1. 名字条未匹配（玩家被遮挡/离屏幕边缘）\n"
                "2. 小块地形匹配分数 < 0.15\n"
                "3. map.png 与当前地图不一致",
                parent=self)
            return
        offset, score, scale = result
        p = self.cfg.setdefault("patrol", {})
        p["offset"] = [round(float(offset[0]), 1), round(float(offset[1]), 1)]
        self.cfg_mgr.save()
        # ★ 写入地图包 profile.json
        try:
            self.maps.update_profile(name, {
                "patrol": {"offset": p["offset"]},
            })
        except Exception as e:
            self.log(f"offset 写入地图包失败: {e}", "warn")
        # 立即生效
        self.engine.route_nav.configure(offset=p["offset"])
        self.log(f"✓ 标定完成 offset=({offset[0]:.0f},{offset[1]:.0f}) "
                 f"score={score:.2f} s={scale:.2f}", "ok")
        messagebox.showinfo(
            "标定成功",
            f"offset = ({offset[0]:.0f}, {offset[1]:.0f})\n"
            f"score = {score:.2f}\nscale = {scale:.2f}\n\n"
            f"已保存到地图包「{name}」",
            parent=self)

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
        removed = self.cfg["monster_templates"][sel[0]]
        if not messagebox.askyesno(
                "删除确认",
                f"确定删除怪物模板「{removed['name']}」？\n"
                f"对应的模板图片文件也将一并删除。",
                parent=self):
            return
        # 删除模板图片文件
        p = removed.get("path", "")
        if p and os.path.exists(p):
            try:
                os.remove(p)
                self.log(f"模板图片已删除: {os.path.basename(p)}", "info")
            except OSError as e:
                self.log(f"模板图片删除失败: {e}", "warn")
        self.cfg["monster_templates"].pop(sel[0])
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
        """校准 HP/MP/EXP 状态条：框选区域 + 引擎自动适配颜色"""
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
        """框选检测区域（缩小搜索范围提速），None=全屏"""
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
        """patrol 配置节（不存在则自动补默认空字典）"""
        return self.cfg.setdefault("patrol", {})

    # ---------- 多路线辅助 ----------
    def _route_count(self):
        """当前已加载的路线数（0 = 无路线）"""
        return len([r for r in self._route_imgs if r is not None])

    def _sync_route_img(self):
        """把 _route_imgs 同步到 _route_img（指向最后一条）"""
        valid = [r for r in self._route_imgs if r is not None]
        self._route_img = valid[-1] if valid else None

    def calibrate_minimap(self):
        """校准小地图：框选地图区域 → 多帧采样玩家黄点颜色 → 顺手录制底图"""
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
            self._quick_snapshot()  # ★ 改成快照
        RegionSelector(self, frame, mode="region", on_ok=ok,
                       tip="框选小地图的【地图区域】（不含标题文字，尽量贴紧边界）")

    # ---------- 地图包 / 颜色路线 ----------
    def refresh_maps(self):
        """刷新地图配置下拉列表（数据源：config/config_map.yaml）

        · 下拉里显示的是 YAML 里的配置名 name
        · cfg['patrol']['current_map'] 里存的仍是 package（目录名），
          所以要用 find_by_package 反查回名字来 set()
        """
        try:
            self._map_entries = load_map_entries()
        except MapConfigError as e:
            self._map_entries = []
            self.maps_combo["values"] = []
            self.maps_combo.set("")
            self.log(f"⚠ 地图配置读取失败: {e}", "warn")
            return

        names = [e.name for e in self._map_entries]
        self.maps_combo["values"] = names

        # 反查：current_map（package 目录名）→ 配置名
        cur_pack = self.cfg.get("patrol", {}).get("current_map", "")
        cur_name = ""
        if cur_pack:
            e = find_by_package(cur_pack, self._map_entries)
            if e:
                cur_name = e.name

        if cur_name and cur_name in names:
            self.maps_combo.set(cur_name)
        elif names:
            self.maps_combo.current(0)

    def _quick_snapshot(self):
        """校准后抓一帧小地图，直接作为 map.png

        适用于"小地图显示整张地图"的情况（火焰之地V、蘑菇山等）。
        滚动小地图请用「🎬 录制小地图」走一圈。
        """
        name = self.cfg.get("patrol", {}).get("current_map", "")
        if not name:
            return
        mm = self.cfg.get("patrol", {}).get("minimap", {})
        if mm.get("w", 0) < 5:
            return
        frame = self._grab_frame()
        if frame is None:
            return
        mini = frame[mm["y"]:mm["y"] + mm["h"],
                     mm["x"]:mm["x"] + mm["w"]].copy()
        out = os.path.join(self.maps.maps_dir, name, "map.png")
        ok, buf = cv2.imencode(".png", mini)
        if ok:
            buf.tofile(out)
            self.log(f"📸 已快照小地图为 map.png（{mm['w']}x{mm['h']}）", "ok")
            self.engine.invalidate_route_cache()
            self.engine.reload_runtime()
        else:
            self.log("❌ 快照保存失败", "error")

    def record_minimap(self):
        """录制小地图（滚动拼图，用于小地图会滚动的长地图）

        走一圈 → 拼成大图 → 写入 map.png
        """
        if not self.engine.window_bound():
            messagebox.showwarning("提示", "请先绑定游戏窗口", parent=self)
            return
        name = self.cfg.get("patrol", {}).get("current_map", "")
        if not name:
            messagebox.showwarning("提示", "请先加载地图包", parent=self)
            return
        mm = self.cfg.get("patrol", {}).get("minimap", {})
        if mm.get("w", 0) < 5:
            messagebox.showwarning("提示", "请先「校准小地图」", parent=self)
            return

        from core.map_recorder import MapRecorder

        roi = (mm["x"], mm["y"], mm["w"], mm["h"])
        color = self.cfg.get("patrol", {}).get("player_dot_color", [0, 128, 255])
        self._recorder = MapRecorder(
            capture=self.engine.capture,   # ★ 复用 engine 的
            roi=roi,
            player_color=color,
            log_fn=self.log)
        self._recorder.start()
        self._recorder_pack = name
        self._recorder_target_w = int(mm["w"])

        self._rec_dialog = tk.Toplevel(self)
        self._rec_dialog.title("录制地图")
        self._rec_dialog.configure(bg=PANEL)
        self._rec_dialog.geometry("320x160")
        self._rec_dialog.transient(self)
        # ★ 不用 grab_set，避免阻塞主循环

        tk.Label(self._rec_dialog, text="🎬 录制中…",
                 bg=PANEL, fg=ACCENT, font=(FONT, 12, "bold")).pack(pady=(15, 5))
        self._rec_info = tk.Label(self._rec_dialog,
                                   text="请在游戏里走一圈，把地图走遍",
                                   bg=PANEL, fg=TEXT, font=(FONT, 10))
        self._rec_info.pack(pady=5)

        btn_row = tk.Frame(self._rec_dialog, bg=PANEL)
        btn_row.pack(pady=10)
        NeoButton(btn_row, "💾 保存", command=self._stop_recording,
                  bg="#2f6f4f", fg=TEXT).pack(side="left", padx=4)
        NeoButton(btn_row, "✖ 取消", command=self._cancel_recording,
                  bg="#3a3f55", fg=TEXT).pack(side="left", padx=4)

        self._poll_recorder()

    def _poll_recorder(self):
        """每 500ms 刷新录制进度"""
        if not hasattr(self, "_recorder") or self._recorder is None:
            return
        if not self._recorder.is_alive():
            return
        n = self._recorder.frame_count
        cw, ch = self._recorder.canvas_size()
        self._rec_info.config(text=f"已采集 {n} 帧\n画布 {cw}×{ch}")
        self.after(500, self._poll_recorder)

    def _stop_recording(self):
        """停止录制并保存"""
        rec = getattr(self, "_recorder", None)
        if rec is None:
            return
        rec.stop()
        rec.join(timeout=2)
        pack = getattr(self, "_recorder_pack", "")
        target_w = getattr(self, "_recorder_target_w", 93)
        from core.config_manager import ROOT
        out = os.path.join(self.maps.maps_dir, pack, "map.png")
        ok = rec.save(out, target_w)
        if ok:
            self.log(f"✅ 拼图已保存: {out}", "ok")
        else:
            self.log("❌ 保存失败", "error")
        self._recorder = None
        try:
            self._rec_dialog.destroy()
        except Exception:
            pass
        # 重新加载地图包
        self.engine.invalidate_route_cache()
        self.engine.reload_runtime()

    def _cancel_recording(self):
        """取消录制"""
        rec = getattr(self, "_recorder", None)
        if rec is not None:
            rec.stop()
            self._recorder = None
        try:
            self._rec_dialog.destroy()
        except Exception:
            pass
        self.log("录制已取消", "warn")


    def paint_route(self):
        """打开颜色路线绘制器（v3：底图 = map.png，大窗口绘制）

        · 底图：从当前地图包加载 map.png
        · 已有路线：作为初始图传入，可二次编辑
        · 覆盖模式：画完后整体替换（追加请用「➕ 新增路线」）
        """
        name = self.cfg.get("patrol", {}).get("current_map", "")
        if not name:
            messagebox.showwarning("提示", "请先选择/加载地图包", parent=self)
            return

        # ★ 从地图包加载底图（wz 原图自动缩放）
        mm = self.cfg.get("patrol", {}).get("minimap", {})
        pack_dir = os.path.join(self.maps.maps_dir, name)
        map_img = self.engine._load_map_for_pack(pack_dir, mm)
        if map_img is None:
            messagebox.showwarning(
                "提示",
                f"地图包「{name}」里没有可用的底图\n\n"
                f"请放入以下任一文件:\n"
                f"  · {pack_dir}\\minimap_wz.png（推荐，wz 原图）\n"
                f"  · {pack_dir}\\map.png（旧格式）",
                parent=self)
            return

        def ok(route_img):
            # 覆盖模式：整份替换
            self._route_imgs = [route_img]
            self._sync_route_img()
            rp = os.path.join(self.maps.maps_dir, name, "route.png")
            self.maps.save_route(name, self._route_imgs)
            self.cfg.setdefault("patrol", {})["route_path"] = rp
            self.cfg_mgr.save()
            self.engine.load_route(self._route_imgs, path_tag=rp)
            self._update_route_label()
            self.refresh_patrol_label()
            self.log(f"🎨 路线已保存到地图包「{name}」", "ok")

        # 传入已有路线（若有），便于二次编辑
        initial_route = self._route_img if self._route_img is not None else None
        if initial_route is not None and \
                initial_route.shape[:2] != map_img.shape[:2]:
            # 尺寸不匹配（旧路线）→ 丢弃
            initial_route = None
        RoutePainter(self, map_img, initial_route, on_ok=ok)

    def paint_route_new(self):
        """新增一条路线（追加到现有列表，不清空已有）"""
        name = self.cfg.get("patrol", {}).get("current_map", "")
        if not name:
            messagebox.showwarning("提示", "请先选择/加载地图包", parent=self)
            return

        mm = self.cfg.get("patrol", {}).get("minimap", {})
        pack_dir = os.path.join(self.maps.maps_dir, name)
        map_img = self.engine._load_map_for_pack(pack_dir, mm)
        if map_img is None:
            messagebox.showwarning(
                "提示",
                f"地图包「{name}」里没有可用的底图\n\n"
                f"请放入以下任一文件:\n"
                f"  · {pack_dir}\\minimap_wz.png\n"
                f"  · {pack_dir}\\map.png",
                parent=self)
            return

        def ok(route_img):
            # 追加模式
            self._route_imgs.append(route_img)
            self._sync_route_img()
            rp = os.path.join(self.maps.maps_dir, name, "route.png")
            self.maps.save_route(name, self._route_imgs)
            self.cfg.setdefault("patrol", {})["route_path"] = rp
            self.cfg_mgr.save()
            self.engine.load_route(self._route_imgs, path_tag=rp)
            self._update_route_label()
            self.refresh_patrol_label()
            self.log(f"🎨 新增路线 → 地图包「{name}」共 "
                     f"{self._route_count()} 条", "ok")

        # 新增：空白底图（从零画）
        RoutePainter(self, map_img, None, on_ok=ok)

    def _update_route_label(self):
        """刷新「路线: N 条」标签"""
        if hasattr(self, "route_count_label"):
            n = self._route_count()
            self.route_count_label.config(
                text=f"路线: {n} 条",
                fg=TEXT if n > 0 else TEXT_DIM)

    def save_map_pack(self):
        """保存当前配置到地图包。

        ★ 与 config_map.yaml 联动：
          · 输入的名称在 YAML 中   → 保存到该配置映射的 package 目录
          · 输入的名称不在 YAML 中 → 按旧行为，直接用名称做目录名
        """
        default_name = self.maps_combo.get().strip() or \
                       self.cfg.get("patrol", {}).get("current_map", "")

        name = (simpledialog.askstring(
            "保存地图包",
            "输入地图配置名：\n"
            "· 在 config_map.yaml 中已存在 → 保存到对应地图包目录\n"
            "· 新名称 → 若不在 YAML 中，将直接作为目录名（建议顺手加进 YAML）",
            initialvalue=default_name, parent=self) or "").strip()
        if not name:
            return

        # ---- 解析配置名 → 包目录名 ----
        try:
            entries = self._map_entries or load_map_entries()
        except MapConfigError:
            entries = []
        entry = find_by_name(name, entries)
        pack = entry.package if entry else name  # ★ 关键：目录名

        if pack in self.maps.list_maps() and not messagebox.askyesno(
                "覆盖确认", f"地图包「{pack}」已存在，覆盖保存？", parent=self):
            return

        mm = self.cfg.get("patrol", {}).get("minimap", {})
        if mm.get("w", 0) < 5:
            messagebox.showwarning("提示", "请先「🧭 校准小地图」再保存地图包",
                                   parent=self)
            return
        try:
            # ★ 保证 config_map.yaml 里有这个目录（老包补录）
            try:
                self.maps.add_yaml_entry(name, package=pack)
            except Exception as e:
                self.log(f"config_map.yaml 写入失败: {e}", "warn")
            self.maps.save(pack, self.cfg, self._minimap_snap, self._route_imgs,
                           grab_fn=lambda: self._grab_frame())
        except Exception as e:
            self.log(f"地图包保存失败: {e}", "error")
            messagebox.showerror("错误", f"地图包保存失败：{e}", parent=self)
            return

        # 回填
        mm_img = self.maps.load_minimap(pack)
        if mm_img is not None:
            self._minimap_snap = mm_img
        if not self._route_imgs:
            self._route_imgs = self.maps.load_route(pack) or []
            self._sync_route_img()
        if self._minimap_snap is not None:
            self.engine.set_nav_base(self._minimap_snap)

        p = self.cfg.setdefault("patrol", {})
        p["current_map"] = pack  # ★ 存包目录名
        p["route_path"] = os.path.join(self.maps.maps_dir, pack, "route.png")
        p["enabled"] = True
        self.cfg_mgr.save()
        self.engine.reload_runtime()
        self.refresh_maps()
        self.refresh_patrol_label()
        self.patrol_var.set(True)
        self.log(f"🗺 地图包「{pack}」已保存（配置名「{name}」）：绑定怪物 "
                 f"{len(self.cfg.get('monster_templates', []))} 个 · "
                 f"底图{'✓' if self._minimap_snap is not None else '✗(未绑定窗口无法补拍)'} · "
                 f"路线{self._route_count()}条，巡逻已启用", "ok")

    def load_map_pack(self):
        """加载地图配置：config_map.yaml 的 配置名 → maps/<package>/

        ★ 校验 map.png 必须存在：maps/<package>/map.png
        """
        sel = self.maps_combo.get().strip()
        if not sel:
            messagebox.showwarning("提示", "请先选择地图配置", parent=self)
            return

        # ---- 解析配置名 → 地图包目录名 ----
        if not self._map_entries:
            try:
                self._map_entries = load_map_entries()
            except MapConfigError as e:
                messagebox.showerror("错误", f"地图配置读取失败：\n{e}", parent=self)
                return
        entry = find_by_name(sel, self._map_entries)
        if entry is None:
            messagebox.showerror(
                "错误",
                f"配置「{sel}」未在 config_map.yaml 中找到\n"
                f"请检查 config/config_map.yaml 里的 name 字段",
                parent=self)
            return
        name = entry.package  # ★ 关键：映射到实际目录名

        # ---- ★ 校验底图存在（wz 原图 或 map.png 任一）----
        pack_dir = os.path.join(self.maps.maps_dir, name)
        wz_png = os.path.join(pack_dir, "minimap_wz.png")
        map_png = os.path.join(pack_dir, "map.png")
        if not os.path.isfile(wz_png) and not os.path.isfile(map_png):
            messagebox.showerror(
                "错误",
                f"地图包「{name}」缺少底图\n\n"
                f"请放入以下任一文件:\n"
                f"  · {wz_png}（推荐，wz 原图）\n"
                f"  · {map_png}（旧格式）",
                parent=self)
            self.log(f"❌ 缺少底图: {pack_dir}", "error")
            return

        # ================= 以下是原有的加载逻辑（未改动） =================
        self.engine.invalidate_route_cache()
        ok, missing = self.maps.load(name, self.cfg)
        # ★ 强制设置 current_map（profile 里可能没有这个字段）
        self.cfg.setdefault("patrol", {})["current_map"] = name

        # ★ 从 profile.json 恢复按键 / 定时按键
        prof = self.maps.read_profile(name)
        if prof:
            keys = prof.get("keys")
            if isinstance(keys, dict):
                for k, v in keys.items():
                    if k in self.cfg["keys"]:
                        self.cfg["keys"][k] = v
            timers = prof.get("timers")
            if isinstance(timers, dict):
                self.cfg.setdefault("timers", {}).update(timers)
            # 同步刷新 UI 控件
            for k, ent in self._key_entries.items():
                ent.var.set(self.cfg["keys"].get(k, ""))

        if not ok:
            messagebox.showerror("错误", "地图包加载失败（profile.json 缺失）",
                                 parent=self)
            return
        self._minimap_snap = self.maps.load_minimap(name)
        self._route_imgs = self.maps.load_route(name) or []
        self._sync_route_img()
        # 底图缺失现场补拍
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
        self._update_route_label()
        for k, ent in self._key_entries.items():
            ent.var.set(self.cfg["keys"].get(k, "-"))
        self.patrol_var.set(self.cfg.get("patrol", {}).get("enabled", False))
        # 同步偏移输入框（地图包切换后）
        cur_off = self.cfg.get("patrol", {}).get("map_offset", [0, 0])
        if hasattr(self, "_off_x_entry") and isinstance(cur_off, list) \
                and len(cur_off) == 2:
            self._off_x_entry.delete(0, "end")
            self._off_x_entry.insert(0, str(cur_off[0]))
            self._off_y_entry.delete(0, "end")
            self._off_y_entry.insert(0, str(cur_off[1]))
        self.refresh_tpl_list()
        self.refresh_maps()
        self.refresh_patrol_label()
        n_routes = self._route_count()
        self.log(f"📦 配置「{sel}」→ 地图包「{name}」已加载：绑定怪物 "
                 f"{len(self.cfg.get('monster_templates', []))} 个 · "
                 f"底图{'✓' if self._minimap_snap is not None else '✗'} · "
                 f"路线{n_routes}条 · "
                 f"玩家模板{'✓' if self.cfg.get('player_template') else '✗'}", "ok")
        if n_routes == 0:
            self.log("路线图缺失：请「🎨 绘制路线」，画完自动存入本地图包", "warn")
        wt = self.cfg.get("window_title", "")
        if wt and not self.engine.window_bound():
            if self.engine.bind_window(wt):
                self.engine.set_mode(Mode.PREVIEW)
                self.win_label.config(text=f"✅ {self.engine.capture.window_title}",
                                      fg=GREEN)

    def delete_map_pack(self):
        sel = self.maps_combo.get().strip()
        if not sel:
            return
        try:
            entries = self._map_entries or load_map_entries()
        except MapConfigError:
            entries = []
        entry = find_by_name(sel, entries)
        pack = entry.package if entry else sel

        if not messagebox.askyesno(
                "删除确认",
                f"确定删除地图包「{pack}」？\n"
                f"（配置名「{sel}」· 包内绑定的怪物模板一并删除）",
                parent=self):
            return

        pack_dir = os.path.join(self.maps.maps_dir, pack)
        keep, removed = [], []
        for tpl in self.cfg.get("monster_templates", []):
            p = tpl.get("path", "")
            if p and os.path.normcase(p).startswith(os.path.normcase(pack_dir)):
                removed.append(tpl)
            else:
                keep.append(tpl)

        self.maps.delete(pack)
        if self.cfg.get("patrol", {}).get("current_map") == pack:
            self.cfg["patrol"]["current_map"] = ""
            self.cfg["patrol"]["route_path"] = ""
            self._minimap_snap = None
            self._route_imgs = []
            self._route_img = None
            self.cfg["monster_templates"] = keep
            for tpl in removed:
                p = tpl.get("path", "")
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                        self.log(f"模板文件已删除: {os.path.basename(p)}", "info")
                    except OSError:
                        pass
            self.cfg_mgr.save()
            self.engine.clear_route()
            self.engine.reload_runtime()
            self.refresh_tpl_list()
        self.refresh_maps()
        self.refresh_patrol_label()
        self.log(f"地图包「{pack}」（配置名「{sel}」）已删除", "warn")

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
        """按键捕获回调：写入 config 并保存（空值 = 清空该按键）"""
        v = "" if (not value or value == "-") else value
        self.cfg["keys"][key] = v
        self.cfg_mgr.save()
        self.log(f"按键 [{key}] → {v or '(空)'}", "info")

    def _apply_map_offset(self):
        """地图坐标微调：改动立即写入 config 并热重载 route_nav

        不重启、不重载地图包，下一次定位就用新值。
        """
        try:
            ox = float(self._off_x_entry.get().strip() or 0)
        except (ValueError, AttributeError):
            ox = 0.0
        try:
            oy = float(self._off_y_entry.get().strip() or 0)
        except (ValueError, AttributeError):
            oy = 0.0

        self.cfg.setdefault("patrol", {})["map_offset"] = [ox, oy]
        self.cfg_mgr.save()
        # ★ 立即生效
        self.engine.route_nav.configure(map_offset_x=ox, map_offset_y=oy)

    def _set_timer(self, key, entry):
        """保存定时按键间隔（秒）"""
        try:
            v = int(float(entry.get().strip()))
            if v < 1:
                v = 1
        except (ValueError, AttributeError):
            v = 180
        self.cfg.setdefault("timers", {})[key] = v
        self.cfg_mgr.save()

    def start_bot(self, _e=None):
        """▶ 启动挂机：前置校验（绑定窗口/怪物模板）→ 引擎切到 RUNNING"""
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
        """⏹ 停止：回到预览监控（保持识别不按键）"""
        self.engine.set_mode(Mode.PREVIEW)
        self.log("⏹ 已停止战斗，保持监控", "warn")

    def toggle_run(self, _e=None):
        """F8：运行 ⇄ 停止"""
        self.stop_bot() if self.engine.mode == Mode.RUNNING else self.start_bot()

    def toggle_pause(self, _e=None):
        """F9：运行 ⇄ 暂停（暂停只停按键，继续监控）"""
        if self.engine.mode == Mode.RUNNING:
            self.engine.set_mode(Mode.PAUSED)
            self.log("已暂停", "warn")
        elif self.engine.mode == Mode.PAUSED:
            self.engine.set_mode(Mode.RUNNING)
            self.log("恢复运行", "ok")

    def _set_hotkey(self, which, value):
        """保存热键配置并重新绑定"""
        hk = self.cfg.setdefault("hotkeys", {})
        hk[which] = value
        self.cfg_mgr.save()
        self._apply_hotkeys()
        self._update_hotkey_label()

    def _set_hotkey_global(self):
        """切换全局热键模式"""
        hk = self.cfg.setdefault("hotkeys", {})
        enabled = self._hk_global_var.get()
        # 全局热键需要 keyboard 库支持；不可用时回退局部模式并提示
        if enabled and not self.hotkey_mgr.global_supported:
            self.log("未安装 keyboard 库，全局热键不可用，回退为局部模式", "warn")
            enabled = False
            self._hk_global_var.set(False)
        hk["global_enabled"] = enabled
        self.cfg_mgr.save()
        self._apply_hotkeys()
        self._update_hotkey_label()

    def _apply_hotkeys(self):
        """从配置读取热键并绑定"""
        hk = self.cfg.get("hotkeys", {})
        k_start = hk.get("start_stop", "F8")
        k_pause = hk.get("pause_resume", "F9")
        global_en = hk.get("global_enabled", False)
        self.hotkey_mgr.set_global(global_en)
        self.hotkey_mgr.bind(k_start, self.toggle_run)
        self.hotkey_mgr.bind(k_pause, self.toggle_pause)
        label = "全局" if (global_en and self.hotkey_mgr.global_supported) else "局部(控制台聚焦时)"
        self.log(f"热键已绑定：{k_start}=启动/停止，{k_pause}=暂停/继续 [{label}]", "info")

    def _update_hotkey_label(self):
        if not hasattr(self, "hotkey_mgr") or self.hotkey_mgr is None:
            return
        hk = self.cfg.get("hotkeys", {})
        k_start = hk.get("start_stop", "F8")
        k_pause = hk.get("pause_resume", "F9")
        global_en = hk.get("global_enabled", False)
        mode = "全局" if (global_en and self.hotkey_mgr.global_supported) else "控制台聚焦时"
        if self.hotkey_mgr and not self.hotkey_mgr.global_supported and global_en:
            mode = "控制台聚焦时(未装keyboard库)"
        self._hotkey_label.config(
            text=f"{k_start}=启动/停止 · {k_pause}=暂停/继续 [{mode}]")

    def log(self, msg, lv="info"):
        """向日志队列推一条日志（引擎线程/GUI 线程共用）"""
        self.log_queue.put((time.strftime("%H:%M:%S"), msg, lv))

    # ================= 轮询刷新 =================
    def _poll_status(self):
        """状态轮询（每 80ms）：拉预览帧 → 刷新进度条/状态胶囊/信息标签"""
        ann = None
        try:
            ann = self.engine.preview_queue.get_nowait()
        except queue.Empty:
            pass
        if ann is not None:
            self._show_preview(ann)

        st = self.engine.status
        self.hp_bar_w.set(st["hp"])
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
        """把引擎的标注帧等比缩放到预览画布上显示"""
        img = cv2.cvtColor(ann, cv2.COLOR_BGR2RGB)
        h, w = img.shape[:2]

        # ★ 尺寸稳定：与上次差异 ≤ 3px 就不重算缩放，避免预览图抖
        last = getattr(self, "_pv_last", None)
        if last is None or abs(last[0] - w) > 3 or abs(last[1] - h) > 3:
            s = min(self.PREVIEW_W / w, self.PREVIEW_H / h)
            nw, nh = max(1, int(w * s)), max(1, int(h * s))
            self._pv_last = (w, h, nw, nh)
        else:
            _, _, nw, nh = self._pv_last

        img = cv2.resize(img, (nw, nh))
        self._pv_photo = ImageTk.PhotoImage(Image.fromarray(img))
        c = self.preview_canvas
        c.delete("all")
        c.create_rectangle(0, 0, self.PREVIEW_W, self.PREVIEW_H,
                           fill="#0a0c12", outline="")
        c.create_image((self.PREVIEW_W - nw) // 2,
                       (self.PREVIEW_H - nh) // 2,
                       anchor="nw", image=self._pv_photo)

    def _poll_log(self):
        """日志轮询（每 250ms）：把队列里的日志增量写入日志框"""
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
        """关窗：先停引擎线程并保存配置，再销毁窗口"""
        self.hotkey_mgr.cleanup()
        self.engine.shutdown()
        self.cfg_mgr.save()
        self.destroy()

    def save_keys_to_pack(self):
        """把按键 + 定时按键 + 攻击方式，写入当前地图包"""
        name = self.cfg.get("patrol", {}).get("current_map", "")
        if not name:
            messagebox.showwarning("提示", "请先加载地图包", parent=self)
            return
        try:
            self.maps.update_profile(name, {
                "keys": dict(self.cfg["keys"]),
                "timers": dict(self.cfg.get("timers", {})),
            })
            self.log(f"💾 按键配置已保存到地图包「{name}」", "ok")
        except Exception as e:
            self.log(f"保存失败: {e}", "error")
            messagebox.showerror("错误", f"保存失败: {e}", parent=self)

    def read_profile(self, name):
        """读地图包 profile.json，返回 dict 或 None"""
        import json
        path = os.path.join(self.maps_dir, name, "profile.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
