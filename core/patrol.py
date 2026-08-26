# -*- coding: utf-8 -*-
# @Time    : 26/8/26 20:57
# @Author  : yy
# @File    : patrol.py
# @Software: MxdAutoLvup

"""
小地图巡逻导航
==============
游戏相机跟随玩家，主画面坐标随移动漂移，不能作为路点坐标；
小地图固定在屏幕左上角，坐标系稳定 —— 因此：
  · 路点   = 小地图上的坐标
  · 玩家位 = 小地图上的黄色玩家点（最大连通域质心）
  · 巡逻   = 朝当前路点按左右键，到达后推进下一个路点

黄点会闪烁 → 当前帧优先 + 滚动累积掩码兜底 双保险定位。
"""
import time
import cv2
import numpy as np

DEFAULT_DOT_COLOR = (60, 230, 255)   # BGR · 冒险岛小地图玩家黄点
BLINK_FRAMES = 12                    # 闪烁兜底：累积最近 12 帧掩码
MOVE_EPS = 2.0                       # 判定“发生了移动”的最小位移（小地图 px）


class PatrolNavigator:
    def __init__(self):
        # ---- 配置 ----
        self.minimap = (0, 0, 0, 0)              # 小地图在屏幕上的 ROI
        self.dot_color = np.array(DEFAULT_DOT_COLOR, dtype=np.int16)
        self.tolerance = 80
        self.waypoints = []                      # [[mx, my], ...]
        self.mode = "pingpong"                   # pingpong / loop
        self.arrive_tol = 6                      # 到点判定半径（小地图 px）
        # ---- 运行时 ----
        self._idx, self._dir = 0, 1
        self._masks = []
        self._last_pos = None
        self._move_t = time.time()

    # ================= 配置 =================
    def configure(self, minimap=None, dot_color=None, tolerance=None,
                  waypoints=None, mode=None, arrive_tol=None):
        if minimap and minimap[2] > 4 and minimap[3] > 4:
            self.minimap = tuple(int(v) for v in minimap)
        if dot_color:
            self.dot_color = np.array(dot_color, dtype=np.int16)
        if tolerance is not None:
            self.tolerance = int(tolerance)
        if waypoints is not None:
            wp = [[int(a), int(b)] for a, b in waypoints]
            if wp != self.waypoints:             # 路点没变就不重置进度
                self.waypoints = wp
                self.reset()
        if mode in ("pingpong", "loop"):
            self.mode = mode
        if arrive_tol is not None:
            self.arrive_tol = max(2, int(arrive_tol))

    def reset(self):
        self._idx, self._dir = 0, 1
        self._masks, self._last_pos = [], None
        self.touch()

    def touch(self):
        """重置停滞计时（战斗输出 / 脱困动作后调用）"""
        self._move_t = time.time()

    @property
    def ready(self):
        return self.minimap[2] > 4 and len(self.waypoints) >= 2

    @property
    def index(self):
        return self._idx

    # ================= 玩家定位 =================
    def player_pos(self, frame_bgr):
        """返回玩家在小地图内的坐标；定位失败返回 None"""
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

        # 滚动累积（始终更新）；当前帧检出优先，闪烁消失时用累积掩码兜底
        self._masks.append(mask)
        if len(self._masks) > BLINK_FRAMES:
            self._masks.pop(0)
        pos = self._centroid(mask)
        if pos is None and self._masks:
            pos = self._centroid(np.maximum.reduce(self._masks))

        if pos is not None:
            if (self._last_pos is None or
                    abs(pos[0] - self._last_pos[0]) >= MOVE_EPS or
                    abs(pos[1] - self._last_pos[1]) >= MOVE_EPS):
                self._move_t = time.time()
            self._last_pos = pos
        return pos

    def stuck_seconds(self):
        """玩家点持续未移动的秒数"""
        return max(0.0, time.time() - self._move_t) if self._last_pos is not None else 0.0

    @staticmethod
    def _centroid(mask):
        """最大连通域质心；面积 < 2 视为噪声"""
        n = cv2.connectedComponentsWithStats(mask, 8)
        if n[0] < 2:
            return None
        best = 1 + int(np.argmax(n[2][1:, cv2.CC_STAT_AREA]))
        if n[2][best][cv2.CC_STAT_AREA] < 2:
            return None
        return (float(n[3][best][0]), float(n[3][best][1]))

    def auto_sample(self, frames):
        """多帧自动采样玩家黄点颜色（BGR 中位色），失败返回 None"""
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
            m = cv2.inRange(hsv, (18, 120, 150), (38, 255, 255))  # 高饱和亮黄
            if m.any():
                pixels.append(roi[m > 0])
        if not pixels:
            return None
        med = np.median(np.vstack(pixels), axis=0).astype(int)
        self.dot_color = np.array(med, dtype=np.int16)
        return med.tolist()

    # ================= 路点推进 =================
    def arrived(self, pos):
        if not pos or not self.waypoints:
            return False
        tx, ty = self.waypoints[self._idx]
        return (abs(pos[0] - tx) <= self.arrive_tol and
                abs(pos[1] - ty) <= self.arrive_tol)

    def direction(self, pos):
        """+1 需向右 / -1 需向左 / 0 已对齐或无法判定"""
        if not pos or not self.waypoints:
            return 0
        d = self.waypoints[self._idx][0] - pos[0]
        if abs(d) <= self.arrive_tol:
            return 0
        return 1 if d > 0 else -1

    def advance(self):
        if len(self.waypoints) < 2:
            return
        if self.mode == "loop":
            self._idx = (self._idx + 1) % len(self.waypoints)
        else:                                    # pingpong：到头折返
            nxt = self._idx + self._dir
            if nxt >= len(self.waypoints):
                self._dir, nxt = -1, self._idx - 1
            elif nxt < 0:
                self._dir, nxt = 1, self._idx + 1
            self._idx = max(0, min(len(self.waypoints) - 1, nxt))