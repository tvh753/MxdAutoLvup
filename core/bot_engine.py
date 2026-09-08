# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:06
# @Author  : yy
# @File    : bot_engine.py
# @Software: MxdAutoLvup

"""截图 → 资源监测 → 模板识别 → 战斗决策 → 按键输出（独立线程）"""
import time
import threading
import queue
import cv2
import os
import random
import numpy as np

from core.window_capture import WindowCapture
from core.detector import TemplateDetector
from core.resource_monitor import BarMonitor
from core.action_controller import ActionController, MovementController
# from core.patrol import PatrolNavigator, WALK, JUMP, ROPE_UP, ROPE_DOWN
from core.color_route import ColorRouteNavigator
from core.rope_detector import RopeDetector
from core.imio import imread_u
from core.config_manager import PLAYER_TPL_PATH, resolve_player_path


class Mode:
    IDLE = "idle";
    PREVIEW = "preview";
    RUNNING = "running";
    PAUSED = "paused"


class BotEngine(threading.Thread):
    DET_INTERVAL = 0.06  # 怪物识别节流: v15 从0.12降到0.06 (≈16.7FPS)，提高流畅度

    def __init__(self, cfg, log_fn):
        super().__init__(daemon=True, name="BotEngine")
        self.cfg = cfg  # 共享配置引用 → GUI 修改即时热生效
        self.log = log_fn
        self.capture = WindowCapture()
        self.detector = TemplateDetector()
        self.controller = ActionController()

        self._chase_t0 = None  # 追击起始时间
        self._giveup_until = None  # 放弃追击截止时间

        self.move = MovementController()  # 连续移动状态机
        self._det_t = 0.0  # 识别节流计时
        self._last_monsters, self._last_player = [], None
        self._last_boxes = []  # 识别标注缓存（每帧重绘用）
        self._none_frames = 0

        self.hp_bar = BarMonitor("hp")
        self.mp_bar = BarMonitor("mp")
        self.exp_bar = BarMonitor("exp")
        self.preview_queue = queue.Queue(maxsize=2)

        self.route_nav = ColorRouteNavigator()  # 颜色路径导航（取代路点式）
        self.rope_det = RopeDetector()  # 主画面绳子识别（精调对位）
        self._rope_t = 0.0
        self._route_path_loaded = None
        self._offroute_log_t = 0.0
        self.route_nav.on_log = self.log  # 导航器诊断日志 → 控制台日志
        self._nav_base = None

        self.mode = Mode.IDLE
        self._stop_flag = False
        self._frame = None
        self._frame_lock = threading.Lock()
        self._player_tpl = None

        self.status = {"hp": -1.0, "mp": -1.0, "exp": -1.0, "fps": 0, "monsters": 0,
                       "action": "-", "mode": Mode.IDLE}

        self._atk_idx = 0;
        self._roam_dir = 1;
        self._roam_t = 0.0
        self._last_warn = 0.0;
        self._last_preview = 0.0
        self._fps_n = 0;
        self._fps_t = time.time()

        self._facing = 1  # 角色朝向（+1右/-1左）
        self._skill_idx = 0
        self._loot_t = 0.0
        self._was_combat = False
        self._last_map_pos = None
        self._resting = False  # 定时休息状态
        self._rest_until = 0.0
        self._sess_end = None
        self._grace_until = None

    # ============ 对外接口 ============
    def bind_window(self, keyword) -> bool:
        if self.capture.bind(keyword):
            self.reload_runtime()
            return True
        return False

    def window_bound(self) -> bool:
        return self.capture.hwnd is not None

    def latest_frame(self):
        with self._frame_lock:
            return None if self._frame is None else self._frame.copy()

    def reload_runtime(self):
        """模板 / 血蓝条配置变更后热重载"""
        import time as _time
        now = _time.time()
        self.detector.clear()
        for item in self.cfg.get("monster_templates", []):
            try:
                self.detector.load(item["name"], item["path"])
            except Exception as e:
                self.log(f"模板加载失败 [{item.get('name')}]: {e}", "warn")

        # ---- 玩家模板（全局体系 + 路径归一化兜底） ----
        from core.config_manager import PLAYER_TPL_PATH, resolve_player_path
        pt = self.cfg.get("player_template")
        tpl_path = resolve_player_path(pt.get("path")) if pt else None
        if tpl_path is None:
            # 路径无效：回退全局模板
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
                if pt and pt.get("path"):
                    try:
                        self.detector.load(pt["name"], pt["path"])
                        self._player_tpl = pt["name"]
                    except Exception as e:
                        self.log(f"玩家模板加载失败: {e}", "warn")
            except Exception as e:
                self.log(f"玩家模板加载失败: {e}", "warn")

        # ---- v19: 小地图黄点模板（独立于游戏画面玩家模板）----
        # 注意：这里不再复用 player_template 路径！
        # 小地图里的黄点只是几个像素的黄色点，和游戏画面里的玩家本体完全不同，
        # 必须用专门截图的黄点模板图做匹配才能准确定位。
        dot_tpl_path = None
        dt = self.cfg.get("dot_template")
        if dt:
            p = dt.get("path")
            if p and os.path.isfile(p):
                dot_tpl_path = p
        if dot_tpl_path is None:
            # 兜底：检查约定路径 templates/dot/dot.png
            from core.config_manager import TEMPLATE_DIR
            import os as _os
            _fallback = os.path.join(TEMPLATE_DIR, "dot", "dot.png")
            if os.path.isfile(_fallback):
                dot_tpl_path = _fallback
                self.cfg["dot_template"] = {"name": "黄点", "path": _fallback}
        if dot_tpl_path and dot_tpl_path != self.route_nav.dot_tpl_path:
            self.route_nav.configure(dot_tpl_path=dot_tpl_path)
            self.log(f"📍 小地图黄点模板: {dot_tpl_path}", "info")
        elif not dot_tpl_path:
            # 没有黄点模板：30秒提示一次，不要每帧刷屏
            if now - getattr(self, "_no_dot_tpl_log_t", -99.0) > 30.0:
                self._no_dot_tpl_log_t = now
                self.log("⚠ 未配置小地图黄点模板(dot_template)。"
                         "请在GUI参数页设置，或把模板图片放到 templates/dot/dot.png。"
                         "小地图定位将无法工作", "warn")

        for key, bar in (("hp_bar", self.hp_bar),
                         ("mp_bar", self.mp_bar),
                         ("exp_bar", self.exp_bar)):
            d = self.cfg.get(key, {})
            bar.set(region=(d.get("x", 0), d.get("y", 0),
                            d.get("w", 0), d.get("h", 0)))

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
        if rp and rp != self._route_path_loaded:
            img = imread_u(rp)
            if img is None:
                self.log(f"路线图不存在或读取失败: {rp}（重新绘制并保存路线可修复）", "warn")
                self._nav_base = None
            else:
                mw, mh = mm.get("w", 0), mm.get("h", 0)
                if mw > 4 and (img.shape[1], img.shape[0]) != (mw, mh):
                    ow, oh = img.shape[1], img.shape[0]
                    img = cv2.resize(img, (mw, mh), interpolation=cv2.INTER_NEAREST)
                    self.log(f"路线图尺寸({ow}x{oh})与小地图({mw}x{mh})不符，"
                             f"已自动缩放适配；若路线错位请重绘", "warn")
                self.route_nav.load(img)
                self._route_path_loaded = rp
                base = imread_u(os.path.join(os.path.dirname(rp), "minimap.png"))
                self._nav_base = base if (base is not None and
                                          base.shape[:2] == img.shape[:2]) else None
                self.route_nav.set_base(self._nav_base)  # 滚动补偿需要底图
                self.log(f"颜色路线已加载：{os.path.basename(os.path.dirname(rp))}"
                         + ("" if self._nav_base is not None else "（无小地图底图）"))
        self.move.bind(self.cfg["keys"])

    def load_route(self, route_bgr, path_tag="memory"):
        if route_bgr is None:
            return
        mm = self.cfg.get("patrol", {}).get("minimap", {})
        mw, mh = mm.get("w", 0), mm.get("h", 0)
        if mw > 4 and (route_bgr.shape[1], route_bgr.shape[0]) != (mw, mh):
            route_bgr = cv2.resize(route_bgr, (mw, mh),
                                   interpolation=cv2.INTER_NEAREST)
            self.log("路线图与小地图尺寸不一致，已自动缩放适配（若错位请重绘）", "warn")
        self.route_nav.load(route_bgr)
        self._route_path_loaded = path_tag

    def invalidate_route_cache(self):
        """切换地图包前调用，强制 reload 重新读文件"""
        self._route_path_loaded = None

    def clear_route(self):
        """删除地图包后调用：路线/底图/玩家点锁定全部复位"""
        self.route_nav.on_log = None
        self.route_nav = ColorRouteNavigator()
        self.route_nav.on_log = self.log
        self._route_path_loaded = None
        self._nav_base = None

    def _anchor_x(self, frame):
        p = self._last_player
        return p[0] if p is not None else frame.shape[1] // 2

    def _rope_assist(self, frame, cmd, now):
        """对准阶段：主画面绳子精调（小地图缩放误差兜底）。
        v9 收紧：仅 align 阶段干预——grab 起跳后覆盖方向/打回对位
        会造成空中横移、松开↑，正是“跳起来抓不到绳”的元凶；
        玩家模板未检出时锚点=画面中心不可靠，同样不干预"""
        if self.route_nav.phase != "align":
            return
        if now - self._rope_t < 0.25:
            return
        self._rope_t = now
        p = self._last_player
        if p is None:
            return
        ropes = self.rope_det.find(frame, self.cfg.get("detect_region"))
        if not ropes:
            return
        best = min(ropes, key=lambda r: abs(r[0] - p[0]))
        dx = best[0] - p[0]
        if abs(dx) > 28:
            cmd.dir = -1 if dx < 0 else 1

    def set_mode(self, mode):
        self.mode = mode
        self.status["mode"] = mode
        if mode != Mode.RUNNING:
            self.move.release_all()  # ⚠ 任何停机都必须松键防失控
        if mode == Mode.RUNNING and self.capture.bring_foreground():
            time.sleep(0.1)

    def calibrate_bar_color(self, which, frame):
        """校准后调用：按实测颜色微调对应状态条的识别区间"""
        bar = getattr(self, f"{which}_bar", None)
        return bar.calibrate_color(frame) if (bar is not None and frame is not None) else None

    def shutdown(self):
        self._stop_flag = True

    # ============ 主循环 ============
    def run(self):
        try:
            while not self._stop_flag:
                if self.mode == Mode.IDLE:
                    time.sleep(0.1)
                    continue
                try:
                    self._tick()
                except Exception as e:
                    self.log(f"引擎异常: {e}", "error")
                    self.move.release_all()
                    time.sleep(0.5)
                time.sleep(0.004)
        finally:
            self.move.release_all()  # 线程退出必松键

    def _tick(self):
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
        self._none_frames = 0
        with self._frame_lock:
            self._frame = frame
        self._update_fps()

        ann = frame.copy()
        st = self.status
        now = time.time()

        # ① 资源监测
        hp, mp = self.hp_bar.percentage(frame), self.mp_bar.percentage(frame)
        st["hp"], st["mp"] = hp, mp
        st["exp"] = self.exp_bar.percentage(frame)
        self._draw_bar(ann, self.cfg.get("hp_bar", {}), (70, 70, 255), "HP")
        self._draw_bar(ann, self.cfg.get("mp_bar", {}), (255, 170, 60), "MP")
        self._draw_bar(ann, self.cfg.get("exp_bar", {}), (60, 220, 255), "EXP")

        # ② 失焦安全 + 自动喝药（休息中也喝药保命）
        focused = True
        if self.mode == Mode.RUNNING:
            focused = self._check_focus()
            if not focused:
                self.move.release_all()
            else:
                self._auto_potion(hp, mp)

        # ③ 目标识别（节流）+ 小地图定位（每帧）
        if now - self._det_t >= self.DET_INTERVAL:
            self._last_monsters, self._last_player = self._detect(frame)
            self._det_t = now
        monsters, player = self._last_monsters, self._last_player
        self._draw_boxes(ann, player=player[:2] if player else None)  # v16: 传递玩家位置用于绘制攻击框
        st["monsters"] = len(monsters)

        player_map = self.route_nav.player_pos(frame) \
            if self.route_nav.minimap[2] > 4 else None
        self._last_map_pos = player_map
        self._draw_patrol(ann, player_map)

        # ④ 决策（含定时休息调度）
        if self.mode == Mode.RUNNING:
            resting = self._schedule_tick(now)
            if resting:
                self.move.release_all()
                remain = max(0.0, self._rest_until - now)
                st["action"] = f"😴 休息中 剩{int(remain // 60)}分{int(remain % 60):02d}秒"
            elif focused:
                st["action"] = self._decide(frame, monsters, player, player_map)
                if self._grace_until and now < self._grace_until:
                    st["action"] += " · 寻找安全点"
            else:
                st["action"] = "失焦保护中"
        else:
            st["action"] = "已暂停" if self.mode == Mode.PAUSED else "监控中"

        self._push_preview(ann)

    # ============ 内部实现 ============
    def _check_focus(self):
        if not self.cfg["options"].get("pause_on_unfocus", True):
            return True
        if self.capture.is_foreground():
            return True
        now = time.time()
        if now - self._last_warn > 4:
            self._last_warn = now
            self.log("游戏窗口失焦，暂停按键输出（点回游戏窗口自动恢复）", "warn")
        return False

    def _auto_potion(self, hp, mp):
        keys, th = self.cfg["keys"], self.cfg["thresholds"]
        if 0 <= hp <= th["hp_potion"] and self.controller.cooldown_ok("hp", th["potion_cooldown"]):
            self.controller.tap(keys.get("hp_potion"))
            self.log(f"❤ HP {hp:.0f}% → 喝红药", "info")
        if 0 <= mp <= th["mp_potion"] and self.controller.cooldown_ok("mp", th["potion_cooldown"]):
            self.controller.tap(keys.get("mp_potion"))
            self.log(f"💧 MP {mp:.0f}% → 喝蓝药", "info")
        if self.cfg["options"].get("stop_on_low_hp") and 0 <= hp <= th.get("hp_stop", 12):
            self.set_mode(Mode.PAUSED)
            self.log(f"‼ 血量过低({hp:.0f}%)，触发停机保护！", "error")

    def _detect(self, frame):
        """模板识别（重操作，由 _tick 节流调用）；标注框存缓存供每帧重绘

        v16改进:
          1. 灰度图转换只做一次，所有模板共享
          2. nametag检测: 用铭牌模板匹配玩家位置（替代血条颜色检测）
          3. 多源融合: 怪物模板匹配 + 名牌模板匹配 → 更可靠的玩家位置
        """
        self._last_boxes = []
        th = self.cfg["thresholds"]["match"]
        region = self.cfg.get("detect_region")
        x = y = 0
        scene = frame
        if region and region[2] > 8 and region[3] > 8:
            rx, ry, rw, rh = region
            scene = frame[ry:ry + rh, rx:rx + rw]
            x, y = rx, ry

        monsters, player = [], None
        if self.detector.templates:
            # v15: 灰度图转换只做一次，所有模板共享
            gray = cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY)
            for name in self.detector.templates:
                hits = self.detector.find_all(name, th, scene_gray=gray, offset=(x, y))
                if not hits:
                    continue
                if name == self._player_tpl:
                    player = hits[0]
                    self._last_boxes.append((*self._box_of(hits[0]), (90, 255, 90), "PLAYER"))
                else:
                    for h in hits:
                        monsters.append((h, name))
                        self._last_boxes.append((*self._box_of(h), (60, 100, 255), name[:10]))

        # v16: 名牌检测作为玩家位置补充（参考项目 get_player_location_by_nametag）
        if player is None:
            nt_pos = self._detect_nametag(frame)
            if nt_pos is not None:
                player = nt_pos
                cx, cy = nt_pos[0], nt_pos[1]
                self._last_boxes.append((int(cx - 10), int(cy - 15), 20, 30, (0, 255, 255), "NAME"))

        return monsters, player

    def _detect_nametag(self, frame):
        """v16: 通过玩家名牌(nametag)模板匹配定位玩家

        参考项目 approach:
          1. 从名牌模板图像获取模板
          2. 使用多种模式匹配（白口罩/灰度/直方图均衡）
          3. 选择最佳匹配，计算玩家位置

        返回: (cx, cy, conf, w, h) 或 None
        """
        # 检查是否有玩家模板
        if self._player_tpl is None or self._player_tpl not in self.detector.templates:
            return None

        tpl = self.detector.templates.get(self._player_tpl)
        if tpl is None:
            return None

        h, w = tpl.gray.shape[:2]

        # 方法1: 直接灰度匹配（最快）
        gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(gray_frame, tpl.gray, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

        # 如果匹配度不够，尝试白口罩模式
        if max_val < 0.65:
            # 方法2: 白色区域匹配（参考项目 white_mask 模式）
            # 高斯模糊
            blur_frame = cv2.GaussianBlur(gray_frame, (3, 3), 0)
            blur_tpl = cv2.GaussianBlur(tpl.gray, (3, 3), 0)

            # 创建白色掩码 (灰度>150的像素作为名牌)
            _, mask_frame = cv2.threshold(blur_frame, 150, 255, cv2.THRESH_BINARY)
            _, mask_tpl = cv2.threshold(blur_tpl, 150, 255, cv2.THRESH_BINARY)

            res2 = cv2.matchTemplate(mask_frame, mask_tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val2, _, max_loc2 = cv2.minMaxLoc(res2)

            # 选择更好的匹配
            if max_val2 > max_val:
                max_val = max_val2
                max_loc = max_loc2

        # 方法3: 直方图均衡化（兜底）
        if max_val < 0.55:
            eq_frame = cv2.equalizeHist(gray_frame)
            eq_tpl = cv2.equalizeHist(tpl.gray)
            res3 = cv2.matchTemplate(eq_frame, eq_tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val3, _, max_loc3 = cv2.minMaxLoc(res3)
            if max_val3 > max_val:
                max_val = max_val3
                max_loc = max_loc3

        # 置信度阈值
        if max_val < 0.5:
            return None

        # 计算玩家位置（名牌中心下方为玩家）
        cx = max_loc[0] + w // 2
        cy = max_loc[1] + h + 18  # 名牌下方18像素为玩家中心
        return (cx, cy, max_val, w, h)

    def _is_front_monster(self, monster_hit, ax, ay=None, direction=None):
        """v15: 判断怪物是否在玩家面前（同一朝向 + 同平台）

        参数:
            monster_hit: (cx, cy, conf, w, h) 怪物中心坐标
            ax: 玩家anchor的x坐标
            ay: 玩家anchor的y坐标(可选，用于同平台判定)
            direction: 角色朝向(1=right, -1=left, None=使用self._facing)

        返回: True=在面前，False=在背后"""
        d = direction if direction is not None else self._facing
        if not d:
            return True  # 未设置朝向时不过滤
        mx, my = monster_hit[0], monster_hit[1]
        # 方向判定: 面朝右→怪在右边(mx>ax), 面朝左→怪在左边(mx<ax)
        if d > 0 and mx < ax:
            return False  # 面朝右但怪在左边 → 背后
        if d < 0 and mx > ax:
            return False  # 面朝左但怪在右边 → 背后
        # 高度判定: 同平台判定（如果传了ay）
        if ay is not None:
            tol = self.cfg.get("thresholds", {}).get("front_height_tol", 60)
            if abs(my - ay) > tol:
                return False  # 高度差太大 → 不同平台
        return True

    def _front_filter(self, monsters, ax, ay=None):
        """v16: 从怪物列表中筛选出在玩家面前的怪物

        改进: 使用攻击矩形框替代简单方向+高度判定，与参考项目一致。
        仅在front_only开启时生效，否则返回原列表"""
        front_only = self.cfg.get("thresholds", {}).get("front_only", True)
        if not front_only or not monsters:
            return monsters

        # 使用攻击框筛选
        attack_range = self.cfg.get("thresholds", {}).get("attack_range", 160)
        d = self._facing

        # 用攻击框过滤
        result = []
        for m in monsters:
            hit = m[0]
            mx, my = hit[0], hit[1]
            mw, mh = hit[3], hit[4] if len(hit) > 4 else 10

            # 方向判定
            if d > 0 and mx < ax:
                continue  # 面朝右但怪在左边
            if d < 0 and mx > ax:
                continue  # 面朝左但怪在右边

            # 攻击框重叠检测
            in_range, overlap = self.route_nav.is_monster_in_attack_range(
                (mx, my, mw, mh), (ax, ay or my), d, attack_range
            )
            if in_range:
                result.append(m)

        # 如果前方攻击框内无怪，回退到简单方向筛选
        if not result:
            result = [m for m in monsters if self._is_front_monster(m[0], ax, ay)]

        return result if result else monsters  # 最终回退：返回全部

    def _decide(self, frame, monsters, player, player_map):
        keys, th = self.cfg["keys"], self.cfg["thresholds"]
        patrol = self.cfg.get("patrol", {})
        now = time.time()
        patrol_on = patrol.get("enabled") and self.route_nav.ready
        # 战斗刚结束 → 立即触发拾取
        if self._was_combat and not monsters:
            self._loot_t = 0.0
        self._was_combat = bool(monsters)
        anchor = player if player is not None else \
            (frame.shape[1] // 2, frame.shape[0] // 2, 1.0, 0, 0)
        ax = anchor[0]
        # ---------- ① 战斗（v10：原地可打目标豁免巡逻门禁） ----------
        can_fight = (self._giveup_until is None or now > self._giveup_until)
        if can_fight and patrol_on and player_map is not None:
            off = self.route_nav.off_route_distance(player_map)
            if off > th.get("off_route_tol", 30):
                can_fight = False
                self._chase_t0 = None
                self._giveup_until = now + 2.0
                if now - self._offroute_log_t > 8:
                    self._offroute_log_t = now
                    self.log(f"已偏离路线 {off:.0f}px，放弃追击回归路线", "info")
        elif can_fight and patrol_on and player_map is None:
            can_fight = False  # 只禁"追"，不禁原地打（见★）
            if now - getattr(self, "_blind_log_t", 0.0) > 10.0:
                self._blind_log_t = now
                self.log("玩家点定位丢失，暂停追击先找回位置", "info")
        if can_fight and patrol_on and self.route_nav.phase != "none":
            can_fight = False  # 爬绳勿扰
        # ★ 原地战斗豁免：目标已在攻击/技能距离内 → 原地输出零位移，
        #   不依赖小地图定位。定位丢失/偏离/回归期间面前的怪照打。
        #   例外：试探定位移动段(0.8s)与爬绳中
        # v20: 攻击框改为左右对称，不再按朝向过滤，_combat 内部用红框判定
        ay = anchor[1]  # 玩家y坐标，用于同平台判定
        stand = []
        if monsters:
            stand_r = max(th["attack_range"], th.get("skill_range", 260))
            stand = [m for m in monsters if abs(m[0][0] - ax) <= stand_r]
        allow_stand = bool(stand) and self.route_nav.phase == "none" \
                      and not self.route_nav.probe_walking()
        fought = False
        if monsters and (can_fight or allow_stand):
            targets = monsters if can_fight else stand
            near = [m for m in targets
                    if abs(m[0][0] - ax) <= th.get("chase_range", 220)] \
                if patrol_on else targets
            if near:
                fought = True
                if self._chase_t0 is None:
                    self._chase_t0 = now
                if now - self._chase_t0 > patrol.get("max_chase_time", 4.0):
                    self._giveup_until = now + 5.0
                    self._chase_t0 = None
                    self.move.release_all()
                    self.log("追击超时，暂离怪物 5 秒，回归巡逻路线", "warn")
                else:
                    return self._combat(frame, near, player, now)
        if not fought:
            self._chase_t0 = None
            if self._giveup_until and now > self._giveup_until:
                self._giveup_until = None
        # ---------- ② 颜色路径巡逻 ----------
        if patrol.get("enabled") and self.route_nav.ready:
            cmd = self.route_nav.step(player_map, now)
            if cmd.stop:
                self.move.release_all()
                return cmd.status
            if self.route_nav.phase == "align":
                self._rope_assist(frame, cmd, now)
            self.move.set_dir(cmd.dir)
            # v15: 巡逻方向也同步到朝向，确保前方判定正确
            if cmd.dir and cmd.dir != 0:
                self._facing = cmd.dir
            self.move.set_climb(cmd.climb)
            if cmd.jump:
                if cmd.vdir:
                    self.controller.combo(keys.get("jump"), keys.get(cmd.vdir))
                else:
                    self.controller.tap(keys.get("jump"))
            if cmd.teleport:
                dkey = {"up": keys.get("up"), "down": keys.get("down"),
                        "left": keys.get("move_left"),
                        "right": keys.get("move_right")}.get(cmd.teleport)
                self.controller.combo(keys.get("teleport"), dkey, hold=0.25)
            if self.route_nav.phase == "none":  # 仅普通走位时：拾取 + 脱困
                self._try_loot(now)
                stuck = self.route_nav.stuck_seconds()
                if stuck > 3.0 and self.controller.cooldown_ok("unstuck", 2.5):
                    self.controller.tap(keys.get("jump"))
                    self.route_nav.touch()
                    self.log(f"巡逻停滞 {stuck:.1f}s，跳跃脱困", "warn")
            return cmd.status
        # ---------- ③ 无路线：连续左右找怪 ----------
        if now - self._roam_t > th.get("roam_interval", 2.5):
            self._roam_t = now
            self._roam_dir *= -1
        self.move.set_climb(None)
        self.move.set_dir(self._roam_dir)
        if self.cfg["options"].get("jump_while_roam") and self.controller.cooldown_ok("jump", 3.5):
            self.controller.tap(keys.get("jump"))
        self._try_loot(now)
        return "巡逻找怪"

    # ================= 战斗（v20：红框判定 + 左右方向按键） =================
    def _combat(self, frame, monsters, player, now):
        """v20: 重写战斗逻辑

        新规则：
          1. 用红框（get_attack_range_box）筛选怪物：水平 ±attack_range，垂直 cy~cy+skill_range
          2. 红框内的怪物：
             - 怪物 x < 玩家 x → 按左(move_left)再攻击
             - 怪物 x > 玩家 x → 按右(move_right)再攻击
          3. 攻击方式由 cfg["keys"]["attack_mode"] 决定：
             - "normal" → 普通攻击键
             - "skill"  → skill1~3 随机使用
          4. 红框外但在追击范围内 → 接近目标
        """
        keys, th = self.cfg["keys"], self.cfg["thresholds"]
        anchor = player if player is not None else \
            (frame.shape[1] // 2, frame.shape[0] // 2, 1.0, 0, 0)
        ax, ay = anchor[0], anchor[1]
        attack_range = th.get("attack_range", 160)
        skill_range = th.get("skill_range", 60)  # 攻击框垂直向下范围

        # ---- 计算红框（玩家脚下水平带状区域）----
        box = self.route_nav.get_attack_range_box((ax, ay), self._facing,
                                                  attack_range, skill_range)
        bx0, by0, bx1, by1 = box

        # ---- 筛选红框内的怪物 ----
        in_box = []
        for m in monsters:
            hit = m[0]
            mx, my = hit[0], hit[1]
            # 怪物中心点落在红框内即算
            if bx0 <= mx <= bx1 and by0 <= my <= by1:
                in_box.append(m)

        if in_box:
            # 选红框内最近的怪
            in_box.sort(key=lambda m: abs(m[0][0] - ax))
            hit, name = in_box[0]
            mx = hit[0]
            # 怪物在玩家左边 → 按左再攻击；在右边 → 按右再攻击
            if mx < ax:
                self.move.set_dir(0)
                self.move.set_climb(None)
                self.controller.tap(keys.get("move_left"), hold=0.06)
                self._facing = -1
            elif mx > ax:
                self.move.set_dir(0)
                self.move.set_climb(None)
                self.controller.tap(keys.get("move_right"), hold=0.06)
                self._facing = 1
            else:
                self.move.set_dir(0)
                self.move.set_climb(None)
            self.route_nav.touch()
            self._do_attack()
            return f"⚔ 攻击 {name} ({'左' if mx < ax else '右'})"

        # ---- 红框外：选最近的怪接近 ----
        if not monsters:
            return "无可攻击目标"
        # 追击范围
        chase_r = th.get("chase_range", 220)
        near = [m for m in monsters if abs(m[0][0] - ax) <= chase_r]
        if not near:
            return "目标超出追击范围"
        near.sort(key=lambda m: abs(m[0][0] - ax))
        hit, name = near[0]
        dx = hit[0] - ax
        self.move.set_climb(None)
        self.move.set_dir(-1 if dx < 0 else 1)
        return f"→ 接近 {name}"

    def _do_attack(self):
        """v20: 根据 attack_mode 执行攻击
        - "normal" → 普通攻击键
        - "skill"  → skill1~3 随机使用
        - 其他/未配置 → 普通攻击键
        """
        keys = self.cfg["keys"]
        if not self.controller.cooldown_ok("atk", 0.22):
            return
        mode = keys.get("attack_mode", "normal")
        if mode == "skill":
            skills = [keys[k] for k in ("skill1", "skill2", "skill3") if keys.get(k)]
            if skills:
                # 随机使用技能1-3
                import random as _rnd
                self.controller.tap(_rnd.choice(skills))
                return
            # 没配置技能键，回退普攻
        self.controller.tap(keys.get("attack"))

    # ================= 边走边拾取 =================
    def _try_loot(self, now):
        if not self.cfg["options"].get("loot_enabled", True):
            return
        key = self.cfg["keys"].get("pickup")
        if not key:
            return
        if now - self._loot_t < self.cfg["thresholds"].get("pickup_interval", 0.9):
            return
        self._loot_t = now
        self.controller.tap(key, hold=0.04)

    # ================= 定时挂机 / 休息调度 =================
    def _schedule_tick(self, now):
        """返回 True = 休息中（跳过战斗与巡逻决策）"""
        sch = self.cfg.get("schedule", {})
        if not sch.get("enabled"):
            self._resting = False
            self._sess_end = None
            self._grace_until = None
            return False
        if self._resting:
            if now >= self._rest_until:
                self._resting = False
                self._start_session(now)
                self.log("😴 休息结束，开始新一轮挂机", "ok")
            return self._resting
        if self._sess_end is None:
            self._start_session(now)
        elif now >= self._sess_end:  # 本轮结束 → 找安全点休息
            if self.route_nav.has_stop:
                if self._grace_until is None:
                    self._grace_until = now + sch.get("safe_stop_wait", 120)
                    self.log("本轮挂机结束，走向停止标记(安全点)…", "info")
                if now < self._grace_until:
                    if self._last_map_pos and \
                            self.route_nav.near_stop(self._last_map_pos):
                        self._begin_rest(now)
                else:
                    self._begin_rest(now)
            else:
                self._begin_rest(now)
        return self._resting

    def _start_session(self, now):
        sch = self.cfg.get("schedule", {})
        base = sch.get("duration_min", 60) * 60
        self._sess_end = now + base + random.uniform(-180, 180)  # ±3分钟
        self._grace_until = None
        self.log(f"⏱ 本轮挂机约 {int((self._sess_end - now) // 60)} 分钟（随机±3分钟）", "info")

    def _begin_rest(self, now):
        sch = self.cfg.get("schedule", {})
        lo, hi = sch.get("rest_lo_min", 5), sch.get("rest_hi_min", 10)
        self._resting = True
        self._rest_until = now + random.uniform(lo, hi) * 60
        self._sess_end = None
        self._grace_until = None
        self.move.release_all()
        self.log(f"😴 进入休息 {int((self._rest_until - now) // 60)} 分钟"
                 f"（血蓝监控保持运行）", "warn")

    def _attack(self):
        keys = self.cfg["keys"]
        if not self.controller.cooldown_ok("atk", 0.22):
            return
        if self.cfg["options"].get("use_skill_rotation"):
            seq = [keys[k] for k in ("attack", "skill1", "skill2", "skill3") if keys.get(k)]
            if seq:
                self.controller.tap(seq[self._atk_idx % len(seq)])
                self._atk_idx += 1
                return
        self.controller.tap(keys.get("attack"))

    @staticmethod
    def _box_of(hit):
        cx, cy, _, w, h = hit
        return (cx - w // 2, cy - h // 2, w, h)

    def _draw_boxes(self, ann, player=None):
        """v18: 绘制检测框 + 攻击范围可视化（边界检查 & 降噪）

        防止:
          - 攻击框画到画面外或尺寸异常导致看起来很奇怪
          - 无效的 nametag 玩家位置导致大色块覆盖屏幕
        """
        fh, fw = ann.shape[:2]

        # ---- 攻击范围矩形框（仅当玩家位置合理时绘制）----
        attack_box_drawn = False
        if player is not None and len(player) >= 2:
            ax, ay = float(player[0]), float(player[1])
            # 玩家位置必须基本在画面内才画攻击框，否则是识别错误导致的废位置
            if 0 <= ax < fw and 0 <= ay < fh:
                d = self._facing if self._facing else 1
                attack_range = self.cfg.get("thresholds", {}).get("attack_range", 160)
                skill_range = self.cfg.get("thresholds", {}).get("skill_range", 60)
                box = self.route_nav.get_attack_range_box((ax, ay), d, attack_range, skill_range)
                if box:
                    x0, y0, x1, y1 = box
                    # 裁剪到画面内
                    x0i, y0i = int(max(0, x0)), int(max(0, y0))
                    x1i, y1i = int(min(fw - 1, x1)), int(min(fh - 1, y1))
                    if x1i - x0i > 4 and y1i - y0i > 4:  # 框体最小尺寸检查
                        # 半透明填充（更淡：从0.15降到0.1，避免视觉喧宾夺主）
                        overlay = ann.copy()
                        cv2.rectangle(overlay, (x0i, y0i), (x1i, y1i),
                                      (0, 0, 255), -1)
                        cv2.addWeighted(overlay, 0.10, ann, 0.90, 0, ann)
                        # 边框：虚线感的细边框
                        cv2.rectangle(ann, (x0i, y0i), (x1i, y1i),
                                      (0, 0, 255), 1)
                        cv2.putText(ann, "ATK", (x0i + 4, y0i + 14),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
                        attack_box_drawn = True

        # ---- 检测框（怪物/玩家）：做边界裁剪，避免离谱的大框 ----
        valid_boxes = 0
        for entry in self._last_boxes:
            if len(entry) < 6:
                continue
            x1, y1, w, h, color, label = entry
            x1i, y1i = int(x1), int(y1)
            wi, hi = int(w), int(h)
            # 裁剪到画面内
            if x1i < 0: wi += x1i; x1i = 0
            if y1i < 0: hi += y1i; y1i = 0
            x2i = min(fw - 1, x1i + wi)
            y2i = min(fh - 1, y1i + hi)
            if x2i <= x1i + 2 or y2i <= y1i + 2:
                continue
            # 过滤掉占画面超过 50% 的离谱大框（一般是误识别）
            if (x2i - x1i) * (y2i - y1i) > 0.5 * fh * fw:
                continue
            cv2.rectangle(ann, (x1i, y1i), (x2i, y2i), color, 2)
            cv2.putText(ann, str(label)[:12], (x1i, max(12, y1i - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
            valid_boxes += 1

    @staticmethod
    def _draw_bar(ann, bar, color, label):
        if bar.get("w", 0) > 0:
            x, y, w, h = bar["x"], bar["y"], bar["w"], bar["h"]
            cv2.rectangle(ann, (x - 2, y - 2), (x + w + 2, y + h + 2), color, 1)
            cv2.putText(ann, label, (x, max(10, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)

    def _push_preview(self, ann):
        now = time.time()
        if now - self._last_preview < 0.066:
            return
        self._last_preview = now
        while True:
            try:
                self.preview_queue.get_nowait()
            except queue.Empty:
                break
        self.preview_queue.put(ann)

    def _update_fps(self):
        self._fps_n += 1
        now = time.time()
        if now - self._fps_t >= 1.0:
            self.status["fps"] = self._fps_n
            self._fps_n = 0
            self._fps_t = now

    def grab_frames(self, n=6, interval=0.12):
        """连抓多帧（黄点颜色采样用）"""
        frames = []
        for _ in range(n):
            f = self.capture.screenshot()
            if f is not None:
                frames.append(f)
            time.sleep(interval)
        return frames

    def sample_dot_color(self, frames):
        """自动采样小地图玩家黄点颜色"""
        return self.route_nav.auto_sample(frames)

    def set_nav_base(self, img):
        """GUI 录制/加载小地图底图后注入（导航面板背景）"""
        self._nav_base = img
        self.route_nav.set_base(img)

    NAV_W = 176  # 导航面板显示宽度（预览右上角）

    def _draw_patrol(self, ann, player_map):
        import time as _time
        now = _time.time()
        x, y, w, h = self.route_nav.minimap
        if w <= 4:
            return
        # ① 真实小地图只画 1px 细边框（不再叠加路线颜色，不遮挡）
        cv2.rectangle(ann, (x - 2, y - 2), (x + w + 2, y + h + 2),
                      (255, 210, 80), 1)
        # ② 组装导航面板：小地图底图 + 路线颜色
        base = self._nav_base
        panel = base.copy() if (base is not None and base.shape[:2] == (h, w)) \
            else np.full((h, w, 3), 24, np.uint8)
        route = self.route_nav.route
        if route is not None and route.shape[:2] == panel.shape[:2]:
            nz = route.max(axis=2) > 40
            panel[nz] = route[nz]
        # ③ 缩放贴到预览画面右上角（轻微半透明融合）
        # v18 修复：先根据 dw=NAV_W 计算 scale_x，再决定 dh；
        # 如果 dh 被画面高度限制截断了，则必须重新计算 scale，
        # 否则 panel 被非等比缩放，绿十字 y 坐标会错位到 NAV 面板外。
        ah, aw = ann.shape[:2]
        max_dh = max(40, ah - 24)
        scale_x = self.NAV_W / max(1, w)
        dh = int(round(h * scale_x))
        if dh > max_dh:
            # 被截断：改为按高度限制反推实际缩放比
            scale = max_dh / max(1, h)
            dw = max(1, int(round(w * scale)))
            dh = max_dh
        else:
            # 未被截断：宽度优先缩放保持不变
            scale = scale_x
            dw = self.NAV_W
        disp = cv2.resize(panel, (dw, dh), interpolation=cv2.INTER_NEAREST)
        px1, py1 = aw - dw - 10, 10
        # 防止面板贴到画面外（极端尺寸时）
        px1 = max(4, min(px1, aw - dw - 4))
        py1 = max(4, min(py1, ah - dh - 4))
        roi = ann[py1:py1 + dh, px1:px1 + dw]
        ann[py1:py1 + dh, px1:px1 + dw] = cv2.addWeighted(roi, 0.2, disp, 0.95, 0)
        cv2.rectangle(ann, (px1 - 1, py1 - 1), (px1 + dw, py1 + dh),
                      (255, 210, 80), 1)
        cv2.putText(ann, "NAV", (px1 + 4, py1 + 13),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 210, 80), 1, cv2.LINE_AA)
        # ④ 候选点（暗黄小圈，调试用）+ 玩家位置（绿十字）
        for cx, cy in self.route_nav.debug_cands:
            cvx = int(px1 + cx * scale)
            cvy = int(py1 + cy * scale)
            # 只有在 NAV 面板内才画，避免画到外面遮挡画面
            if px1 <= cvx < px1 + dw and py1 <= cvy < py1 + dh:
                cv2.circle(ann, (cvx, cvy), 2, (0, 180, 255), 1)
        if player_map and len(player_map) >= 2:
            mx = int(px1 + player_map[0] * scale)
            my = int(py1 + player_map[1] * scale)
            # v18: 一次性诊断日志（30秒），告诉用户绿十字具体画在了哪里
            now = time.time()
            last_log_t = getattr(self, "_patrol_log_t", -99.0)
            if now - last_log_t > 30.0:
                self._patrol_log_t = now
                self.log(
                    f"📍 NAV面板: pos=({player_map[0]:.1f},{player_map[1]:.1f}) "
                    f"→ 屏幕像素=({mx},{my}) | NAV面板: ({px1},{py1})-({px1 + dw},{py1 + dh}) "
                    f"scale={scale:.3f} | 底图base={'有' if base is not None else '无'} "
                    f"route={'有' if route is not None else '无'}",
                    "info"
                )
            # 十字坐标裁剪到面板范围内（至少画一个可见标记）
            if mx < px1 or mx >= px1 + dw or my < py1 or my >= py1 + dh:
                # 位置不在面板内：诊断日志
                if now - getattr(self, "_patrol_oob_log_t", -99.0) > 20.0:
                    self._patrol_oob_log_t = now
                    self.log(
                        f"⚠ 玩家位置越界: map=({player_map[0]:.1f},{player_map[1]:.1f}) "
                        f"超出 NAV 面板范围(w={w},h={h})。"
                        f"若base存在请检查滚动补偿；否则说明小地图坐标计算异常",
                        "warn"
                    )
                # 至少在面板边缘画一个醒目提示（黄圈），让用户知道定位有值但越界
                cxm = min(max(mx, px1 + 6), px1 + dw - 6)
                cym = min(max(my, py1 + 6), py1 + dh - 6)
                cv2.drawMarker(ann, (cxm, cym), (0, 200, 255),
                               cv2.MARKER_CROSS, 8, 1)
                cv2.putText(ann, "OOB", (cxm + 6, cym - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 200, 255), 1)
            else:
                # 正常范围：画绿十字（缩小线条避免遮挡 mini 地图上的绳子标记）
                cv2.drawMarker(ann, (mx, my), (80, 255, 120),
                               cv2.MARKER_CROSS, 8, 1)
                cv2.circle(ann, (mx, my), 4, (80, 255, 120), 1)
        elif player_map is None and now - getattr(self, "_patrol_none_log_t", -99.0) > 20.0:
            self._patrol_none_log_t = now
            self.log("⚠ NAV面板: player_map 为空，绿十字不显示。"
                     "若 BGR 锁定日志正常，请检查 player_pos() 的滚动补偿逻辑", "warn")
        if self.route_nav.laps:
            cv2.putText(ann, f"LAP {self.route_nav.laps}", (px1, py1 + dh + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 210, 80), 1, cv2.LINE_AA)