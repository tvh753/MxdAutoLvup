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

from core.window_capture import WindowCapture
from core.detector import TemplateDetector
from core.resource_monitor import BarMonitor
from core.action_controller import ActionController
from core.patrol import PatrolNavigator


class Mode:
    IDLE = "idle"; PREVIEW = "preview"; RUNNING = "running"; PAUSED = "paused"


class BotEngine(threading.Thread):
    def __init__(self, cfg, log_fn):
        super().__init__(daemon=True, name="BotEngine")
        self.cfg = cfg                      # 共享配置引用 → GUI 修改即时热生效
        self.log = log_fn
        self.capture = WindowCapture()
        self.detector = TemplateDetector()
        self.controller = ActionController()
        self.hp_bar = BarMonitor("hp")
        self.mp_bar = BarMonitor("mp")
        self.exp_bar = BarMonitor("exp")
        self.preview_queue = queue.Queue(maxsize=2)

        self.nav = PatrolNavigator()  # 小地图巡逻导航
        self._chase_t0 = None  # 追击起始时间
        self._giveup_until = None  # 放弃追击截止时间

        self.mode = Mode.IDLE
        self._stop_flag = False
        self._frame = None
        self._frame_lock = threading.Lock()
        self._player_tpl = None

        self.status = {"hp": -1.0, "mp": -1.0, "exp": -1.0, "fps": 0, "monsters": 0,
                       "action": "-", "mode": Mode.IDLE}

        self._atk_idx = 0; self._roam_dir = 1; self._roam_t = 0.0
        self._last_warn = 0.0; self._last_preview = 0.0
        self._fps_n = 0; self._fps_t = time.time()

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
            self.nav.configure(
                minimap=(mm.get("x", 0), mm.get("y", 0), mm.get("w", 0), mm.get("h", 0)),
                dot_color=p.get("player_dot_color"),
                tolerance=p.get("dot_tolerance", 80),
                waypoints=p.get("waypoints", []),
                mode=p.get("mode", "pingpong"),
                arrive_tol=p.get("arrive_tol", 6),
        )


    def set_mode(self, mode):
        self.mode = mode
        self.status["mode"] = mode
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
        while not self._stop_flag:
            if self.mode == Mode.IDLE:
                time.sleep(0.1); continue
            try:
                self._tick()
            except Exception as e:
                self.log(f"引擎异常: {e}", "error")
                time.sleep(0.5)
            time.sleep(0.004)

    def _tick(self):
        frame = self.capture.screenshot()
        if frame is None:
            time.sleep(0.15); return
        with self._frame_lock:
            self._frame = frame
        self._update_fps()

        ann = frame.copy()
        st = self.status

        # ① 资源监测（红HP · 蓝MP · 黄EXP）
        hp, mp = self.hp_bar.percentage(frame), self.mp_bar.percentage(frame)
        exp = self.exp_bar.percentage(frame)
        st["hp"], st["mp"], st["exp"] = hp, mp, exp
        self._draw_bar(ann, self.cfg.get("hp_bar", {}), (70, 70, 255), "HP")
        self._draw_bar(ann, self.cfg.get("mp_bar", {}), (255, 170, 60), "MP")
        self._draw_bar(ann, self.cfg.get("exp_bar", {}), (60, 220, 255), "EXP")

        # ② 失焦安全 + 自动喝药（按键只在游戏聚焦时发送）
        focused = True
        if self.mode == Mode.RUNNING:
            focused = self._check_focus()
            if focused:
                self._auto_potion(hp, mp)

        # ③ 目标识别
        monsters, player = self._detect(frame, ann)
        st["monsters"] = len(monsters)

        # ③' 小地图玩家定位（巡逻）
        player_map = self.nav.player_pos(frame) if self.nav.minimap[2] > 4 else None
        self._draw_patrol(ann, player_map)

        # ④ 战斗决策
        if self.mode == Mode.RUNNING:
            st["action"] = (self._decide(frame, monsters, player, player_map)
                            if focused else "失焦保护中")

        # ⑤ 预览推送（限流 ~15fps）
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
            gray = cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY)   # 整图只转一次灰度
            for name in self.detector.templates:
                hits = self.detector.find_all(name, th, scene_gray=gray, offset=(x, y))
                if not hits:
                    continue
                if name == self._player_tpl:
                    player = hits[0]
                    self._draw_box(ann, hits[0], (90, 255, 90), "PLAYER")
                else:
                    for h in hits:
                        monsters.append((h, name))
                        self._draw_box(ann, h, (60, 100, 255), name[:10])
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
                self.log("追击超时，暂离怪物 5 秒，回归巡逻路线", "warn")
            else:
                # 锚点：玩家模板优先，缺省用画面中心近似（相机跟随玩家近似居中）
                anchor = player if player is not None else \
                    (frame.shape[1] // 2, frame.shape[0] // 2, 1.0, 0, 0)
                hit, name = min(monsters, key=lambda m: abs(m[0][0] - anchor[0]))
                dx = hit[0] - anchor[0]
                if abs(dx) <= th["attack_range"]:
                    self.nav.touch()  # 战斗中不算巡逻停滞
                    self._attack()
                    return f"攻击 {name}"
                self.controller.hold(keys["move_left"] if dx < 0 else keys["move_right"], 0.15)
                return f"→ 接近 {name}"
        else:
            self._chase_t0 = None
            if self._giveup_until and now > self._giveup_until:
                self._giveup_until = None

        # ---------- ② 路线巡逻 ----------
        if patrol.get("enabled") and self.nav.ready:
            if player_map is None:
                return "🧭 定位玩家点中…（黄点闪烁/被遮挡）"
            if self.nav.arrived(player_map):
                self.nav.advance()
                return "✔ 到达路点 → 下一点"
            d = self.nav.direction(player_map)
            if d:
                self.controller.hold(keys["move_left"] if d < 0 else keys["move_right"], 0.12)
                stuck = self.nav.stuck_seconds()
                if stuck > 2.5 and self.controller.cooldown_ok("unstuck", 2.0):
                    self.controller.tap(keys.get("jump"))
                    self.nav.touch()
                    self.log(f"巡逻停滞 {stuck:.1f}s，跳跃脱困", "warn")
                elif stuck > 8.0:
                    self.nav.advance()
                    self.nav.touch()
                    self.log("长时间卡住，跳过当前路点", "warn")
                return f"🧭 路点 {self.nav.index + 1}/{len(self.nav.waypoints)}"
            return "🧭 路点对齐中"

        # ---------- ③ 无路线：传统左右找怪 ----------
        if now - self._roam_t > th.get("roam_interval", 2.5):
            self._roam_t = now
            self._roam_dir *= -1
        self.controller.hold(keys["move_left"] if self._roam_dir < 0 else keys["move_right"], 0.12)
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
    def _draw_box(ann, hit, color, label):
        cx, cy, conf, w, h = hit
        x1, y1 = cx - w // 2, cy - h // 2
        cv2.rectangle(ann, (x1, y1), (x1 + w, y1 + h), color, 2)
        cv2.putText(ann, f"{label} {conf:.2f}", (x1, max(12, y1 - 5)),
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
        return self.nav.auto_sample(frames)

    def _draw_patrol(self, ann, player_map):
        """预览叠加：小地图框 + 路点 + 玩家点十字"""
        x, y, w, h = self.nav.minimap
        if w <= 4:
            return
        cv2.rectangle(ann, (x - 2, y - 2), (x + w + 2, y + h + 2), (255, 210, 80), 1)
        pts = self.nav.waypoints
        for i in range(len(pts) - 1):  # 路线连线
            cv2.line(ann, (x + pts[i][0], y + pts[i][1]),
                     (x + pts[i + 1][0], y + pts[i + 1][1]),
                     (255, 210, 80), 1, cv2.LINE_AA)
        if self.nav.mode == "loop" and len(pts) > 2:
            cv2.line(ann, (x + pts[-1][0], y + pts[-1][1]),
                     (x + pts[0][0], y + pts[0][1]), (255, 210, 80), 1, cv2.LINE_AA)
        for i, (wx, wy) in enumerate(pts):  # 路点编号
            ax, ay, color = x + wx, y + wy, (255, 210, 80)
            if i == self.nav.index:
                color = (80, 255, 120)  # 当前目标高亮
                cv2.circle(ann, (ax, ay), 8, color, 2)
            cv2.circle(ann, (ax, ay), 3, color, -1)
            cv2.putText(ann, str(i + 1), (ax + 6, ay - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
        if player_map:  # 玩家点十字
            cv2.drawMarker(ann, (int(x + player_map[0]), int(y + player_map[1])),
                           (80, 255, 120), cv2.MARKER_CROSS, 12, 2)