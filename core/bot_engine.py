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

from core.window_capture import WindowCapture
from core.detector import TemplateDetector
from core.resource_monitor import BarMonitor
from core.action_controller import ActionController, MovementController
# from core.patrol import PatrolNavigator, WALK, JUMP, ROPE_UP, ROPE_DOWN
from core.color_route import ColorRouteNavigator
from core.rope_detector import RopeDetector


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
            )
            # 自动加载地图包路线图（尺寸须与小地图区域一致）
            rp = p.get("route_path", "")
            if rp and os.path.exists(rp) and rp != self._route_path_loaded:
                img = cv2.imread(rp)
                if img is not None:
                    if img.shape[0] == mm.get("h", 0) and img.shape[1] == mm.get("w", 0):
                        self.route_nav.load(img)
                        self._route_path_loaded = rp
                        self.log(f"颜色路线已加载：{os.path.basename(os.path.dirname(rp))}")
                    else:
                        self.log("路线图尺寸与小地图区域不符，请重录小地图并重画路线", "warn")
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

        # ② 失焦安全 + 自动喝药
        focused = True
        if self.mode == Mode.RUNNING:
            focused = self._check_focus()
            if not focused:
                self.move.release_all()
            else:
                self._auto_potion(hp, mp)

        # ③ 目标识别（节流，重操作）+ 小地图定位（每帧，便宜）
        if now - self._det_t >= self.DET_INTERVAL:
            self._last_monsters, self._last_player = self._detect(frame, ann)
            self._det_t = now
        monsters, player = self._last_monsters, self._last_player
        self._draw_boxes(ann)
        st["monsters"] = len(monsters)

        player_map = self.route_nav.player_pos(frame) \
            if self.route_nav.minimap[2] > 4 else None

        # ④ 决策
        if self.mode == Mode.RUNNING:
            st["action"] = (self._decide(frame, monsters, player, player_map)
                            if focused else "失焦保护中")
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

    def _detect(self, frame, ann):
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
                anchor = player if player is not None else \
                    (frame.shape[1] // 2, frame.shape[0] // 2, 1.0, 0, 0)
                hit, name = min(monsters, key=lambda m: abs(m[0][0] - anchor[0]))
                dx = hit[0] - anchor[0]
                if abs(dx) <= th["attack_range"]:
                    self.move.set_dir(0)
                    self.move.set_climb(None)
                    self.route_nav.touch()  # ← 修复点：原 self.nav.touch()
                    self._attack()
                    return f"攻击 {name}"
                self.move.set_climb(None)
                self.move.set_dir(-1 if dx < 0 else 1)  # 连续追击，匀速接近
                return f"→ 接近 {name}"
        else:
            self._chase_t0 = None
            if self._giveup_until and now > self._giveup_until:
                self._giveup_until = None
        # ---------- ② 颜色路径巡逻 ----------
        if patrol.get("enabled") and self.route_nav.ready:
            cmd = self.route_nav.step(player_map, now)
            if cmd.stop:
                self.move.release_all()
                return cmd.status
            if self.route_nav.phase in ("align", "grab"):  # 主画面绳子精调
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
            # 停滞脱困（仅普通走位阶段；爬绳状态机有自己的超时逻辑）
            if self.route_nav.phase == "none" and cmd.dir:
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
        return "巡逻找怪"
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

    def _draw_patrol(self, ann, player_map):
        x, y, w, h = self.route_nav.minimap
        if w <= 4:
            return
        cv2.rectangle(ann, (x - 2, y - 2), (x + w + 2, y + h + 2), (255, 210, 80), 1)
        route = self.route_nav.route  # 叠加路线标记
        if route is not None:
            roi = ann[y:y + h, x:x + w]
            nz = route.max(axis=2) > 40
            roi[nz] = route[nz]
        if self.route_nav.laps:
            cv2.putText(ann, f"LAP {self.route_nav.laps}", (x + 4, y + h + 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 210, 80), 1, cv2.LINE_AA)
        if player_map:
            cv2.drawMarker(ann, (int(x + player_map[0]), int(y + player_map[1])),
                           (80, 255, 120), cv2.MARKER_CROSS, 12, 2)
