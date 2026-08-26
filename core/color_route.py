# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:24
# @Author  : yy
# @File    : color_route.py
# @Software: MxdAutoLvup

"""
小地图颜色路径导航
==================
路径画在小地图快照上，颜色即指令；运行时在玩家黄点周围
search_range 像素内找【最近的】颜色标记并执行 —— 无状态导航。

颜色编码表 (RGB)
  红色 (255,0,0)    左走        蓝色 (0,0,255)    右走
  橙色 (255,127,0)  左跳        青色 (0,255,255)  右跳
  淡绿 (127,255,0)  下跳(下平台) 紫色 (255,0,255)  原地跳
  浅绿 (0,255,127)  停止        黄色 (255,255,0)  终点(计圈)
  粉色 (255,0,127)  上瞬移      深紫 (127,0,255)  下瞬移
  墨绿 (0,127,0)    左瞬移      棕色 (139,69,19)  右瞬移
  灰色 (127,127,127) 向上爬绳   浅黄 (255,255,127) 向下爬绳

爬绳流程（跳跃抓绳）：对准绳子x → 【跳跃+↑同时按】→ y变化确认上绳
→ 持续按↑攀爬 → 玩家点脱离灰色标记后由下一标记接管。
"""
import time
import cv2
import numpy as np

DEFAULT_DOT_COLOR = (60, 230, 255)      # BGR 小地图玩家黄点
BLINK_FRAMES = 12
MOVE_EPS = 1.5
NEAREST_TOL = 100       # 标记色归类阈值（14色最小间距127，安全不互串）

# (RGB, 水平, 垂直, 动作, 名称)
RAW_CODES = [
    ((255, 0, 0),     "left",  None,    None,       "左走"),
    ((0, 0, 255),     "right", None,    None,       "右走"),
    ((255, 127, 0),   "left",  None,    "jump",     "左跳"),
    ((0, 255, 255),   "right", None,    "jump",     "右跳"),
    ((127, 255, 0),   None,    "down",  "jump",     "下跳"),
    ((255, 0, 255),   None,    None,    "jump",     "原地跳"),
    ((0, 255, 127),   "stop",  "stop",  "stop",     "停止"),
    ((255, 255, 0),   None,    None,    "goal",     "终点"),
    ((255, 0, 127),   None,    "up",    "teleport", "上瞬移"),
    ((127, 0, 255),   None,    "down",  "teleport", "下瞬移"),
    ((0, 127, 0),     "left",  None,    "teleport", "左瞬移"),
    ((139, 69, 19),   "right", None,    "teleport", "右瞬移"),
    ((127, 127, 127), None,    "up",    None,       "上爬绳"),
    ((255, 255, 127), None,    "down",  None,       "下爬绳"),
]


class RouteCmd:
    """单帧导航指令（引擎负责翻译成按键）"""
    __slots__ = ("dir", "climb", "vdir", "jump", "teleport",
                 "stop", "status", "label")

    def __init__(self, dir=None, climb=None, vdir=None, jump=False,
                 teleport=None, stop=False, status="", label=""):
        self.dir = dir          # -1/0/+1；None=保持当前水平键
        self.climb = climb      # None/'up'/'down' 爬绳持续键
        self.vdir = vdir        # None/'up'/'down' 瞬时垂直键(下跳组合)
        self.jump = jump        # 点按跳跃
        self.teleport = teleport  # None/'up'/'down'/'left'/'right'
        self.stop = stop        # 全部松开
        self.status = status
        self.label = label


class ColorRouteNavigator:
    def __init__(self):
        # ---- 配置 ----
        self.minimap = (0, 0, 0, 0)
        self.dot_color = np.array(DEFAULT_DOT_COLOR, np.int16)
        self.tolerance = 80
        self.search_range = 10        # 玩家周围搜索半径（小地图px）
        self.grab_tol = 4             # 抓绳水平对准容差
        # ---- 路线 ----
        self.route = None             # 原始路线图(BGR)
        self._label = None            # 预计算标签图（每像素→颜色索引）
        self._codes = []
        self.laps = 0                 # 完成圈数
        # ---- 玩家定位运行时 ----
        self._masks = []
        self._last_pos = None
        self._move_t = time.time()
        # ---- 爬绳状态机 ----
        self._phase = "none"          # none/align/grab/climb
        self._t0 = 0.0
        self._y0 = None
        self._retries = 0
        # ---- 动作冷却 ----
        self._jump_t = 0.0
        self._tp_t = 0.0
        self._goal_t = 0.0

    # ================= 配置 =================
    def configure(self, minimap=None, dot_color=None, tolerance=None,
                  search_range=None, grab_tol=None):
        if minimap and minimap[2] > 4 and minimap[3] > 4:
            self.minimap = tuple(int(v) for v in minimap)
        if dot_color:
            self.dot_color = np.array(dot_color, np.int16)
        if tolerance is not None:
            self.tolerance = int(tolerance)
        if search_range is not None:
            self.search_range = max(3, int(search_range))
        if grab_tol is not None:
            self.grab_tol = max(2, int(grab_tol))

    @property
    def ready(self):
        return self._label is not None and self.minimap[2] > 4

    @property
    def phase(self):
        return self._phase

    # ================= 路线加载 =================
    def load(self, route_bgr):
        """加载颜色路线图（黑底+纯色标记）→ 预计算标签图（最近色归类）"""
        if route_bgr is None:
            return
        self.route = route_bgr
        h, w = route_bgr.shape[:2]
        img = route_bgr.astype(np.int16)
        colors = np.array([list(c[::-1]) for (c, *_) in RAW_CODES], np.int16)
        dist = np.zeros((h, w, len(colors)), np.int32)
        for ch in range(3):
            dist += np.abs(img[:, :, ch:ch + 1] - colors[None, None, :, ch])
        best, bestd = dist.argmin(2), dist.min(2)
        lab = np.full((h, w), -1, np.int8)
        hit = bestd < NEAREST_TOL
        lab[hit] = best[hit].astype(np.int8)
        self._label = lab
        self._codes = [(hh, v, act, nm) for (_, hh, v, act, nm) in RAW_CODES]
        self.laps = 0
        self._reset_climb()

    def _reset_climb(self):
        self._phase, self._t0, self._y0, self._retries = "none", 0.0, None, 0

    def back_to_align(self):
        """外部（主画面绳子精调）强制回到对准阶段重试"""
        if self._phase in ("grab", "climb"):
            self._phase = "align"
            self._retries += 1

    # ================= 玩家定位（黄点·最近邻追踪） =================
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
        pos = self._track(mask)                              # 当前帧优先
        if pos is None and self._masks:
            pos = self._track(np.maximum.reduce(self._masks))  # 闪烁兜底
        return pos

    def _track(self, mask):
        """候选质心中：有历史→最近邻（抗NPC黄点）；无→最大面积"""
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

    # ================= 主逻辑 =================
    def step(self, pos, now=None):
        now = now if now is not None else time.time()
        if not self.ready:
            return RouteCmd(status="未加载颜色路线")
        if pos is None:
            self._reset_climb()
            return RouteCmd(status="🧭 定位玩家点中…")
        if (self._last_pos is None or
                abs(pos[0] - self._last_pos[0]) >= MOVE_EPS or
                abs(pos[1] - self._last_pos[1]) >= MOVE_EPS):
            self._move_t = now
        self._last_pos = pos
        x, y = int(round(pos[0])), int(round(pos[1]))

        mk = self._nearest_mark(x, y)
        if mk is None:
            return RouteCmd(status="⚠ 附近无路径标记（偏离路线）")
        idx, mx, my, _ = mk
        hh, v, act, nm = self._codes[idx]

        # 终点：计圈
        if act == "goal":
            if now - self._goal_t > 8:
                self.laps += 1
                self._goal_t = now
            return RouteCmd(status=f"🏁 终点 · 已完成 {self.laps} 圈")
        # 停止点
        if act == "stop":
            return RouteCmd(stop=True, status=f"⏸ 停止点")
        # 瞬移（法师）
        if act == "teleport":
            self._reset_climb()
            if now - self._tp_t > 1.5:
                self._tp_t = now
                return RouteCmd(teleport=(v or hh),
                                dir={"left": -1, "right": 1}.get(hh),
                                status=f"✨ {nm}")
            return RouteCmd(status=f"✨ {nm}(冷却)")
        # 爬绳（灰↑ / 浅黄↓）
        if act is None and v in ("up", "down"):
            return self._climb(v, x, y, mx, now, nm)

        # 普通移动（走/跳）
        self._reset_climb()
        cmd = RouteCmd(dir={"left": -1, "right": 1}.get(hh), label=nm)
        if act == "jump":
            cmd.vdir = v if v in ("up", "down") else None
            if now - self._jump_t > 0.55:
                self._jump_t = now
                cmd.jump = True
        cmd.status = ("🦘 " if cmd.jump else "🚶 ") + nm + \
            (f" · 第{self.laps}圈" if self.laps else "")
        return cmd

    def _nearest_mark(self, x, y):
        """玩家周围 search_range 内最近的标记 → (索引, mx, my, 距离)"""
        r = self.search_range
        lab = self._label
        h, w = lab.shape
        x0, x1 = max(0, x - r), min(w, x + r)
        y0, y1 = max(0, y - r), min(h, y + r)
        win = lab[y0:y1, x0:x1]
        ys, xs = np.nonzero(win >= 0)
        if len(xs) == 0:
            return None
        dx, dy = xs + x0 - x, ys + y0 - y
        d2 = dx * dx + dy * dy
        i = int(np.argmin(d2))
        return int(win[ys[i], xs[i]]), int(xs[i] + x0), int(ys[i] + y0), \
            float(np.sqrt(d2[i]))

    def _climb(self, way, x, y, mx, now, nm):
        """爬绳状态机：align对准 → grab跳跃抓绳/挂绳 → climb攀爬"""
        # —— 攀爬中：y 持续变化则按住；停滞超时=爬到头，交给下一标记 ——
        if self._phase == "climb":
            if now - self._move_t < 0.6:
                self._t0 = now
                return RouteCmd(climb=way, status=f"🪢 攀爬中[{nm}]")
            if now - self._t0 > 1.2:
                self._reset_climb()
                return RouteCmd(status="🪢 攀爬结束")
            return RouteCmd(climb=way, status=f"🪢 攀爬中[{nm}]")

        # —— 抓绳等待：y 变化≥2 即确认上绳 ——
        if self._phase == "grab":
            if self._y0 is not None and abs(y - self._y0) >= 2:
                self._phase, self._t0 = "climb", now
                return RouteCmd(climb=way, status="🪢 已上绳")
            if now - self._t0 > 0.9:
                self._retries += 1
                if self._retries >= 4:
                    self._reset_climb()
                    return RouteCmd(status="⚠ 抓绳多次失败，暂停该动作")
                self._phase = "align"
                return RouteCmd(status="🪢 未抓住绳，重新对位")
            return RouteCmd(climb=way, status="🪢 抓绳中…")

        # —— 对准绳子 x（小地图标记位置） ——
        if abs(x - mx) > self.grab_tol:
            self._phase = "align"
            return RouteCmd(dir=1 if mx > x else -1,
                            status=f"🪢 对准绳子…[{nm}]")

        # —— 已对准：上绳=跳跃+↑同时按；下绳=按住↓自动挂绳 ——
        self._phase, self._t0, self._y0 = "grab", now, y
        if way == "up":
            return RouteCmd(jump=True, climb="up",
                            status="🦘+🪢 跳跃抓绳")
        return RouteCmd(climb="down", status="🪢 挂绳下滑")

        # ================= 外部接口（战斗/脱困联动） =================
    def touch(self):
        """战斗输出或脱困动作后调用：
        刷新移动计时（防止战斗耗时被误判为停滞）+ 复位爬绳状态机
        （战斗可能打断在攀爬中，战后必须从重新对准开始）"""
        self._move_t = time.time()
        self._reset_climb()

    def stuck_seconds(self):
        """玩家点持续未移动的秒数（巡逻脱困判断用）"""
        return max(0.0, time.time() - self._move_t) if self._last_pos is not None else 0.0