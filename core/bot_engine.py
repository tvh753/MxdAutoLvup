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


class Mode:
    IDLE = "idle";
    PREVIEW = "preview";
    RUNNING = "running";
    PAUSED = "paused"


class BotEngine(threading.Thread):
    DET_INTERVAL = 0.12  # 怪物识别节流：移动决策每帧跑（便宜），重识别节流

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
        self.detector.clear()
        for item in self.cfg.get("monster_templates", []):
            try:
                self.detector.load(item["name"], item["path"])
            except Exception as e:
                self.log(f"模板加载失败 [{item.get('name')}]: {e}", "warn")
        pt = self.cfg.get("player_template")
        self._player_tpl = None
        if pt and pt.get("path"):
            try:
                self.detector.load(pt["name"], pt["path"])
                self._player_tpl = pt["name"]
            except Exception as e:
                self.log(f"玩家模板加载失败: {e}", "warn")

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
            elif img.shape[0] == mm.get("h", 0) and img.shape[1] == mm.get("w", 0):
                self.route_nav.load(img)
                self._route_path_loaded = rp
                base = imread_u(os.path.join(os.path.dirname(rp), "minimap.png"))
                self._nav_base = base if (base is not None and
                                          base.shape[:2] == img.shape[:2]) else None
                self.log(f"颜色路线已加载：{os.path.basename(os.path.dirname(rp))}"
                         + ("" if self._nav_base is not None
                            else "（无小地图底图，导航面板用暗底）"))
            else:
                self.log("路线图尺寸与小地图区域不符，请重录小地图并重画路线", "warn")
                self._nav_base = None
        self.move.bind(self.cfg["keys"])


    def load_route(self, route_bgr, path_tag="memory"):
        """GUI 绘制/加载路线后直接喂入；path_tag 用于缓存判断"""
        if route_bgr is None:
            return
        mm = self.cfg.get("patrol", {}).get("minimap", {})
        if route_bgr.shape[0] == mm.get("h", 0) and \
                route_bgr.shape[1] == mm.get("w", 0):
            self.route_nav.load(route_bgr)
            self._route_path_loaded = path_tag
        else:
            self.log("路线图尺寸与小地图区域不符", "warn")

    def invalidate_route_cache(self):
        """切换地图包前调用，强制 reload 重新读文件"""
        self._route_path_loaded = None

    def _anchor_x(self, frame):
        p = self._last_player
        return p[0] if p is not None else frame.shape[1] // 2

    def _rope_assist(self, frame, cmd, now):
        """爬绳对准阶段：主画面绳子精调（小地图缩放误差的兜底）
        检出绳子且未对准 → 覆盖走位方向继续微调；在grab阶段则打回重对位"""
        if now - self._rope_t < 0.25:
            return
        self._rope_t = now
        ropes = self.rope_det.find(frame, self.cfg.get("detect_region"))
        if not ropes:
            return
        best = min(ropes, key=lambda r: abs(r[0] - self._anchor_x(frame)))
        dx = best[0] - self._anchor_x(frame)
        if abs(dx) > 28:
            cmd.dir = -1 if dx < 0 else 1
            if self.route_nav.phase == "grab":
                self.route_nav.back_to_align()

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
        self._draw_boxes(ann)
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
        """模板识别（重操作，由 _tick 节流调用）；标注框存缓存供每帧重绘"""
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
        return monsters, player

    def _decide(self, frame, monsters, player, player_map):
        keys, th = self.cfg["keys"], self.cfg["thresholds"]
        patrol = self.cfg.get("patrol", {})
        now = time.time()
        # 战斗刚结束 → 立即触发拾取
        if self._was_combat and not monsters:
            self._loot_t = 0.0
        self._was_combat = bool(monsters)
        # ---------- ① 战斗优先 ----------
        if monsters and (self._giveup_until is None or now > self._giveup_until):
            if self._chase_t0 is None:
                self._chase_t0 = now
            if now - self._chase_t0 > patrol.get("max_chase_time", 8.0):
                self._giveup_until = now + 5.0
                self._chase_t0 = None
                self.move.release_all()
                self.log("追击超时，暂离怪物 5 秒，回归巡逻路线", "warn")
            else:
                return self._combat(frame, monsters, player, now)
        else:
            self._chase_t0 = None
            if self._giveup_until and now > self._giveup_until:
                self._giveup_until = None
        # ---------- ② 颜色路径巡逻 ----------
        if patrol.get("enabled") and self.route_nav.ready:
            cmd = self.route_nav.step(player_map, now)
            if cmd.stop:
                self.move.release_all()
                self._try_loot(now)
                return cmd.status
            if self.route_nav.phase in ("align", "grab"):
                self._rope_assist(frame, cmd, now)
            self.move.set_dir(cmd.dir)
            self.move.set_climb(cmd.climb)
            if cmd.jump:
                if cmd.vdir:  # 下跳：按住↓+跳
                    self.controller.combo(keys.get("jump"), keys.get(cmd.vdir))
                else:
                    self.controller.tap(keys.get("jump"))
            if cmd.teleport:  # 瞬移：方向+传送键
                dkey = {"up": keys.get("up"), "down": keys.get("down"),
                        "left": keys.get("move_left"),
                        "right": keys.get("move_right")}.get(cmd.teleport)
                self.controller.combo(keys.get("teleport"), dkey, hold=0.25)
            if self.route_nav.phase == "none":  # 边走边拾取 + 脱困
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

    # ================= 战斗（前方普攻 / 周围技能） =================
    def _combat(self, frame, monsters, player, now):
        keys, th = self.cfg["keys"], self.cfg["thresholds"]
        anchor = player if player is not None else \
            (frame.shape[1] // 2, frame.shape[0] // 2, 1.0, 0, 0)
        ax = anchor[0]
        d = self.move.h_dir
        if d:
            self._facing = d  # 移动方向即朝向
        hit, name = min(monsters, key=lambda m: abs(m[0][0] - ax))
        dx = hit[0] - ax
        adx = abs(dx)
        # ① 前方近身 → 普攻
        if adx <= th["attack_range"]:
            if dx == 0 or (1 if dx > 0 else -1) == self._facing:
                self.move.set_dir(0)
                self.move.set_climb(None)
                self.route_nav.touch()
                self._attack()
                return f"⚔ 攻击 {name}"
            self.move.set_dir(0)  # 背后 → 松键轻点反向转身
            self.move.set_climb(None)
            self.controller.tap(keys["move_left"] if dx < 0 else keys["move_right"],
                                hold=0.06)
            self._facing = 1 if dx > 0 else -1
            return f"↩ 转身 → {name}"
        # ② 技能（仅配置了技能键时，攻击周围目标）
        skills = [keys[k] for k in ("skill1", "skill2", "skill3") if keys.get(k)]
        if skills and adx <= th.get("skill_range", 260) and \
                self.controller.cooldown_ok("skill", 0.3):
            self.move.set_dir(0)
            self.move.set_climb(None)
            self.route_nav.touch()
            self.controller.tap(skills[self._skill_idx % len(skills)])
            self._skill_idx += 1
            return f"✨ 技能攻击 {name}"
        # ③ 接近目标
        self.move.set_climb(None)
        self.move.set_dir(-1 if dx < 0 else 1)
        return f"→ 接近 {name}"

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

    def _draw_boxes(self, ann):
        for x1, y1, w, h, color, label in self._last_boxes:
            cv2.rectangle(ann, (x1, y1), (x1 + w, y1 + h), color, 2)
            cv2.putText(ann, label, (x1, max(12, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

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

    NAV_W = 176  # 导航面板显示宽度（预览右上角）

    def _draw_patrol(self, ann, player_map):
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
        scale = self.NAV_W / max(1, w)
        dw = self.NAV_W
        dh = int(round(h * scale))
        ah, aw = ann.shape[:2]
        dh = min(dh, max(40, ah - 24))
        disp = cv2.resize(panel, (dw, dh), interpolation=cv2.INTER_NEAREST)
        px1, py1 = aw - dw - 10, 10
        roi = ann[py1:py1 + dh, px1:px1 + dw]
        ann[py1:py1 + dh, px1:px1 + dw] = cv2.addWeighted(roi, 0.2, disp, 0.95, 0)
        cv2.rectangle(ann, (px1 - 1, py1 - 1), (px1 + dw, py1 + dh),
                      (255, 210, 80), 1)
        cv2.putText(ann, "NAV", (px1 + 4, py1 + 13),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 210, 80), 1, cv2.LINE_AA)
        # ④ 候选点（暗黄小圈，调试用）+ 玩家位置（绿十字）
        for cx, cy in self.route_nav.debug_cands:
            cv2.circle(ann, (int(px1 + cx * scale), int(py1 + cy * scale)),
                       2, (0, 180, 255), 1)
        if player_map:
            mx = int(px1 + player_map[0] * scale)
            my = int(py1 + player_map[1] * scale)
            cv2.drawMarker(ann, (mx, my), (80, 255, 120),
                           cv2.MARKER_CROSS, 12, 2)
            cv2.circle(ann, (mx, my), 4, (80, 255, 120), 1)
        if self.route_nav.laps:
            cv2.putText(ann, f"LAP {self.route_nav.laps}", (px1, py1 + dh + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 210, 80), 1, cv2.LINE_AA)