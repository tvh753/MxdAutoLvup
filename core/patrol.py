# -*- coding: utf-8 -*-
# @Time    : 26/8/26 20:57
# @Author  : yy
# @File    : patrol.py
# @Software: MxdAutoLvup

"""
小地图巡逻导航 v2 —— 平滑移动 + 平台跳跃 + 绳索攀爬
=====================================================
  1. 输出 PatrolCommand 指令流，配合 MovementController 连续按住移动键
  2. 路点类型：walk 行走 / jump 跳跃 / rope_up 爬绳上 / rope_down 绳下
  3. 玩家点最近邻追踪（抗 NPC 黄点干扰）+ 闪烁累积兜底
  4. 行走/跳跃到点只判水平（游戏无法竖直微调，双轴判定会永远到不了点）
  5. 抓绳：按住 上/下 → 检测 y 变化确认上绳 → 持续攀爬到目标高度
     未上绳则退开重新对位重试，超次跳过该路点
"""
import time
import cv2
import numpy as np

DEFAULT_DOT_COLOR = (60, 230, 255)  # BGR · 小地图玩家黄点
BLINK_FRAMES = 12  # 闪烁兜底：累积掩码帧数
MOVE_EPS = 1.5  # 判定“移动中”的最小位移

WALK, JUMP, ROPE_UP, ROPE_DOWN = "walk", "jump", "rope_up", "rope_down"
ACTIONS = (WALK, JUMP, ROPE_UP, ROPE_DOWN)


class PatrolCommand:
    """单帧巡逻指令（引擎负责翻译成按键）"""
    __slots__ = ("dir", "climb", "jump", "grab", "status")

    def __init__(self, dir=None, climb=None, jump=False, grab=None, status=""):
        self.dir = dir  # -1/0/+1；None=保持当前方向
        self.climb = climb  # None=松开 / 'up' / 'down' 持续按住（爬绳）
        self.jump = jump  # True → 点按跳跃键
        self.grab = grab  # 'up'/'down' → 点按上/下键（备用抓绳）
        self.status = status


class PatrolNavigator:
    def __init__(self):
        # ---- 配置 ----
        self.minimap = (0, 0, 0, 0)
        self.dot_color = np.array(DEFAULT_DOT_COLOR, dtype=np.int16)
        self.tolerance = 80
        self.waypoints = []  # [[mx, my, action], ...]，兼容旧 [mx, my]
        self.mode = "pingpong"
        self.arrive_tol = 6  # 行走/跳跃到点容差（水平）
        self.grab_tol = 4  # 抓绳水平容差（绳窄，需对准）
        self.air_time = 0.55  # 跳跃后保持方向键时长（空中惯性）
        self.grab_timeout = 0.9  # 抓绳等待 y 变化的超时
        self.max_retries = 3  # 抓绳最大重试
        # ---- 运行时 ----
        self._idx, self._dir = 0, 1
        self._masks = []
        self._last_pos = None
        self._move_t = time.time()
        # ---- 路点动作状态机 ----
        self._phase = "approach"  # approach / jumping / backoff / grabbing / climbing
        self._t0 = 0.0
        self._y0 = None
        self._retries = 0
        self._backoff_dir = 1

    # ================= 配置 =================
    def configure(self, minimap=None, dot_color=None, tolerance=None,
                  waypoints=None, mode=None, arrive_tol=None, grab_tol=None,
                  air_time=None, grab_timeout=None, max_retries=None):
        if minimap and minimap[2] > 4 and minimap[3] > 4:
            self.minimap = tuple(int(v) for v in minimap)
        if dot_color:
            self.dot_color = np.array(dot_color, dtype=np.int16)
        if tolerance is not None:
            self.tolerance = int(tolerance)
        if waypoints is not None:
            wp = [self._norm_wp(w) for w in waypoints]
            if wp != self.waypoints:
                self.waypoints = wp
                self.reset()
        if mode in ("pingpong", "loop"):
            self.mode = mode
        if arrive_tol is not None:
            self.arrive_tol = max(2, int(arrive_tol))
        if grab_tol is not None:
            self.grab_tol = max(2, int(grab_tol))
        if air_time is not None:
            self.air_time = float(air_time)
        if grab_timeout is not None:
            self.grab_timeout = float(grab_timeout)
        if max_retries is not None:
            self.max_retries = int(max_retries)

    @staticmethod
    def _norm_wp(w):
        x, y = int(w[0]), int(w[1])
        act = w[2] if len(w) > 2 and w[2] in ACTIONS else WALK
        return [x, y, act]

    def reset(self):
        self._idx, self._dir = 0, 1
        self._masks, self._last_pos = [], None
        self._move_t = time.time()
        self._reset_phase()

    def touch(self):
        self._move_t = time.time()

    @property
    def ready(self):
        return self.minimap[2] > 4 and len(self.waypoints) >= 2

    @property
    def index(self):
        return self._idx

    @property
    def phase(self):
        return self._phase

    # ================= 玩家定位（最近邻追踪） =================
    def player_pos(self, frame_bgr):
        x, y, w, h = self.minimap
        if w <= 4 or h <= 4 or frame_bgr is None:
            return None
        H, W = frame_bgr.shape[:2]
        x2, y2 = min(x + w, W), min(y + h, H)
        if x < 0 or y < 0 or x2 <= x or y2 <= y:
            return None
        roi = frame_bgr[y:y2, x:x2]

        dist = np.abs(roi.astype(np.int16) - self.dot_color).sum(axis=2)
        mask = (dist < self.tolerance * 3).astype(np.uint8)

        self._masks.append(mask)
        if len(self._masks) > BLINK_FRAMES:
            self._masks.pop(0)

        pos = self._track(mask)  # 当前帧优先
        if pos is None and self._masks:
            pos = self._track(np.maximum.reduce(self._masks))  # 闪烁兜底
        return pos

    def _track(self, mask):
        """质心候选中：有上次位置→取最近邻；否则取最大面积。
        （旧版恒取最大面积，NPC 黄点常比玩家点亮 → 位置锁死在 NPC 上）"""
        n = cv2.connectedComponentsWithStats(mask, 8)
        if n[0] < 2:
            return None
        cents, stats = n[3], n[2]
        cands = [(float(cents[i][0]), float(cents[i][1]),
                  int(stats[i][cv2.CC_STAT_AREA]))
                 for i in range(1, n[0]) if stats[i][cv2.CC_STAT_AREA] >= 2]
        if not cands:
            return None
        if self._last_pos is None:
            return max(cands, key=lambda c: c[2])[:2]
        lx, ly = self._last_pos
        return min(cands, key=lambda c: (c[0] - lx) ** 2 + (c[1] - ly) ** 2)[:2]

    def stuck_seconds(self):
        return max(0.0, time.time() - self._move_t) if self._last_pos is not None else 0.0

    def auto_sample(self, frames):
        """多帧采样玩家黄点 BGR 中位色"""
        x, y, w, h = self.minimap
        if w <= 4 or not frames:
            return None
        pixels = []
        for f in frames:
            H, W = f.shape[:2]
            x2, y2 = min(x + w, W), min(y + h, H)
            if x < 0 or y < 0 or x2 <= x or y2 <= y:
                continue
            roi = f[y:y2, x:x2]
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            m = cv2.inRange(hsv, (18, 120, 150), (38, 255, 255))
            if m.any():
                pixels.append(roi[m > 0])
        if not pixels:
            return None
        med = np.median(np.vstack(pixels), axis=0).astype(int)
        self.dot_color = np.array(med, dtype=np.int16)
        return med.tolist()

    # ================= 路点推进 =================
    def _wp(self, i):
        return self.waypoints[i]

    def _next_wp(self):
        if len(self.waypoints) < 2:
            return None
        return self.waypoints[(self._idx + 1) % len(self.waypoints)]

    def advance(self):
        if len(self.waypoints) < 2:
            return
        if self.mode == "loop":
            self._idx = (self._idx + 1) % len(self.waypoints)
        else:
            nxt = self._idx + self._dir
            if nxt >= len(self.waypoints):
                self._dir, nxt = -1, self._idx - 1
            elif nxt < 0:
                self._dir, nxt = 1, self._idx + 1
            self._idx = max(0, min(len(self.waypoints) - 1, nxt))
        self._reset_phase()

    def _reset_phase(self):
        self._phase, self._t0, self._y0, self._retries = "approach", 0.0, None, 0

    def _dir_to_next(self, x):
        nxt = self._next_wp()
        if nxt is None:
            return 0
        d = nxt[0] - x
        return 1 if d > self.arrive_tol else (-1 if d < -self.arrive_tol else 0)

    # ================= 主逻辑：每帧输出指令 =================
    def step(self, pos, now=None):
        now = now if now is not None else time.time()
        if not self.waypoints:
            return PatrolCommand(dir=0, status="无路线")
        if pos is None:
            return PatrolCommand(dir=0, status="🧭 定位玩家点中…（黄点闪烁/被遮挡）")

        # 更新停滞计时
        if (self._last_pos is None or
                abs(pos[0] - self._last_pos[0]) >= MOVE_EPS or
                abs(pos[1] - self._last_pos[1]) >= MOVE_EPS):
            self._move_t = now
        self._last_pos = pos

        x, y = pos
        wx, wy, act = self._wp(self._idx)
        n = len(self.waypoints)

        # ---------- 行走（只判水平） ----------
        if act == WALK:
            self._reset_phase()
            if abs(x - wx) <= self.arrive_tol:
                self.advance()
                return PatrolCommand(status="✔ 到点 → 下一路点")
            return PatrolCommand(dir=1 if wx > x else -1,
                                 status=f"🚶 路点 {self._idx + 1}/{n}")

        # ---------- 跳跃 ----------
        if act == JUMP:
            if self._phase == "approach":
                if abs(x - wx) <= self.arrive_tol:
                    self._phase, self._t0 = "jumping", now
                    return PatrolCommand(jump=True, dir=self._dir_to_next(x),
                                         status="🦘 起跳")
                return PatrolCommand(dir=1 if wx > x else -1,
                                     status=f"🚶→🦘 路点 {self._idx + 1}/{n}")
            if now - self._t0 >= self.air_time:  # 空中惯性结束
                self.advance()
                return PatrolCommand(status="🦘 落地 → 下一路点")
            return PatrolCommand(dir=self._dir_to_next(x), status="🦘 跳跃中")

        # ---------- 绳索 ----------
        grab_key = "up" if act == ROPE_UP else "down"

        if self._phase == "backoff":  # 抓绳失败退开重试
            if now - self._t0 >= 0.35:
                self._phase = "approach"
                return PatrolCommand(dir=0, status="🪢 重新对位")
            return PatrolCommand(dir=self._backoff_dir, status="🪢 退开重试")

        if self._phase == "approach":
            if abs(x - wx) <= self.grab_tol:
                self._phase, self._t0, self._y0 = "grabbing", now, y
                return PatrolCommand(dir=0, climb=grab_key, status="🪢 抓绳…")
            return PatrolCommand(dir=1 if wx > x else -1,
                                 status=f"🚶→🪢 路点 {self._idx + 1}/{n}")

        if self._phase == "grabbing":
            if self._y0 is not None and abs(y - self._y0) >= 2:
                self._phase, self._t0, self._y0 = "climbing", now, y  # y 变化=已上绳
                return PatrolCommand(dir=0, climb=grab_key, status="🪢 攀爬中")
            if now - self._t0 >= self.grab_timeout:
                self._retries += 1
                if self._retries >= self.max_retries:
                    self.advance()
                    return PatrolCommand(climb=None, dir=0,
                                         status="⚠ 抓绳失败，跳过该路点")
                self._backoff_dir = 1 if self._retries % 2 else -1
                self._phase, self._t0 = "backoff", now
                return PatrolCommand(climb=None, dir=0, status="🪢 抓绳未果，退开重试")
            return PatrolCommand(dir=0, climb=grab_key, status="🪢 抓绳…")

        # climbing：按住上/下直到到达目标高度
        reached = (y <= wy + self.arrive_tol) if act == ROPE_UP \
            else (y >= wy - self.arrive_tol)
        if reached:
            self.advance()
            return PatrolCommand(climb=None, dir=0, status="🪢 到位 → 下一路点")
        if self._y0 is None or abs(y - self._y0) >= 1:
            self._y0, self._t0 = y, now  # 还在爬，刷新计时
        elif now - self._t0 >= 1.6:  # 爬到绳端自动脱出等
            self.advance()
            return PatrolCommand(climb=None, dir=0,
                                 status="⚠ 绳索停滞，跳过该路点")
        return PatrolCommand(dir=0, climb=grab_key,
                             status=f"🪢 攀爬 路点 {self._idx + 1}/{n}")