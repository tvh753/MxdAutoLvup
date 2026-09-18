# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:06
# @Author  : yy
# @File    : bot_engine.py
# @Software: MxdAutoLvup

"""截图 → 资源监测 → 模板识别 → 战斗决策 → 按键输出（独立线程）

线程模型：
  · 主循环 _tick：截图 / 血蓝条 / 决策 / 预览（不阻塞按键下发）
  · 键盘线程 _keyboard_loop：30 FPS 消费命令队列，独立于主循环
  · 识别线程 _det_loop：异步跑模板匹配，主循环只读最新结果
  · 定位线程在 ColorRouteNavigator 内部（大 map 模式下异步匹配）
"""
import time
import threading
import queue
import cv2
import os
import random
import datetime as _dt
import numpy as np

from core.window_capture import WindowCapture, VncCapture
from core.detector import TemplateDetector
from core.resource_monitor import BarMonitor
from core.action_controller import ActionController, MovementController
from core.vnc_controller import VncActionController, VncMovementController
from core.color_route import ColorRouteNavigator
from core.imio import imread_u
from core.config_manager import PLAYER_TPL_PATH, resolve_player_path


class Mode:
    """运行模式"""
    IDLE = "idle"        # 空闲（未绑定窗口）
    PREVIEW = "preview"  # 监控（只识别不按键）
    RUNNING = "running"  # 运行（挂机）
    PAUSED = "paused"    # 暂停（保留识别，不按键）


class BotEngine(threading.Thread):
    """机器人引擎主线程 + 三个辅助线程（键盘 / 识别 / 定位）"""

    DET_INTERVAL = 0.20   # 怪物识别节流（0.2s = 5Hz，兼顾性能）
    KB_FPS = 30           # 键盘线程下发帧率

    def __init__(self, cfg, log_fn):
        super().__init__(daemon=True, name="BotEngine")
        self.cfg = cfg
        self.log = log_fn
        self._canvas_size = None  # ★ 锁定的画布尺寸 (w, h)，防止截图抖动

        self.detector = TemplateDetector()

        # capture / controller / move 三件套按 capture_mode 创建
        self.capture = None
        self.controller = None
        self.move = None
        self._setup_capture_bindings()

        # 追击超时控制
        self._chase_t0 = None
        self._giveup_until = None

        # 识别节流（旧接口保留，识别已迁到独立线程）
        self._det_t = 0.0
        self._last_monsters, self._last_player = [], None
        self._last_boxes = []       # 预览用：本帧识别框
        self._none_frames = 0       # 连续截图失败计数

        # 血蓝经验条
        self.hp_bar = BarMonitor("hp")
        self.mp_bar = BarMonitor("mp")
        self.exp_bar = BarMonitor("exp")

        # 预览帧队列（GUI 侧轮询取出）
        self.preview_queue = queue.Queue(maxsize=2)
        self._th_cache = None

        # 颜色路线导航
        self.route_nav = ColorRouteNavigator()
        self._route_path_loaded = None
        self._offroute_log_t = 0.0
        self.route_nav.on_log = self.log
        self._nav_base = None

        # 运行状态
        self.mode = Mode.IDLE
        self._stop_flag = False
        self._frame = None
        self._frame_lock = threading.Lock()
        self._player_tpl = None     # 玩家模板名

        # 状态面板数据
        self.status = {"hp": -1.0, "mp": -1.0, "exp": -1.0, "fps": 0,
                       "monsters": 0, "action": "-", "mode": Mode.IDLE}

        # 巡逻/战斗状态
        self._roam_dir = 1
        self._roam_t = 0.0
        self._last_warn = 0.0
        self._last_preview = 0.0
        self._fps_n = 0
        self._fps_t = time.time()
        self._facing = 1                # 当前朝向：-1 左 / 1 右
        self._loot_t = 0.0
        self._unstuck_until = 0.0  # 脱困锁定期到期时间
        self._unstuck_dir = 1  # 脱困锁定期方向：-1 左 / 1 右
        self._was_combat = False
        self._last_map_pos = None

        # 休息 / 定时调度
        # ============ 拍卖场休息状态机 ============
        self._rest_phase = "idle"  # idle / to_auction / in_auction / from_auction
        self._rest_phase_t = 0.0  # 相位开始时间
        self._resting = False
        self._rest_until = 0.0
        self._sess_end = None
        self._grace_until = None
        self._rest_rope = None
        self._rest_status = ""
        self._logged_out = False
        self._logout_deadline = None
        self._login_deadline = None
        self._logout_done_today = None
        self._login_done_today = None

        # 随机走神（防检测：每隔几分钟停 1~3 秒）
        self._idle_until = 0
        self._next_idle_t = time.time() + 180

        # ★ 键盘线程：30 FPS 独立下发，避免主循环卡顿影响按键节奏
        self._kb_stop = False
        self._kb_thread = threading.Thread(
            target=self._keyboard_loop, daemon=True, name="KbThread")
        self._kb_thread.start()

        # ★ 识别线程：异步跑模板匹配，主循环只读结果
        self._det_lock = threading.Lock()
        self._det_frame_slot = None        # 待识别帧（覆盖式）
        self._det_monsters_latest = []     # 最近识别结果
        self._det_player_latest = None
        self._det_thread_stop = False
        self._det_thread = threading.Thread(
            target=self._det_loop, daemon=True, name="DetThread")
        self._det_thread.start()

        # ★ 导航线程：异步跑小地图/大map定位，主循环只读最新结果
        #
        # 【为什么独立线程】
        # player_pos 在"大map模式"下要做模板匹配（5~15ms），
        # 在 30 FPS 主循环里同步执行会拖慢帧率。
        # 移到独立线程后，主循环只花 ~1μs（投喂 + 读结果）。
        #
        # 【数据流】
        # 主线程：_tick → 把 frame 放入 _nav_frame_slot（覆盖式）
        # 导航线程：读 slot → 跑 player_pos → 写 _nav_latest
        # 主线程：读 _nav_latest（立即返回，不阻塞）
        #
        # 【为什么用覆盖式 slot 而不是队列】
        # 导航只需要"最新"的结果，中间帧丢弃无所谓。
        # 队列会积压，覆盖式天然保持"最新"。
        self._nav_lock = threading.Lock()
        self._nav_frame_slot = None       # 待处理帧
        self._nav_latest = None           # 最新定位结果
        self._nav_thread_stop = False
        self._nav_thread = threading.Thread(
            target=self._nav_loop, daemon=True, name="NavThread")
        self._nav_thread.start()

        self._focus_cache = True
        self._focus_tick = 0

    # ============ 对外接口 ============
    def bind_window(self, keyword) -> bool:
        """绑定游戏窗口。换窗口后重置画布尺寸锁定"""
        if self.capture is None:
            return False
        if self.capture.bind(keyword):
            self._canvas_size = None  # ★ 换窗口后重新锁
            self.reload_runtime()
            return True
        return False

    def unbind_window(self) -> bool:
        """解绑当前游戏窗口

        · 释放所有按键
        · 关闭 capture 内部资源
        · 清空 hwnd 标记（window_bound 变 False）
        · 切回 IDLE 模式
        """
        if self.capture is None:
            return False
        try:
            # 释放所有按键（避免角色一直按着某个键）
            if self.move is not None:
                self.move.release_all()
            # 关闭 capture 内部资源
            if hasattr(self.capture, "close"):
                try:
                    self.capture.close()
                except Exception:
                    pass
            # 清空绑定标记
            self.capture.hwnd = None
            # 切回 IDLE
            self.mode = Mode.IDLE
            self.status["mode"] = Mode.IDLE
            return True
        except Exception as e:
            self.log(f"解绑失败: {e}", "warn")
            return False

    def _setup_capture_bindings(self):
        """按 capture_mode 创建 capture / controller / move 三元组"""
        mode = self.cfg.get("capture_mode", "window")
        if mode == "vnc":
            v = self.cfg.get("vnc", {})
            self.capture = VncCapture(
                host=v.get("host", "127.0.0.1"),
                port=v.get("port", 5900),
                password=v.get("password") or None)
            self.controller = VncActionController(self.capture)
            self.move = VncMovementController(self.capture)
        else:
            # ★ 优先用 FastWindowCapture（异步，FPS 更高）
            try:
                from core.window_capture import FastWindowCapture
                self.capture = FastWindowCapture()
            except Exception as e:
                self.capture = WindowCapture()
            self.controller = ActionController()
            self.move = MovementController()

    def switch_capture_mode(self, mode, host=None, port=None, password=None) -> bool:
        """切换抓屏模式（只重建三元组，不自动连接）"""
        self._close_capture()
        self._canvas_size = None  # ★ 重置尺寸锁定
        self.cfg["capture_mode"] = mode
        if mode == "vnc":
            v = self.cfg.setdefault("vnc", {})
            if host is not None:
                v["host"] = host
            if port is not None:
                try:
                    v["port"] = int(port)
                except (TypeError, ValueError):
                    pass
            if password is not None:
                v["password"] = password or ""
        self._setup_capture_bindings()
        if self.move is not None:
            self.move.bind(self.cfg.get("keys", {}))
        return True

    def _close_capture(self):
        """关闭 capture（VNC 需要断开连接，窗口模式无操作）"""
        if self.capture is None:
            return
        if hasattr(self.capture, "close"):
            try:
                self.capture.close()
            except Exception:
                pass

    def window_bound(self) -> bool:
        return self.capture.hwnd is not None

    def latest_frame(self):
        with self._frame_lock:
            return None if self._frame is None else self._frame.copy()

    def reload_runtime(self):
        """模板 / 血蓝条 / 路线配置变更后热重载

        ★ 怪物模板以"当前地图包目录"为准：
          · 有激活地图包：扫 maps/<地图包>/monsters/*.png 全部加载；
            config 里指向别处的旧条目自动丢弃 → 避免"哪来的怪物"。
          · 无激活地图包：只加载 config 里已有的有效条目。
        """
        import time as _time
        from core.config_manager import ROOT

        now = _time.time()
        self.detector.clear()
        current_map = self.cfg.get("patrol", {}).get("current_map", "")
        loaded_names = []

        # ---- 有激活地图包 → 以包内 monsters/ 目录为准 ----
        if current_map:
            pack_dir = os.path.join(ROOT, "maps", current_map, "monsters")
            if os.path.isdir(pack_dir):
                pngs = sorted(f for f in os.listdir(pack_dir)
                              if f.lower().endswith(".png"))

                # 从旧 config 里取"用户起的名字"，优先保留
                name_map = {}
                for item in self.cfg.get("monster_templates", []):
                    p = item.get("path", "")
                    if p:
                        key = os.path.normcase(os.path.abspath(p))
                        name_map[key] = item.get("name") or ""

                # 逐个生成模板条目，重名自动加后缀
                used = set()
                new_list = []
                for fname in pngs:
                    fpath = os.path.join(pack_dir, fname)
                    key = os.path.normcase(os.path.abspath(fpath))
                    tname = name_map.get(key) or os.path.splitext(fname)[0]
                    base = tname
                    i = 1
                    while tname in used:
                        i += 1
                        tname = f"{base}_{i}"
                    used.add(tname)
                    new_list.append({"name": tname, "path": fpath})

                # ★ 用新列表完整替换 cfg（清掉历史残留）
                self.cfg["monster_templates"] = new_list

                for item in new_list:
                    try:
                        self.detector.load(item["name"], item["path"])
                        loaded_names.append(item["name"])
                    except Exception as e:
                        self.log(f"模板加载失败 [{item['name']}]: {e}", "warn")
            else:
                # 地图包目录不存在 → 用 config 原样
                for item in self.cfg.get("monster_templates", []):
                    p = item.get("path", "")
                    if p and os.path.isfile(p):
                        try:
                            self.detector.load(item["name"], p)
                            loaded_names.append(item["name"])
                        except Exception as e:
                            self.log(f"模板加载失败 [{item['name']}]: {e}", "warn")
        else:
            # 无激活地图包 → 用 config 原样
            for item in self.cfg.get("monster_templates", []):
                p = item.get("path", "")
                if p and os.path.isfile(p):
                    try:
                        self.detector.load(item["name"], p)
                        loaded_names.append(item["name"])
                    except Exception as e:
                        self.log(f"模板加载失败 [{item['name']}]: {e}", "warn")

        # ---- 日志汇总 ----
        if loaded_names:
            self.log(f"🎯 识别模板 ({len(loaded_names)}): {loaded_names}", "info")
        else:
            self.log("⚠ 未加载任何怪物模板", "warn")

        # ---- 玩家模板 ----
        pt = self.cfg.get("player_template")
        tpl_path = resolve_player_path(pt.get("path")) if pt else None
        if tpl_path is None:
            if os.path.isfile(PLAYER_TPL_PATH):
                tpl_path = PLAYER_TPL_PATH
                self.cfg["player_template"] = {"name": "玩家", "path": tpl_path}
                self.log("玩家模板路径已自动修正为全局模板", "info")
            else:
                self.cfg["player_template"] = None
                self.log("玩家模板不可用（路径无效且无全局模板），已停用；"
                         "重新「框选玩家模板」可恢复", "warn")
        if tpl_path:
            try:
                self._player_tpl = None
                if pt:
                    try:
                        # ★ 用解析后的完整路径 tpl_path，而不是 pt["path"]（可能是相对路径/目录）
                        self.detector.load(pt.get("name", "玩家"), tpl_path)
                        self._player_tpl = pt.get("name", "玩家")
                    except Exception as e:
                        self.log(f"玩家模板加载失败: {e}", "warn")
            except Exception as e:
                self.log(f"玩家模板加载失败: {e}", "warn")

        # ---- 小地图黄点模板（兼容旧小地图模式）----
        dot_tpl_path = None
        dt = self.cfg.get("dot_template")
        if dt:
            p = dt.get("path")
            if p and os.path.isfile(p):
                dot_tpl_path = p
        if dot_tpl_path is None:
            from core.config_manager import TEMPLATE_DIR
            _fallback = os.path.join(TEMPLATE_DIR, "dot", "dot.png")
            if os.path.isfile(_fallback):
                dot_tpl_path = _fallback
                self.cfg["dot_template"] = {"name": "黄点", "path": _fallback}
        if dot_tpl_path and dot_tpl_path != self.route_nav.dot_tpl_path:
            self.route_nav.configure(dot_tpl_path=dot_tpl_path)
        elif not dot_tpl_path:
            if now - getattr(self, "_no_dot_tpl_log_t", -99.0) > 30.0:
                self._no_dot_tpl_log_t = now
                self.log("⚠ 未配置小地图黄点模板(dot_template)。"
                         "请在GUI参数页设置，或把模板图片放到 templates/dot/dot.png。"
                         "小地图定位将无法工作", "warn")

        # ---- 血蓝经验条区域 ----
        for key, bar in (("hp_bar", self.hp_bar),
                         ("mp_bar", self.mp_bar),
                         ("exp_bar", self.exp_bar)):
            d = self.cfg.get(key, {})
            bar.set(region=(d.get("x", 0), d.get("y", 0),
                            d.get("w", 0), d.get("h", 0)))

        # ---- 路线导航配置 ----
        p = self.cfg.get("patrol", {})
        mm = p.get("minimap", {})
        self.route_nav.configure(
            minimap=(mm.get("x", 0), mm.get("y", 0), mm.get("w", 0), mm.get("h", 0)),
            dot_color=p.get("player_dot_color"),
            tolerance=p.get("dot_tolerance", 80),
            search_range=p.get("search_range", 10),
            grab_tol=p.get("grab_tol", 4),
            dot_max_area=p.get("dot_max_area", 40),
        )



        rp = p.get("route_path", "")

        self.log(f"[reload] route_path={rp!r} loaded={self._route_path_loaded!r}",
                 "info")

        if rp and rp != self._route_path_loaded:
            import glob as _glob
            route_dir = os.path.dirname(rp)
            multi_files = sorted(_glob.glob(os.path.join(route_dir, "route[0-9]*.png")))
            route_imgs = []
            if multi_files:
                for rf in multi_files:
                    img = imread_u(rf)
                    if img is not None:
                        route_imgs.append(img)
            else:
                img = imread_u(rp)
                if img is not None:
                    route_imgs.append(img)

            if not route_imgs:
                self.log(f"路线图不存在或读取失败: {rp}，"
                         f"仅加载底图用于定位", "warn")
                # ★ 即使没路线，也加载 map（定位和 NAV 显示需要）
                map_img = self._load_map_for_pack(route_dir, mm)
                if map_img is not None:
                    self.route_nav.load([], map_bgr=map_img)
                    self._route_path_loaded = rp
                    self._nav_base = map_img
                    mh, mw = map_img.shape[:2]
                    self.log(f"🗺 底图已加载（无路线）：{mw}x{mh}", "info")
                else:
                    self._nav_base = None
            else:
                # ---- 加载并缩放 map（wz 原图自动处理）----
                map_img = self._load_map_for_pack(route_dir, mm)
                if map_img is not None:
                    mh, mw = map_img.shape[:2]
                    # route 同步缩放到 map 尺寸
                    norm_imgs = []
                    for r in route_imgs:
                        if r.shape[:2] != (mh, mw):
                            r = cv2.resize(r, (mw, mh),
                                           interpolation=cv2.INTER_NEAREST)
                        norm_imgs.append(r)
                    route_imgs = norm_imgs

                # ★ 玩家模板 → 传给 route_nav
                player_tpl_np = None
                if tpl_path and os.path.isfile(tpl_path):
                    player_tpl_np = imread_u(tpl_path, cv2.IMREAD_COLOR)

                self.log(f"[reload] 即将 load: "
                         f"map_img={'✓'+str(map_img.shape) if map_img is not None else '✗'} "
                         f"routes={len(route_imgs)}", "info")
                self.route_nav.load(route_imgs, map_bgr=map_img)

                ui_h = p.get("ui_y_start", 640)
                self.route_nav.configure(
                    ui_y_start=ui_h,
                    player_tpl_np=player_tpl_np,
                    map_offset_x=p.get("map_offset", [0, 0])[0],
                    map_offset_y=p.get("map_offset", [0, 0])[1],
                )
                self._route_path_loaded = rp
                self._nav_base = map_img

                n = len(route_imgs)
                mode = "大map" if map_img is not None else "小地图"
                self.log(f"颜色路线已加载（{n} 条，{mode}坐标系）："
                         f"{os.path.basename(route_dir)}", "info")

        if self.move is not None:
            self.move.bind(self.cfg["keys"])

        self._th_cache = None

    def _load_map_for_pack(self, pack_dir, mm):
        """加载地图包底图

        缩放规则：
          · wz 原图 → 按 ROI 宽度等比缩放（高度自动算，避免黑边拉伸）
          · 无 wz → 用 map.png
        """
        wz_path = os.path.join(pack_dir, "minimap_wz.png")
        w = int(mm.get("w", 0))
        if os.path.isfile(wz_path) and w > 5:
            wz = imread_u(wz_path)
            if wz is not None:
                wz_w, wz_h = wz.shape[1], wz.shape[0]
                # ★ 只用 ROI 宽度，高度按 wz 原比例算
                target_w = w
                target_h = int(wz_h * target_w / wz_w)
                if (wz_w, wz_h) == (target_w, target_h):
                    return wz
                map_img = cv2.resize(wz, (target_w, target_h),
                                     interpolation=cv2.INTER_LANCZOS4)
                self.log(f"🔍 wz {wz_w}x{wz_h} → map "
                         f"{target_w}x{target_h}（等比）", "info")
                return map_img

        return imread_u(os.path.join(pack_dir, "map.png"))

    def load_route(self, route_bgr, path_tag="memory"):
        """加载路线（单张或列表；列表 = 多路线循环）"""
        if route_bgr is None:
            return
        if isinstance(route_bgr, list):
            route_list = [r for r in route_bgr if r is not None]
        else:
            route_list = [route_bgr]
        if not route_list:
            return
        # 尺寸统一到小地图区域
        mm = self.cfg.get("patrol", {}).get("minimap", {})
        mw, mh = mm.get("w", 0), mm.get("h", 0)
        if mw > 4:
            for i, r in enumerate(route_list):
                if (r.shape[1], r.shape[0]) != (mw, mh):
                    route_list[i] = cv2.resize(r, (mw, mh),
                                               interpolation=cv2.INTER_NEAREST)
        self.route_nav.load(route_list)
        self._route_path_loaded = path_tag

    def invalidate_route_cache(self):
        """让 reload_runtime 下次一定重新读路线文件"""
        self._route_path_loaded = None

    def clear_route(self):
        """清空路线导航（销毁定位线程，重建一个空的）"""
        self.route_nav.on_log = None
        self.route_nav = ColorRouteNavigator()
        self.route_nav.on_log = self.log
        self._route_path_loaded = None
        self._nav_base = None

    def set_mode(self, mode):
        """切换运行模式"""
        self.mode = mode
        self.status["mode"] = mode
        if mode != Mode.RUNNING:
            self.move.release_all()
        if mode == Mode.RUNNING and self.capture.bring_foreground():
            time.sleep(0.1)

    def calibrate_bar_color(self, which, frame):
        """校准 HP/MP/EXP 条颜色"""
        bar = getattr(self, f"{which}_bar", None)
        return bar.calibrate_color(frame) if (bar is not None and frame is not None) else None

    def shutdown(self):
        """停止所有线程 + 释放按键 + 关闭 capture"""
        self._stop_flag = True
        self._kb_stop = True
        self._det_thread_stop = True
        self._nav_thread_stop = True        # ★ 新增：停导航线程
        try:
            if self.move is not None:
                self.move.release_all()
        except Exception:
            pass
        self._close_capture()

    # ============ 键盘线程 ============
    def _keyboard_loop(self):
        """键盘线程主循环：30 FPS 消费命令队列 / 检查目标状态变化

        ★ 关键：本线程独立于主循环。即使 _tick 卡 200ms（小地图匹配、
          模板识别），按键下发节奏依然是 30 FPS，游戏里角色移动不会顿。
        """
        interval = 1.0 / self.KB_FPS
        print("[kb] 键盘线程启动")
        while not self._kb_stop:
            t0 = time.time()
            try:
                if self.move is not None and hasattr(self.move, "tick"):
                    self.move.tick()
            except Exception as e:
                import traceback
                print(f"[kb] move.tick 异常: {e}\n{traceback.format_exc()}")
            try:
                if self.controller is not None and hasattr(self.controller, "tick"):
                    self.controller.tick()
            except Exception as e:
                import traceback
                print(f"[kb] controller.tick 异常: {e}\n{traceback.format_exc()}")
            dt = time.time() - t0
            if dt < interval:
                time.sleep(interval - dt)

    # ============ 识别线程 ============
    def _det_loop(self):
        """识别独立线程：不断消费 _det_frame_slot，跑 _detect 更新结果"""
        while not self._det_thread_stop:
            with self._det_lock:
                frame = self._det_frame_slot
                self._det_frame_slot = None
            if frame is None:
                time.sleep(0.02)
                continue
            try:
                monsters, player = self._detect(frame)
                # ★ 补跑快扫（识别线程里做，不阻塞主循环）
                if player is not None:
                    quick = self._quick_attack_scan(frame, player)
                    if quick:
                        monsters = list(monsters) + quick
            except Exception:
                monsters, player = [], None
            with self._det_lock:
                self._det_monsters_latest = monsters
                self._det_player_latest = player

    def _nav_loop(self):
        """导航线程主循环：异步跑玩家定位

        ============================================================
        【工作方式】
        ============================================================
        每轮：
          ① 从 _nav_frame_slot 取一帧（覆盖式，没有就等一下）
          ② 跑 route_nav.player_pos(frame) 得到地图坐标
          ③ 把结果写入 _nav_latest

        ============================================================
        【异常处理】
        ============================================================
        player_pos 内部可能抛 cv2.error（匹配失败/尺寸异常）。
        捕获后写入 None，让主循环下一帧拿到"定位失败"，
        与同步版本行为一致（不会因为一个异常卡死线程）。
        """
        while not self._nav_thread_stop:
            # ① 取帧
            with self._nav_lock:
                frame = self._nav_frame_slot
                self._nav_frame_slot = None
            if frame is None:
                time.sleep(0.02)
                continue

            # ② 跑定位
            try:
                has_bigmap = getattr(self.route_nav, "_map_bgr", None) is not None
                has_minimap = self.route_nav.minimap[2] > 4
                if has_bigmap or has_minimap:
                    pos = self.route_nav.player_pos(frame)
                else:
                    pos = None
            except Exception:
                pos = None

            # ③ 写结果
            with self._nav_lock:
                self._nav_latest = pos

    # ============ 主循环 ============
    def run(self):
        """引擎主线程"""
        try:
            while not self._stop_flag:
                if self.mode == Mode.IDLE:
                    time.sleep(0.1)
                    continue
                try:
                    self._tick()
                except Exception as e:
                    import traceback
                    self.log(f"引擎异常: {e}\n{traceback.format_exc()}", "error")
                    self.move.release_all()
                    time.sleep(0.5)
                # 目标 30 FPS：本帧耗时 < 33ms 就补足；> 33ms 就不 sleep
                elapsed = time.time() - self._t0
                if elapsed < 0.033:
                    time.sleep(0.033 - elapsed)
        finally:
            self.move.release_all()

    def _tick(self):
        """主循环单帧处理：截图 → 血蓝条 → 识别 → 决策 → 预览"""
        _T = {}
        self._t0 = time.time()
        frame = self.capture.screenshot()
        if frame is None:
            self._none_frames += 1
            if self._none_frames >= 8:
                self.move.release_all()
                if self.mode == Mode.RUNNING:
                    self.set_mode(Mode.PAUSED)
                    self.log("连续截图失败（窗口最小化/关闭？），自动暂停", "error")
            time.sleep(0.1)
            return

        # ★ 锁定画布尺寸：第一帧记下，之后每帧 resize 回去，
        #   彻底消除窗口边框/DPI 抖动导致的"预览忽大忽小"
        if self._canvas_size is None:
            self._canvas_size = (frame.shape[1], frame.shape[0])
        elif (frame.shape[1], frame.shape[0]) != self._canvas_size:
            frame = cv2.resize(frame, self._canvas_size,
                               interpolation=cv2.INTER_LINEAR)
        _T["截图"] = time.time() - self._t0

        self._none_frames = 0
        with self._frame_lock:
            self._frame = frame
        self._update_fps()

        st = self.status
        now = time.time()

        # ① 资源监测：每 3 帧算一次（喝药判定 100ms 延迟没影响）
        _t = time.time()
        self._bar_tick = getattr(self, "_bar_tick", 0) + 1
        if self._bar_tick % 5 == 1 or self._bar_cache is None:
            hp, mp = self.hp_bar.percentage(frame), self.mp_bar.percentage(frame)
            exp = self.exp_bar.percentage(frame)
            self._bar_cache = (hp, mp, exp)
        else:
            hp, mp, exp = self._bar_cache
        st["hp"], st["mp"], st["exp"] = hp, mp, exp
        _T["血蓝条"] = time.time() - _t

        # ② 失焦安全 + 自动喝药
        focused = True
        potion_used = False
        if self.mode == Mode.RUNNING:
            focused = self._check_focus()
            if not focused:
                self.move.release_all()
            else:
                potion_used = self._auto_potion(hp, mp)
                self._auto_timers()  # ★ 定时按键：宠物药 + BUFF

        # ③ 全屏识别（独立线程，异步）
        _t = time.time()
        with self._det_lock:
            self._det_frame_slot = frame
            monsters = self._det_monsters_latest
            player = self._det_player_latest
        self._last_monsters, self._last_player = monsters, player
        _T["怪物识别"] = time.time() - _t

        # ④ 玩家定位（异步：投喂帧 + 读上一帧结果）
        #
        # 【和旧版的区别】
        # 旧版：本帧投喂，本帧等结果 → 主循环阻塞 5~15ms
        # 新版：本帧投喂，读上一帧结果 → 主循环几乎不花时间
        #
        # 【结果滞后】
        # player_map 是上一帧（33ms 前）的结果。
        # 对巡逻/战斗没影响：玩家 33ms 最多移动 ~20px，
        # 攻击框 220px 的范围完全覆盖这点误差。
        _t = time.time()
        with self._nav_lock:
            self._nav_frame_slot = frame      # 投喂本帧
            player_map = self._nav_latest     # 读上一帧结果
        self._last_map_pos = player_map
        _T["玩家定位"] = time.time() - _t

        # ⑤ 决策
        _t = time.time()
        if self.mode == Mode.RUNNING:
            if potion_used:
                st["action"] = "❤ 喝药优先中…"
            else:
                in_out_proc = self._logout_login_tick(now)
                resting = self._schedule_tick(now) if not in_out_proc else False
                if in_out_proc:
                    if self._logged_out:
                        st["action"] = "🚪 已下线，等待定时上线…"
                    else:
                        st["action"] = "⏳ 下线/上线流程中…"
                elif resting:
                    self.move.release_all()
                    if self._rest_phase == "to_auction":
                        st["action"] = "🛒 进拍卖场中…"
                    elif self._rest_phase == "in_auction":
                        remain = max(0.0, self._rest_until - now)
                        st["action"] = (f"😴 拍卖场休息中 "
                                        f"剩{int(remain // 60)}分"
                                        f"{int(remain % 60):02d}秒")
                    elif self._rest_phase == "from_auction":
                        st["action"] = "☕ 退出拍卖场中…"
                    elif self._resting:
                        remain = max(0.0, self._rest_until - now)
                        st["action"] = (f"😴 休息中 剩{int(remain // 60)}分"
                                        f"{int(remain % 60):02d}秒")
                    else:
                        st["action"] = f"🧗 {getattr(self, '_rest_status', '走向安全点…')}"
                elif focused:
                    st["action"] = self._decide(frame, monsters, player, player_map)
                    if self._grace_until and now < self._grace_until:
                        st["action"] += " · 寻找安全点"
                else:
                    st["action"] = "失焦保护中"
        else:
            st["action"] = "已暂停" if self.mode == Mode.PAUSED else "监控中"
        _T["决策"] = time.time() - _t

        # ⑥ 预览绘制
        _t = time.time()
        if now - self._last_preview >= 0.10:      # 15 Hz → 10 Hz
            self._last_preview = now
            ann = frame.copy()
            self._draw_bar(ann, self.cfg.get("hp_bar", {}), (70, 70, 255), "HP")
            self._draw_bar(ann, self.cfg.get("mp_bar", {}), (255, 170, 60), "MP")
            self._draw_bar(ann, self.cfg.get("exp_bar", {}), (60, 220, 255), "EXP")
            self._draw_boxes(ann, player=player[:2] if player else None)
            self._draw_patrol(ann, player_map)
            self._push_preview(ann)

    # ============ 内部实现 ============
    def _check_focus(self):
        """检查游戏窗口是否失焦；失焦时暂停按键"""
        if not self.cfg["options"].get("pause_on_unfocus", True):
            return True
        # ★ 每 5 帧查一次
        self._focus_tick = getattr(self, "_focus_tick", 0) + 1
        if self._focus_tick % 5 != 1:
            return self._focus_cache
        self._focus_cache = self.capture.is_foreground()
        if self._focus_cache:
            return True
        now = time.time()
        if now - self._last_warn > 4:
            self._last_warn = now
            self.log("游戏窗口失焦，暂停按键输出", "warn")
        return False

    def _thresholds(self):
        if self._th_cache is not None:
            return self._th_cache
        raw = self.cfg.get("thresholds", {}) or {}
        out = {}
        for k, v in raw.items():
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = v
        self._th_cache = out
        return out

    def _set_facing(self, direction, force=False):
        """统一设置目标朝向：同时更新 move 的目标方向 + 脚本侧 _facing

        ============================================================
        【为什么需要这个方法】
        ============================================================
        旧代码里"改变朝向"分散在多处，每处只调 move.set_dir，
        忘了同步 self._facing。这导致：
          · 脚本以为角色朝右，实际游戏里朝左
          · 攻击前 need_turn 判断错误 → 多等/少等一次方向键
          · 表现：攻击时机被打乱，偶尔打空

        本方法把"改朝向"收口到一个入口，任何地方改朝向都走这里，
        就不会再漏。

        ============================================================
        【force 参数的用途】
        ============================================================
        force=False（默认）：
            普通移动更新方向，调 move.set_dir(dir)
            键盘线程下次 tick 检查 _target_dir 变化才会按键
            → 省按键，避免"方向没变还重复按"

        force=True：
            强制重按方向键，调 move.repress_dir(dir)
            先释放旧方向键，再按新方向键
            → 用于攻击前，保证游戏里角色朝向 = 目标方向
               （游戏朝向由"最后按下的方向键"决定，
                仅靠"按住"无法在被打退/动作后恢复朝向）

        ============================================================
        【direction=0 的处理】
        ============================================================
        0 表示"松开方向键"（停止移动）。
        此时方向键松开、角色朝向保持（游戏机制），
        所以 _facing 不更新。

        ============================================================
        参数：
            direction: -1 左 / +1 右 / 0 停
            force    : True → 强制重按方向键（攻击前用）
        """
        if force:
            self.move.repress_dir(direction)
        else:
            self.move.set_dir(direction)
        # 只有真正给出方向才更新朝向；0 是"松开"，朝向保持
        if direction != 0:
            self._facing = direction

    def _auto_potion(self, hp, mp):
        """自动喝药；返回 True 表示本帧发出了喝红药指令"""
        keys, th = self.cfg["keys"], self._thresholds()
        hp_used = False
        # ★ 喝药冷却随机化（±10% 抖动），避免固定节奏被反外挂识别
        hp_cd = th["potion_cooldown"] * random.uniform(0.3, 0.8)
        if 0 <= hp <= th["hp_potion"] and self.controller.cooldown_ok("hp", hp_cd):
            self.controller.tap(keys.get("hp_potion"))
            self.log(f"❤ HP {hp:.0f}% → 喝红药", "info")
            hp_used = True
        if 0 <= mp <= th["mp_potion"] and self.controller.cooldown_ok("mp", th["potion_cooldown"]):
            self.controller.tap(keys.get("mp_potion"))
            self.log(f"💧 MP {mp:.0f}% → 喝蓝药", "info")
        if self.cfg["options"].get("stop_on_low_hp") and 0 <= hp <= th.get("hp_stop", 12):
            self.set_mode(Mode.PAUSED)
            self.log(f"‼ 血量过低({hp:.0f}%)，触发停机保护！", "error")
        return hp_used

    def _auto_timers(self):
        """定时按键：宠物药 + 5 个 BUFF

        每个按键用 controller 的冷却池管理；间隔从 cfg["timers"] 读。
        按键留空 = 该功能禁用。
        """
        keys = self.cfg["keys"]
        timers = self.cfg.get("timers", {})

        # ---- 宠物药 ----
        pet_key = keys.get("pet_potion")
        pet_itv = float(timers.get("pet_potion", 600))
        if pet_key and pet_itv > 0:
            if self.controller.cooldown_ok("pet_potion", pet_itv):
                self.controller.tap(pet_key)
                self.log(f"🐾 宠物药（{pet_itv:.0f}s）", "info")

        # ---- 5 个 BUFF ----
        for i in range(1, 6):
            bkey = keys.get(f"buff{i}")
            itv = float(timers.get(f"buff{i}", 180))
            if bkey and itv > 0:
                if self.controller.cooldown_ok(f"buff{i}", itv):
                    self.controller.tap(bkey)
                    self.log(f"✨ BUFF{i}（{itv:.0f}s）", "info")

    def _detect(self, frame):
        """怪物识别 + 玩家定位（在识别线程里执行）

        ============================================================
        【本版新增：玩家模板局部搜索】
        ============================================================
        旧版：玩家模板每次全屏匹配（1368×800）
        新版：用上一帧位置 ± 200px 局部搜索（400×400）

        为什么可以用局部搜索：
          · 玩家位置在相邻帧之间变化很小（跑得再快也就几十像素）
          · 200px 的 pad 足够覆盖"高速移动 + 击退"等异常情况

        为什么用户模板要局部而怪物模板反而用更宽的框：
          · 玩家模板只有 1 个，全屏代价大，局部收益高
          · 怪物模板可能出现在玩家周围各个方向，攻击框已经覆盖了合理范围

        ============================================================
        【三层玩家搜索范围（按优先级）】
        ============================================================
        ① 有上一帧位置 → 局部 ±200px（最快，首选）
        ② 无上一帧但有 detect_region → 用 detect_region
        ③ 都没有 → 全屏（首次启动、位置丢失时的兜底）
        """
        # 本帧识别框缓存（供预览绘制用）
        self._last_boxes = []
        # 相似度阈值
        th = self.cfg["thresholds"]["match"]
        # 用户配置的可选检测区域
        region = self.cfg.get("detect_region")

        # 本帧识别结果
        monsters = []   # [(hit, name), ...]
        player = None   # (cx, cy, conf, w, h) 或 None

        # ============================================================
        # ① 决定玩家搜索区域
        # ============================================================
        H, W = frame.shape[:2]
        prev = self._last_player
        px_off = py_off = 0

        if prev is not None:
            # ---- 情况 ①：有上一帧位置 → 局部 ±200px ----
            ax, ay = int(prev[0]), int(prev[1])
            pad = 200
            rx = max(0, ax - pad)
            ry = max(0, ay - pad)
            rx1 = min(W, ax + pad)
            ry1 = min(H, ay + pad)
            # 局部窗口太小（玩家贴边）→ 退回全屏，避免匹配失败
            if (rx1 - rx) > 60 and (ry1 - ry) > 60:
                player_scene = frame[ry:ry1, rx:rx1]
                px_off, py_off = rx, ry
            else:
                player_scene = frame
        elif region and region[2] > 8 and region[3] > 8:
            # ---- 情况 ②：无上一帧但有 detect_region ----
            rx, ry, rw, rh = region
            player_scene = frame[ry:ry + rh, rx:rx + rw]
            px_off, py_off = rx, ry
        else:
            # ---- 情况 ③：全屏（首次启动兜底）----
            player_scene = frame

        # ============================================================
        # ② 玩家模板匹配
        # ============================================================
        if self._player_tpl and self._player_tpl in self.detector.templates:
            # 灰度转换：局部区域（几百 KB），比全屏快很多
            player_gray = cv2.cvtColor(player_scene, cv2.COLOR_BGR2GRAY)
            hits = self.detector.find_all(
                self._player_tpl, th,
                scene_gray=player_gray,
                offset=(px_off, py_off))   # 加偏移还原到整帧坐标
            if hits:
                player = hits[0]
                self._last_boxes.append(
                    (*self._box_of(hits[0]), (90, 255, 90), "PLAYER"))

        # ============================================================
        # ③ 玩家模板未命中 → 名字条兜底
        # ============================================================
        if player is None:
            nt_pos = self._detect_nametag(frame)
            if nt_pos is not None:
                player = nt_pos
                cx, cy = nt_pos[0], nt_pos[1]
                self._last_boxes.append(
                    (int(cx - 10), int(cy - 15), 20, 30,
                     (0, 255, 255), "NAME"))

        # ============================================================
        # ④ 计算怪物扫描框（攻击框 ± margin）
        # ============================================================
        scan_box = self._attack_scan_box(player) if player else None

        # 玩家未知 → 退回 detect_region 或全屏
        if scan_box is None:
            if region and region[2] > 8 and region[3] > 8:
                scan_box = (region[0], region[1],
                            region[0] + region[2],
                            region[1] + region[3])
            else:
                scan_box = (0, 0, W, H)

        # 边界裁剪
        x0 = max(0, int(scan_box[0]))
        y0 = max(0, int(scan_box[1]))
        x1 = min(W, int(scan_box[2]))
        y1 = min(H, int(scan_box[3]))
        rw, rh = x1 - x0, y1 - y0

        # ============================================================
        # ⑤ 怪物模板匹配（只在扫描框内）
        # ============================================================
        if rw > 20 and rh > 20:
            monster_scene = frame[y0:y1, x0:x1]
            monster_gray = cv2.cvtColor(monster_scene, cv2.COLOR_BGR2GRAY)

            # 遍历所有怪物模板（list() 拷贝键，防热重载时 dict 变化）
            for name in list(self.detector.templates):
                if name == self._player_tpl:
                    continue
                hits = self.detector.find_all(
                    name, th,
                    scene_gray=monster_gray,
                    offset=(x0, y0))
                for h in hits:
                    monsters.append((h, name))
                    self._last_boxes.append(
                        (*self._box_of(h),
                         (60, 100, 255), name[:10]))

        return monsters, player

    def _quick_attack_scan(self, frame, player):
        """攻击框内低延迟补扫（缩小扫描范围，比全屏识别快得多）"""
        if player is None or self.detector is None:
            return []
        templates = list(self.detector.templates)
        if not templates:
            return []
        th = self._thresholds()
        ax, ay = float(player[0]), float(player[1])
        ar = float(th.get("attack_range", 160))
        sr = float(th.get("skill_range", 60))
        pad = 24
        fh, fw = frame.shape[:2]
        bx0 = int(max(0, ax - ar - pad))
        bx1 = int(min(fw, ax + ar + pad))
        by0 = int(max(0, ay - sr - pad))
        by1 = int(min(fh, ay + sr + pad))
        if bx1 - bx0 < 16 or by1 - by0 < 16:
            return []
        roi = frame[by0:by1, bx0:bx1]
        if roi.size == 0:
            return []
        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        match_sim = float(th.get("match", 0.90))
        hits = []
        for name in templates:
            if name == self._player_tpl:
                continue
            try:
                hh = self.detector.find_all(
                    name, match_sim, scene_gray=roi_gray, offset=(bx0, by0))
            except Exception:
                continue
            for h in hh:
                hits.append((h, name))
        return hits

    def _detect_nametag(self, frame):
        """名字条兜底定位（玩家模板未命中时用）

        三个方法逐步尝试，找到最高分。
        """
        if self._player_tpl is None or self._player_tpl not in self.detector.templates:
            return None
        tpl = self.detector.templates.get(self._player_tpl)
        if tpl is None:
            return None
        h, w = tpl.gray.shape[:2]
        gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(gray_frame, tpl.gray, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        # 方法 2：白遮罩
        if max_val < 0.65:
            blur_frame = cv2.GaussianBlur(gray_frame, (3, 3), 0)
            blur_tpl = cv2.GaussianBlur(tpl.gray, (3, 3), 0)
            _, mask_frame = cv2.threshold(blur_frame, 150, 255, cv2.THRESH_BINARY)
            _, mask_tpl = cv2.threshold(blur_tpl, 150, 255, cv2.THRESH_BINARY)
            res2 = cv2.matchTemplate(mask_frame, mask_tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val2, _, max_loc2 = cv2.minMaxLoc(res2)
            if max_val2 > max_val:
                max_val = max_val2
                max_loc = max_loc2
        # 方法 3：直方图均衡化
        if max_val < 0.55:
            eq_frame = cv2.equalizeHist(gray_frame)
            eq_tpl = cv2.equalizeHist(tpl.gray)
            res3 = cv2.matchTemplate(eq_frame, eq_tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val3, _, max_loc3 = cv2.minMaxLoc(res3)
            if max_val3 > max_val:
                max_val = max_val3
                max_loc = max_loc3
        if max_val < 0.5:
            return None
        cx = max_loc[0] + w // 2
        cy = max_loc[1] + h + 18
        return (cx, cy, max_val, w, h)

    def _decide(self, frame, monsters, player, player_map):
        """每帧决策：完全对齐参考项目 HuntingState.on_frame 的"覆盖式"架构

        流程：
          ① 巡逻指令 → 写入 (cmd_x, cmd_y, cmd_action)
          ② 战斗检测 → 有怪能打就"覆盖"cmd_action/cmd_x
          ③ 卡住脱困 → 覆盖所有 cmd
          ④ 统一应用三个 cmd 到 move/controller
        """
        now = time.time()

        # ---- 脱困锁定期 ----
        if now < self._unstuck_until:
            keys = self.cfg["keys"]
            self.move.set_climb(None)
            self._set_facing(self._unstuck_dir)    # ← 改
            if self.controller.cooldown_ok("unstuck_jump", 0.4):
                self.controller.tap(keys.get("jump"))
            return "🎲 脱困中…"

        # ---- 随机走神 ----
        if now < self._idle_until:
            self.move.release_all()
            return "🤔 稍作停顿"
        if now >= self._next_idle_t:
            self._idle_until = now + random.uniform(1.0, 3.0)
            self._next_idle_t = now + random.uniform(300, 500)
            return "🤔 稍作停顿"

        keys, th = self.cfg["keys"], self._thresholds()
        patrol = self.cfg.get("patrol", {})
        patrol_on = patrol.get("enabled") and self.route_nav.ready

        # ============================================================
        # ★ 三个共享 cmd 字段（对应参考项目的 self.bot.cmd_move_x/y/action）
        # ============================================================
        cmd_x = 0  # -1/0/+1
        cmd_y = None  # "up"/"down"/None
        cmd_action = None  # "attack"/"jump"/"teleport"/None
        status = "巡逻找怪"

        # ============================================================
        # ① 巡逻指令（参考项目 update_cmd_by_route）
        # ============================================================
        if patrol_on:
            patrol_cmd = self.route_nav.step(player_map, now)
            if patrol_cmd is not None:
                cmd_x = patrol_cmd.lr or 0
                cmd_y = patrol_cmd.ud
                status = patrol_cmd.status
                if patrol_cmd.act in ("jump", "teleport"):
                    cmd_action = patrol_cmd.act
                if patrol_cmd.act == "stop":
                    self.move.release_all()
                    return patrol_cmd.status

        # ============================================================
        # ② 战斗指令（参考项目 update_cmd_by_mob_detection）—— 覆盖 ①
        #    注意：没有 can_fight / allow_stand / chase_range，
        #          只要怪在"攻击搜索框内"，冷却 OK 就打
        # ============================================================
        if monsters and player is not None:
            # 搜索框 = 攻击范围 + margin（对齐参考项目）
            margin = 30
            dx = float(th.get("attack_range", 160)) + margin
            dy = float(th.get("skill_range", 80)) + margin
            px, py = float(player[0]), float(player[1])

            near = [m for m in monsters
                    if abs(m[0][0] - px) <= dx
                    and abs(m[0][1] - py) <= dy]

            if near:
                lr, act = self._update_cmd_by_mob_detection(near, player, now)
                if act == "attack":
                    # ============================================
                    # ★ 攻击前先确保朝向正确
                    # ============================================
                    # 【核心思路】
                    # 不再依赖 self._facing（可能和游戏实际朝向不同步），
                    # 而是每次攻击前"强制重按"方向键，让游戏内朝向 = 目标方向。
                    #
                    # 【为什么每次都要重按】
                    # 冒险岛角色的朝向 = "最后按下的方向键"。
                    # 即使方向键已经按着，如果角色因受伤/攻击动画/被击退
                    # 改变了朝向，继续"按住"不会让它重新朝向目标方向。
                    # → 必须"释放 → 重按"一次，游戏才能确认朝向。
                    #
                    # 【等待确认】
                    # repress_dir 是异步的：主线程更新状态后，
                    # 真正按键由键盘线程在下一次 tick（33ms 后）下发。
                    # 所以调用后要轮询 current_dir_key()，
                    # 直到确认方向键真的按下再攻击。
                    if lr is not None:
                        # 攻击时松开上下键（爬绳状态下不攻击）
                        self.move.set_climb(None)

                        # ★ 强制重按方向键（走 _set_facing 统一入口）
                        #   force=True → 内部调 repress_dir
                        #   同时 _facing 也在这里被同步更新
                        self._set_facing(lr, force=True)

                        # 目标方向键名
                        want_key = self.move._k(
                            "move_left" if lr == -1 else "move_right")

                        # 轮询等待方向键真正按下（最多 100ms 兜底）
                        # 100ms 是按 30 FPS 键盘线程算的 3 个 tick，
                        # 足够覆盖"释放 + 重按"的完整时序
                        deadline = time.time() + 0.10
                        while time.time() < deadline:
                            if self.move.current_dir_key() == want_key:
                                break
                            time.sleep(0.005)   # 5ms 轮询，避免忙等

                        # ★ 调试：每次攻击前打一次（每秒最多 1 次）
                        now2 = time.time()
                        if now2 - getattr(self, "_turn_dbg_t", -99.0) > 1.0:
                            self._turn_dbg_t = now2
                            got = self.move.current_dir_key()
                            self.log(
                                f"[TURN] want={want_key!r} "
                                f"got={got!r} "
                                f"waited={0.10 - (deadline - now2):.3f}s",
                                "info")

                    # ---- 方向已确认，执行攻击 ----
                    self._do_attack()
                    self.route_nav.touch()   # 打怪时刷新停滞计时
                    return f"⚔ 攻击({'左' if self._facing < 0 else '右'})"

        # ============================================================
        # ③ 应用三个 cmd 到实际控制器
        # ============================================================
        self.move.set_climb(cmd_y)
        self._set_facing(cmd_x)      # ← 改（cmd_x=0 时不改 _facing，符合预期）

        if cmd_action == "attack":
            self._do_attack()
            self.route_nav.touch()
        elif cmd_action == "jump":
            self.controller.tap(keys.get("jump"))
        elif cmd_action == "teleport":
            dkey = keys.get(cmd_y) if cmd_y in ("up", "down") else \
                keys.get("move_right" if cmd_x == 1 else "move_left")
            self.controller.combo(keys.get("teleport"), dkey, hold=0.25)

        # 拾取 + 脱困（只在非战斗、非瞬移时做）
        if cmd_action not in ("attack", "teleport"):
            self._try_loot(now)
            if self.route_nav.ready and self.route_nav.stuck_seconds() > 1.5 \
                    and self.controller.cooldown_ok("unstuck", 1.2):
                rcmd = self.route_nav.random_cmd()
                self._unstuck_dir = rcmd.lr or random.choice([-1, 1])
                self._unstuck_until = now + 1.5
                self._set_facing(self._unstuck_dir)  # ← 改
                self.move.set_climb(None)
                self.controller.tap(keys.get("jump"))
                self.route_nav.touch()
                self.log("巡逻停滞 → 随机脱困", "warn")
                return rcmd.status

        return status

    # ============ 攻击判定（参考项目 get_attack_range / get_nearest_monster /
    #              get_attack_direction / update_cmd_by_mob_detection）============
    def _get_attack_range(self, player, is_left=True):
        """攻击判定框

        · attack_range          → 水平范围（normal: 单侧；skill: 双侧）
        · skill_range           → 垂直总高度
        · attack_bottom_offset  → 底边相对玩家 Y 的偏移（★ 新增，控制台可调）
            - 0  → 底边正好在玩家 Y（你要的效果）
            - >0 → 底边下移 N px，覆盖脚下同平台的怪
            - <0 → 底边上移 N px，只打更高处的怪
        """
        if player is None:
            return (0, 0, 0, 0)
        px, py = float(player[0]), float(player[1])
        th = self._thresholds()
        mode = self.cfg["keys"].get("attack_mode", "normal")

        rw = float(th.get("attack_range", 160))
        rh = float(th.get("skill_range", 80))
        bottom_off = float(th.get("attack_bottom_offset", 0))

        # ---- 水平 ----
        if mode == "skill":
            x0, x1 = px - rw, px + rw
        else:
            if is_left:
                x0, x1 = px - rw, px
            else:
                x0, x1 = px, px + rw

        # ---- 垂直：底边 = 玩家 Y + 偏移，从底边向上展开 rh ----
        y1 = py + bottom_off
        y0 = y1 - rh

        return (x0, y0, x1, y1)

    def _get_nearest_monster(self, monsters, player, is_left=True):
        """攻击框内最近的怪

        · 只保留与攻击框交叠面积 ≥ mob_overlap_area 的怪
        · 用曼哈顿距离选最近
        返回 (hit, name) 或 None
        """
        if player is None or not monsters:
            return None
        x0, y0, x1, y1 = self._get_attack_range(player, is_left)
        px, py = float(player[0]), float(player[1])

        # ★ 动态阈值：取"最小怪物模板面积的一半"与"配置上限"中较小者
        min_mob_area = 99999
        for tpl in self.detector.templates.values():
            a = tpl.h * tpl.w
            if a < min_mob_area:
                min_mob_area = a
        max_trigger = float(self.cfg["thresholds"].get("max_mob_area_trigger", 200))
        thres = min(min_mob_area * 0.5, max_trigger)

        nearest, min_d = None, float("inf")
        for m in monsters:
            hit, name = m[0], m[1]
            mx, my = hit[0], hit[1]
            mw = hit[3] if len(hit) > 3 else 10
            mh = hit[4] if len(hit) > 4 else 10
            mx1, my1 = mx - mw // 2, my - mh // 2
            mx2, my2 = mx + mw // 2, my + mh // 2
            ix1, iy1 = max(x0, mx1), max(y0, my1)
            ix2, iy2 = min(x1, mx2), min(y1, my2)
            iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
            if iw * ih < thres:
                continue
            d = abs(mx - px) + abs(my - py)
            if d < min_d:
                min_d, nearest = d, m
        return nearest

    def _attack_scan_box(self, player, margin=20):
        """根据玩家位置 + 当前攻击范围参数，返回怪物识别搜索框

        【攻击框长什么样】
        normal 模式下攻击框分左右两个：
          · 左框：[px-ar, px]      ×  [py-sr, py+off]
          · 右框：[px, px+ar]      ×  [py-sr, py+off]
        其中：ar = attack_range（水平范围）
              sr = skill_range（垂直总高）
              off = attack_bottom_offset（底边相对玩家 Y 的偏移）

        【识别怪物需要覆盖两侧】
        因为怪物可能在左也可能在右，所以扫描区域必须同时覆盖左框+右框，
        并集就是：
          · 水平：[px-ar, px+ar]  （左右各 ar 像素）
          · 垂直：[py-sr, py+off] （上方 sr，下方 off）

        【margin 的作用】
        给扫描框加一点边缘余量，避免怪物"刚刚进入攻击框边缘"时
        因为模板中心还在框外而被漏检。默认 20px，可根据需要调整。

        参数：
            player: (px, py, conf, w, h) 玩家位置；None 时返回 None
            margin: 边缘余量像素（默认 20）

        返回：
            (x0, y0, x1, y1) 或 None（玩家未知时）
        """
        if player is None:
            return None
        px, py = float(player[0]), float(player[1])
        th = self._thresholds()
        ar = float(th.get("attack_range", 160))     # 水平范围
        sr = float(th.get("skill_range", 220))      # 垂直总高
        off = float(th.get("attack_bottom_offset", 0))  # 底边偏移

        x0 = int(px - ar - margin)   # 左边界：玩家左边 ar+margin
        x1 = int(px + ar + margin)   # 右边界：玩家右边 ar+margin
        y0 = int(py - sr - margin)   # 上边界：玩家上方 sr+margin
        y1 = int(py + off + margin)  # 下边界：玩家下方 off+margin
        return (x0, y0, x1, y1)

    def _get_attack_direction(self, ml, mr, player):
        """攻击方向决策

        · 只一侧有怪 → 打那侧
        · 两侧都有怪 → 位置合法的那侧优先
        · 两侧都合法且距离差 < 50 → 返回 None（避免左右横跳）
        """
        px = float(player[0])

        def _valid(m, side):
            if m is None:
                return False
            mx = m[0][0]
            return (mx < px) if side == "left" else (mx > px)

        def _dist(m):
            if m is None:
                return float("inf")
            return abs(m[0][0] - px) + abs(m[0][1] - player[1])

        vl, vr = _valid(ml, "left"), _valid(mr, "right")
        dl, dr = _dist(ml), _dist(mr)

        if vl and not vr:
            return "left"
        if vr and not vl:
            return "right"
        if vl and vr:
            # ★ 两侧都有怪时直接选近的一侧（不再返回 None）
            return "left" if dl <= dr else "right"
        return None

    def _update_cmd_by_mob_detection(self, monsters, player, now):
        """一次决策产出攻击指令，返回 (lr, act)

        lr  : -1 左转 / 1 右转 / None 不转身
        act : "attack" / None
        """
        if not monsters:
            return None, None

        mode = self.cfg["keys"].get("attack_mode", "normal")
        th = self._thresholds()
        key = "atk_cd_skill" if mode == "skill" else "atk_cd_normal"
        rng = th.get(key, [0.20, 0.25])
        cd = random.uniform(float(rng[0]), float(rng[1]))
        if not self.controller.cooldown_ok("atk", cd):
            # ★ 调试
            if now - getattr(self, "_atk_dbg_t", -99) > 1.0:
                self._atk_dbg_t = now
                self.log(f"[ATK-DBG] 冷却未到 cd={cd:.3f}", "info")
            return None, None

        ml = self._get_nearest_monster(monsters, player, is_left=True)
        mr = self._get_nearest_monster(monsters, player, is_left=False)
        direction = self._get_attack_direction(ml, mr, player)

        # ★ 调试：每秒一次
        if now - getattr(self, "_atk_dbg_t", -99) > 1.0:
            self._atk_dbg_t = now
            px, py = player[0], player[1]
            self.log(
                f"[ATK-DBG] mode={mode} player=({px:.0f},{py:.0f}) "
                f"thr={th.get('attack_range')}/{th.get('skill_range')} "
                f"mobs={len(monsters)} ml={ml and ml[0][:2]} mr={mr and mr[0][:2]} "
                f"dir={direction}", "info")

        if direction is None:
            return None, None

        lr = -1 if direction == "left" else 1
        return lr, "attack"

    def _do_attack(self):
        """执行一次攻击按键

        【随机按住时长】
        每次攻击的按住时长在 40~80ms 之间随机，原因：
          · 固定时长容易被游戏反外挂系统识别为"脚本节奏"
          · 随机抖动让每次按键都略有差异（人类手指按压本身就不均匀）
          · 参考项目也用 pydirectinput + 随机 pause 做按键抖动

        【攻击模式】
        · normal → 按"普通攻击"键（config.keys.attack）
        · skill  → 从 skill1/skill2/skill3 里随机挑一个
                   实现了简单的"技能轮换"效果
        """
        keys = self.cfg["keys"]
        mode = keys.get("attack_mode", "normal")

        # ★ 随机按住时长：40~80ms
        #   注意：这里必须是 random.uniform 而不是固定值
        #   原代码写死 0.05 会丧失随机性，本次已修复
        hold = random.uniform(0.04, 0.08)

        # 调试日志：记录本次攻击信息（按住时长可见）
        # 每秒最多打一次，避免刷屏
        now = time.time()
        if now - getattr(self, "_atk_log_t", -99.0) > 1.0:
            self._atk_log_t = now
            self.log(
                f"[攻击] mode={mode} key={keys.get('attack')!r} "
                f"hold={hold * 1000:.0f}ms", "warn")

        if mode == "skill":
            # 技能模式：从已配置的技能键中随机挑一个
            skills = [keys[k] for k in ("skill1", "skill2", "skill3")
                      if keys.get(k)]
            if skills:
                pick = random.choice(skills)
                self.controller.tap(pick, hold=hold)
                return

        # 普通攻击模式
        key = keys.get("attack")
        if key:
            self.controller.tap(key, hold=hold)

    def _try_loot(self, now):
        """自动拾取（可关）"""
        if not self.cfg["options"].get("loot_enabled", True):
            return
        key = self.cfg["keys"].get("pickup")
        if not key:
            return
        if now - self._loot_t < self._thresholds().get("pickup_interval", 0.9):
            return
        self._loot_t = now
        self.controller.tap(key, hold=0.04)

    def _schedule_tick(self, now):
        """定时休息调度（拍卖场 / 绳子 两种模式）

        返回 True = 正在休息/准备休息 → 主循环跳过战斗和巡逻
        返回 False = 正常挂机中
        """
        sch = self.cfg.get("schedule", {})
        if not sch.get("enabled"):
            # 未启用 → 清状态，正常挂机
            self._rest_phase = "idle"
            self._resting = False
            self._sess_end = None
            self._grace_until = None
            return False

        # ============================================================
        # ① 已在休息流程中 → 优先处理，直接 return True
        # ============================================================

        # 拍卖场 3 相位
        if self._rest_phase == "to_auction":
            return self._tick_to_auction(now)
        if self._rest_phase == "in_auction":
            return self._tick_in_auction(now)
        if self._rest_phase == "from_auction":
            return self._tick_from_auction(now)

        # 绳子模式：已在休息
        if self._resting:
            if now >= self._rest_until:
                self._resting = False
                self._start_session(now)
                self.log("😴 休息结束，开始新一轮挂机", "ok")
            return True

        # ============================================================
        # ② 检查是否该开始休息（会话到点）
        # ============================================================
        if self._sess_end is None:
            self._start_session(now)
            return False
        if now < self._sess_end:
            return False

        # ---- 到点了 → 启动休息流程 ----
        mode = sch.get("rest_mode", "rope")
        if mode == "auction":
            # 拍卖场：点击进拍卖，进入 to_auction 相位
            if self._grace_until is None:
                self._grace_until = now
                self.log("⏰ 本轮挂机结束，准备进拍卖场休息…", "info")
                self._auction_enter_begin(now)
            return True

        # 绳子模式：走安全点
        return self._tick_walk_to_rope(now, sch)

    def _tick_to_auction(self, now):
        """拍卖场：已点击"进"按钮，等待界面加载

        返回 True（主循环保持在休息状态）
        """
        au = self.cfg.get("schedule", {}).get("auction", {})
        wait = float(au.get("enter_wait", 3.0))
        if now - self._rest_phase_t >= wait:
            # 界面加载完成 → 进入休息计时
            self._rest_phase = "in_auction"
            self._rest_phase_t = now
            sch = self.cfg.get("schedule", {})
            lo = float(sch.get("rest_lo_min", 5))
            hi = float(sch.get("rest_hi_min", 10))
            self._rest_until = now + random.uniform(lo, hi) * 60
            self.log(f"😴 进入拍卖场休息 "
                     f"{int((self._rest_until - now) // 60)} 分钟", "warn")
        self.move.release_all()
        return True

    def _tick_in_auction(self, now):
        """拍卖场：正在休息中，到点退出

        返回 True
        """
        self.move.release_all()
        if now >= self._rest_until:
            self._auction_exit_begin(now)
        return True

    def _tick_from_auction(self, now):
        """拍卖场：已点击"退"按钮，等待回到游戏

        返回 True
        """
        au = self.cfg.get("schedule", {}).get("auction", {})
        wait = float(au.get("exit_wait", 3.0))
        if now - self._rest_phase_t >= wait:
            # 已回到游戏 → 开始新会话
            self._rest_phase = "idle"
            self._start_session(now)
            self.log("✅ 已退出拍卖场，开始新一轮挂机", "ok")
        self.move.release_all()
        return True

    def _tick_walk_to_rope(self, now, sch):
        """绳子模式：到点后走安全点，到了就进休息

        返回 True（走的过程中也算"休息准备中"）
        """
        if self._grace_until is None:
            self._grace_until = now + float(sch.get("safe_stop_wait", 120))
            self._rest_rope = None
            self.log("本轮挂机结束，走向最近绳子(安全点)…", "info")

        # 超时 or 没有位置信息 → 原地休息
        if now >= self._grace_until or not self._last_map_pos:
            self._begin_rest(now)
            return True

        px, py = self._last_map_pos
        if self._rest_rope is None:
            self._rest_rope = self.route_nav.nearest_rope_for_rest(px, py)
        rope = self._rest_rope

        if rope is None or rope[0] is None:
            self._begin_rest(now)
            return True

        top_y, bottom_y, cx = rope
        if top_y <= py <= bottom_y:
            # 已到绳子 → 进休息
            self._begin_rest(now)
            return True

        # 还在走 → 下发移动指令
        cmd = self.route_nav.move_to_rope_cmd(px, py, rope, now)
        self._exec_route_cmd(cmd)
        self._rest_status = cmd.status
        # 走安全点卡住 → 跳跃脱困
        if self.route_nav.stuck_seconds() > 1.5:
            keys = self.cfg["keys"]
            if self.controller.cooldown_ok("rest_jump", 1.5):
                self.controller.tap(keys.get("jump"))
            self.route_nav.touch()
        return True

    def _today_ts(self, hhmm):
        """把 HH:MM 转成今天的 Unix 时间戳"""
        try:
            h, m = hhmm.split(":")
            h, m = int(h), int(m)
        except Exception:
            return None
        t = _dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
        return t.timestamp()

    def _logout_login_tick(self, now):
        """定时下线/上线"""
        sch = self.cfg.get("schedule", {})
        today = _dt.date.today().toordinal()

        if sch.get("logout_enabled") and not self._logged_out and \
                self._logout_done_today != today:
            if self._logout_deadline is None:
                base = self._today_ts(sch.get("logout_time", "23:00"))
                if base is not None:
                    self._logout_deadline = base + random.uniform(180, 480)
                    self.log(f"⏰ 定时下线将于 {_dt.datetime.fromtimestamp(self._logout_deadline).strftime('%H:%M:%S')} 执行", "info")
            if self._logout_deadline is not None and now >= self._logout_deadline:
                self._do_logout()
                self._logout_done_today = today
                self._login_done_today = None
                return True

        if self._logged_out and sch.get("login_enabled") and \
                self._login_done_today != today:
            if self._login_deadline is None:
                base = self._today_ts(sch.get("login_time", "08:00"))
                if base is not None:
                    if base < now:
                        base += 86400
                    self._login_deadline = base + random.uniform(180, 480)
                    self.log(f"⏰ 定时上线将于 {_dt.datetime.fromtimestamp(self._login_deadline).strftime('%H:%M:%S')} 执行", "info")
            if self._login_deadline is not None and now >= self._login_deadline:
                self.log("⌨ 执行上线：按 Enter…", "info")
                keys = self.cfg["keys"]
                self.controller.tap(keys.get("enter"))
                self._login_deadline = None
            if self._last_player is not None:
                self._logged_out = False
                self._login_done_today = today
                self.log("✅ 检测到玩家，上线成功，恢复巡逻", "ok")
            return True

        return False

    def _do_logout(self):
        """执行下线：Esc → ↑ → Enter"""
        self.log("⌨ 执行下线：Esc → ↑ → Enter…", "warn")
        self.move.release_all()
        keys = self.cfg["keys"]
        self.controller.tap(keys.get("escape"))
        time.sleep(1)
        self.controller.tap(keys.get("up"))
        time.sleep(1)
        self.controller.tap(keys.get("enter"))
        self._logged_out = True
        self._logout_deadline = None
        self.log("🚪 已发送下线指令", "warn")

    def _start_session(self, now):
        """开始一轮挂机（时长随机 ±3 分钟）"""
        sch = self.cfg.get("schedule", {})
        base = float(sch.get("duration_min", 60)) * 60
        self._sess_end = now + base + random.uniform(-180, 180)
        self._grace_until = None
        self.log(f"⏱ 本轮挂机约 {int((self._sess_end - now) // 60)} 分钟（随机±3分钟）", "info")

    def _begin_rest(self, now):
        """进入休息"""
        sch = self.cfg.get("schedule", {})
        lo, hi = float(sch.get("rest_lo_min", 5)), float(sch.get("rest_hi_min", 10))
        self._resting = True
        self._rest_until = now + random.uniform(lo, hi) * 60
        self._sess_end = None
        self._grace_until = None
        self._rest_rope = None
        self.move.release_all()
        self.log(f"😴 进入休息 {int((self._rest_until - now) // 60)} 分钟"
                 f"（血蓝监控保持运行）", "warn")

    def _click_game(self, x, y, jitter=8):
        """在游戏窗口相对坐标 (x, y) 点击鼠标左键（带随机抖动 ±jitter）

        依赖 capture 实现了 click(x, y) 方法。
        """
        if not self.window_bound():
            return False
        if not hasattr(self.capture, "click"):
            self.log("⚠ 当前 capture 未实现 click() 方法", "warn")
            return False
        jx = random.randint(-jitter, jitter)
        jy = random.randint(-jitter, jitter)
        try:
            return bool(self.capture.click(int(x + jx), int(y + jy)))
        except Exception as e:
            self.log(f"⚠ 点击失败: {e}", "warn")
            return False

    def _auction_enter_begin(self, now):
        """触发进拍卖：点击 + 切到 to_auction 相位"""
        sch = self.cfg.get("schedule", {})
        au = sch.get("auction", {})
        x = int(au.get("enter_x", 1220))
        y = int(au.get("enter_y", 745))
        jitter = int(au.get("click_jitter", 8))
        self.move.release_all()
        if not self._click_game(x, y, jitter):
            self.log("⚠ 点击进拍卖失败，退回原地休息", "warn")
            self._begin_rest(now)  # 兜底：原地休息
            return
        self._rest_phase = "to_auction"
        self._rest_phase_t = now
        self.log("🛒 点击进入拍卖场…", "info")

    def _auction_exit_begin(self, now):
        """触发退拍卖：点击右上角退出 或 ESC+Enter"""
        sch = self.cfg.get("schedule", {})
        au = sch.get("auction", {})
        if au.get("use_esc_to_exit", False):
            keys = self.cfg["keys"]
            self.controller.tap(keys.get("escape"))
            time.sleep(0.3)
            self.controller.tap(keys.get("enter"))
        else:
            x = int(au.get("exit_x", 1230))
            y = int(au.get("exit_y", 25))
            jitter = int(au.get("click_jitter", 8))
            self._click_game(x, y, jitter)
        self._rest_phase = "from_auction"
        self._rest_phase_t = now
        self.log("☕ 退出拍卖场…", "info")

    def _exec_route_cmd(self, cmd):
        """执行路线指令（休息安全点走向绳子时调用）"""
        self._set_facing(cmd.lr or 0)     # ← 改
        self.move.set_climb(cmd.ud)

    @staticmethod
    def _box_of(hit):
        """识别结果 → 包围盒 (x, y, w, h)"""
        cx, cy, _, w, h = hit
        return (cx - w // 2, cy - h // 2, w, h)

    def _draw_boxes(self, ann, player=None):
        """在预览帧上画攻击框 + 识别框"""
        fh, fw = ann.shape[:2]

        # ---- 攻击框 ----
        if player is not None and len(player) >= 2:
            ax, ay = float(player[0]), float(player[1])
            if 0 <= ax < fw and 0 <= ay < fh:
                mode = self.cfg["keys"].get("attack_mode", "normal")

                if mode == "skill":
                    # AOE：画一个对称框
                    box = self._get_attack_range((ax, ay), is_left=True)
                    x0, y0, x1, y1 = box
                    x0i, y0i = int(max(0, x0)), int(max(0, y0))
                    x1i, y1i = int(min(fw - 1, x1)), int(min(fh - 1, y1))
                    if x1i - x0i > 4 and y1i - y0i > 4:
                        overlay = ann.copy()
                        cv2.rectangle(overlay, (x0i, y0i), (x1i, y1i),
                                      (0, 0, 255), -1)
                        cv2.addWeighted(overlay, 0.10, ann, 0.90, 0, ann)
                        cv2.rectangle(ann, (x0i, y0i), (x1i, y1i),
                                      (0, 0, 255), 1)
                        cv2.putText(ann, "AOE", (x0i + 4, y0i + 14),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
                else:
                    # directional：左右各画一个框（青色 = 左，黄色 = 右）
                    for is_left, color, tag in ((True, (255, 200, 0), "L"),
                                                (False, (0, 220, 255), "R")):
                        box = self._get_attack_range((ax, ay), is_left=is_left)
                        x0, y0, x1, y1 = box
                        x0i, y0i = int(max(0, x0)), int(max(0, y0))
                        x1i, y1i = int(min(fw - 1, x1)), int(min(fh - 1, y1))
                        if x1i - x0i > 4 and y1i - y0i > 4:
                            overlay = ann.copy()
                            cv2.rectangle(overlay, (x0i, y0i), (x1i, y1i),
                                          color, -1)
                            cv2.addWeighted(overlay, 0.08, ann, 0.92, 0, ann)
                            cv2.rectangle(ann, (x0i, y0i), (x1i, y1i),
                                          color, 1)
                            cv2.putText(ann, tag, (x0i + 4, y0i + 14),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
        # # 在 _draw_boxes 里，画完攻击框后加一行
        # th = self._thresholds()
        # off = float(th.get("attack_bottom_offset", 0))
        # cv2.putText(ann, f"bottom_off={off:+.0f}",
        #             (int(ax) - 40, int(ay) + int(off) + 14),
        #             cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)

        # ---- 识别框 ----
        for entry in self._last_boxes:
            if len(entry) < 6:
                continue
            x1, y1, w, h, color, label = entry
            x1i, y1i = int(x1), int(y1)
            wi, hi = int(w), int(h)
            if x1i < 0:
                wi += x1i
                x1i = 0
            if y1i < 0:
                hi += y1i
                y1i = 0
            x2i = min(fw - 1, x1i + wi)
            y2i = min(fh - 1, y1i + hi)
            if x2i <= x1i + 2 or y2i <= y1i + 2:
                continue
            if (x2i - x1i) * (y2i - y1i) > 0.5 * fh * fw:
                continue
            cv2.rectangle(ann, (x1i, y1i), (x2i, y2i), color, 2)
            cv2.putText(ann, str(label)[:12], (x1i, max(12, y1i - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

    @staticmethod
    def _draw_bar(ann, bar, color, label):
        """在预览帧上画血蓝条区域框"""
        if bar.get("w", 0) > 0:
            x, y, w, h = bar["x"], bar["y"], bar["w"], bar["h"]
            cv2.rectangle(ann, (x - 2, y - 2), (x + w + 2, y + h + 2), color, 1)
            cv2.putText(ann, label, (x, max(10, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)

    def _push_preview(self, ann):
        """预览帧入队（覆盖旧帧）"""
        while True:
            try:
                self.preview_queue.get_nowait()
            except queue.Empty:
                break
        self.preview_queue.put(ann)

    def _update_fps(self):
        """每秒统计一次 FPS"""
        self._fps_n += 1
        now = time.time()
        if now - self._fps_t >= 1.0:
            self.status["fps"] = self._fps_n
            self._fps_n = 0
            self._fps_t = now

    def grab_frames(self, n=6, interval=0.12):
        """连续抓 N 帧（用于小地图黄点颜色采样）"""
        frames = []
        for _ in range(n):
            f = self.capture.screenshot()
            if f is not None:
                frames.append(f)
            time.sleep(interval)
        return frames

    def sample_dot_color(self, frames):
        """从多帧中采样小地图黄点颜色"""
        return self.route_nav.auto_sample(frames)

    def set_nav_base(self, img):
        """设置滚动补偿底图"""
        self._nav_base = img
        self.route_nav.set_base(img)

    NAV_W = 176

    def _draw_patrol(self, ann, player_map):
        """NAV 面板：显示玩家周围 80×80 局部地图（放大 3 倍）

        照搬参考项目 update_info_on_img_frame_debug：
          · 裁剪玩家周围 80x80 区域 → 放大 3 倍
          · 玩家黄十字固定在面板中央附近
          · 贴右上角，白边框
        """
        map_bgr = getattr(self.route_nav, "_map_bgr", None)
        if map_bgr is None:
            return

        # 合成图：map 底图 + route 线条
        panel = map_bgr.copy()
        route = self.route_nav.route
        if route is not None and route.shape[:2] == panel.shape[:2]:
            nz = route.max(axis=2) > 40
            panel[nz] = route[nz]
        map_h, map_w = panel.shape[:2]

        CROP_W, CROP_H = 80, 80
        ZOOM = 3

        # 没玩家位置 → 跳过（防止"整张 map 占满屏幕"）
        if player_map is None or len(player_map) < 2:
            return

        px, py = int(player_map[0]), int(player_map[1])
        x0 = max(0, px - CROP_W // 2)
        y0 = max(0, py - CROP_H // 2)
        x1 = min(map_w, x0 + CROP_W)
        y1 = min(map_h, y0 + CROP_H)
        if x1 - x0 < CROP_W:
            x0 = max(0, x1 - CROP_W)
        if y1 - y0 < CROP_H:
            y0 = max(0, y1 - CROP_H)

        crop = panel[y0:y1, x0:x1].copy()
        rel_x = px - x0
        rel_y = py - y0

        # 放大 3 倍
        ch, cw = crop.shape[:2]
        disp = cv2.resize(crop, (cw * ZOOM, ch * ZOOM),
                          interpolation=cv2.INTER_NEAREST)
        dh, dw = disp.shape[:2]

        # 尺寸超限自动缩
        ah, aw = ann.shape[:2]
        if dw > aw - 20 or dh > ah - 20:
            s = min((aw - 20) / dw, (ah - 20) / dh)
            dw = int(dw * s)
            dh = int(dh * s)
            disp = cv2.resize(disp, (dw, dh),
                              interpolation=cv2.INTER_AREA)

        # 贴右上角
        x_paste = max(0, aw - dw - 10)
        y_paste = 10
        if x_paste + dw > aw:
            x_paste = aw - dw
        if y_paste + dh > ah:
            y_paste = ah - dh
        ann[y_paste:y_paste + dh, x_paste:x_paste + dw] = disp

        # 白边框
        cv2.rectangle(ann, (x_paste - 1, y_paste - 1),
                      (x_paste + dw, y_paste + dh),
                      (255, 255, 255), 2)

        # 玩家黄点：实心菱形 + 黑描边（仿 mini 地图黄点风格）
        sx = dw / max(1, cw)
        sy = dh / max(1, ch)
        mx = int(x_paste + rel_x * sx)
        my = int(y_paste + rel_y * sy)
        if x_paste <= mx < x_paste + dw and y_paste <= my < y_paste + dh:
            DIAMOND_R = 10     # 菱形半径（面板像素），可调
            pts = np.array([
                [mx, my - DIAMOND_R],          # 上
                [mx + DIAMOND_R, my],          # 右
                [mx, my + DIAMOND_R],          # 下
                [mx - DIAMOND_R, my],          # 左
            ], np.int32)
            # 描边 + 黄色填充（先画大菱形，再画小菱形）
            cv2.fillConvexPoly(ann, pts, (0, 255, 255))
            inner = np.array([
                [mx, my - DIAMOND_R + 2],
                [mx + DIAMOND_R - 2, my],
                [mx, my + DIAMOND_R - 2],
                [mx - DIAMOND_R + 2, my],
            ], np.int32)
            cv2.fillConvexPoly(ann, inner, (0, 255, 255))

        # 圈数标记
        if self.route_nav.laps:
            cv2.putText(ann, f"LAP {self.route_nav.laps}",
                        (x_paste, min(ah - 4, y_paste + dh + 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (255, 255, 255), 1, cv2.LINE_AA)