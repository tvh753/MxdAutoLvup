# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:24
# @Author  : yy
# @File    : color_route.py
# @Software: MxdAutoLvup

# -*- coding: utf-8 -*-
"""小地图颜色路径导航 v4 —— 玩家点检测重构版
v4 修复「玩家恒在地图中央」：
  旧版 BGR 距离匹配黄点 → 小地图米黄底色/黄褐平台线同被命中
  → 整块底色连成巨大连通域 → 质心≈地图几何中心 → 位置恒为中央。
  新管线：
    ① HSV 高饱和亮黄(S≥90, V≥140, 色相=采样色±8)   滤除低饱和米黄底色
    ② 连通域面积(2~dot_max_area)+边长(≤8)双过滤      滤除平台黄线/图标
    ③ 初始捕获：12帧闪烁判别（玩家点闪烁 0.15~0.85 出现率；NPC 常亮≈1.0）
    ④ 锁定后：最近邻追踪 + 累积掩码兜底 + 末位位置兜底(1.5s)
"""
import time
import cv2
import numpy as np

DEFAULT_DOT_COLOR = (60, 230, 255)  # BGR 玩家黄点
BLINK_FRAMES = 12
MOVE_EPS = 1.5
NEAREST_TOL = 100
LOST_MOMENTUM = 0.8
CONSUME_T = 2.5
BEHIND_FREE = 6
RAW_CODES = [
    ((255, 0, 0), "left", None, None, "左走"),
    ((0, 0, 255), "right", None, None, "右走"),
    ((255, 127, 0), "left", None, "jump", "左跳"),
    ((0, 255, 255), "right", None, "jump", "右跳"),
    ((127, 255, 0), None, "down", "jump", "下跳"),
    ((255, 0, 255), None, None, "jump", "原地跳"),
    ((0, 255, 127), "stop", "stop", "stop", "停止"),
    ((255, 255, 0), None, None, "goal", "终点"),
    ((255, 0, 127), None, "up", "teleport", "上瞬移"),
    ((127, 0, 255), None, "down", "teleport", "下瞬移"),
    ((0, 127, 0), "left", None, "teleport", "左瞬移"),
    ((139, 69, 19), "right", None, "teleport", "右瞬移"),
    ((127, 127, 127), None, "up", None, "上爬绳"),
    ((255, 255, 127), None, "down", None, "下爬绳"),
]
CONSUMABLE = {"jump", "teleport"}


class RouteCmd:
    __slots__ = ("dir", "climb", "vdir", "jump", "teleport",
                 "stop", "status", "label")

    def __init__(self, dir=None, climb=None, vdir=None, jump=False,
                 teleport=None, stop=False, status="", label=""):
        self.dir, self.climb, self.vdir = dir, climb, vdir
        self.jump, self.teleport, self.stop = jump, teleport, stop
        self.status, self.label = status, label


class ColorRouteNavigator:
    DOT_S_MIN = 90  # 玩家点最低饱和度（米黄底色 S≈40，被滤除）
    DOT_V_MIN = 140  # 玩家点最低亮度
    DOT_SIDE = 8  # 玩家点最大边长（滤掉平台黄线等长条）

    def __init__(self):
        # ---- 配置 ----
        self.minimap = (0, 0, 0, 0)
        self.dot_color = np.array(DEFAULT_DOT_COLOR, np.int16)
        self.tolerance = 80
        self.dot_max_area = 40  # 玩家点面积上限 px²
        self.search_range = 10
        self.grab_tol = 4
        # ---- 路线 ----
        self.route = None
        self._label = None
        self._codes = []
        self.laps = 0
        self._stop_idx = -1
        self._has_stop = False
        # ---- 玩家定位运行时 ----
        self._masks = []
        self._last_pos = None
        self._move_t = time.time()
        self._dot_seen_t = -99.0  # 最后成功检测玩家点的时刻
        self._acq = None  # 初始捕获帧缓存
        self._h_lo, self._h_hi = 20, 33
        self.debug_cands = []  # 调试：当前帧候选点（预览面板绘制用）
        # ---- 爬绳状态机 ----
        self._phase = "none"
        self._t0 = 0.0
        self._y0 = None
        self._retries = 0
        # ---- 动作冷却 ----
        self._jump_t = 0.0
        self._tp_t = 0.0
        self._goal_t = 0.0
        # v3 跟随运行时
        self._cur_dir = 0
        self._last_mark = None
        self._last_seen_t = -99.0
        self._consumed = {}
        self._update_hue()

    # ================= 配置 =================
    def configure(self, minimap=None, dot_color=None, tolerance=None,
                  search_range=None, grab_tol=None, dot_max_area=None):
        if minimap and minimap[2] > 4 and minimap[3] > 4:
            self.minimap = tuple(int(v) for v in minimap)
        if dot_color:
            self.dot_color = np.array(dot_color, np.int16)
            self._update_hue()
        if tolerance is not None:
            self.tolerance = int(tolerance)
        if search_range is not None:
            self.search_range = max(3, int(search_range))
        if grab_tol is not None:
            self.grab_tol = max(2, int(grab_tol))
        if dot_max_area is not None:
            self.dot_max_area = max(8, int(dot_max_area))

    def _update_hue(self):
        """由采样点色计算检测色相区间（±8）"""
        px = np.array(self.dot_color, np.uint8).reshape(1, 1, 3)
        h = int(cv2.cvtColor(px, cv2.COLOR_BGR2HSV)[0, 0, 0])
        self._h_lo, self._h_hi = max(0, h - 8), min(179, h + 8)

    @property
    def ready(self):
        return self._label is not None and self.minimap[2] > 4

    @property
    def phase(self):
        return self._phase

    @property
    def has_stop(self):
        return self._has_stop

    # ================= 路线加载 =================
    def load(self, route_bgr):
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
        for i, entry in enumerate(RAW_CODES):
            if entry[3] == "stop":
                self._stop_idx = i
                break
        self._has_stop = self._stop_idx >= 0 and bool((lab == self._stop_idx).any())
        self.laps = 0
        self._consumed.clear()
        self._cur_dir = 0
        self._last_mark = None
        self._last_pos = None  # 换路线 → 重新捕获玩家
        self._acq = None
        self._reset_climb()

    def _reset_climb(self):
        self._phase, self._t0, self._y0, self._retries = "none", 0.0, None, 0

    def back_to_align(self):
        if self._phase in ("grab", "climb"):
            self._phase = "align"
            self._retries += 1

    def touch(self):
        self._move_t = time.time()
        self._reset_climb()

    def stuck_seconds(self):
        return max(0.0, time.time() - self._move_t) if self._last_pos is not None else 0.0

    # ================= 玩家定位（v4 重构） =================
    def player_pos(self, frame_bgr):
        x, y, w, h = self.minimap
        if w <= 4 or h <= 4 or frame_bgr is None:
            return None
        H, W = frame_bgr.shape[:2]
        x2, y2 = min(x + w, W), min(y + h, H)
        if x < 0 or y < 0 or x2 <= x or y2 <= y:
            return None
        roi = frame_bgr[y:y2, x:x2]
        now = time.time()
        cands, mask = self._dot_candidates(roi)
        self.debug_cands = [(c[0], c[1]) for c in cands]
        self._masks.append(mask)
        if len(self._masks) > BLINK_FRAMES:
            self._masks.pop(0)
        # —— 已锁定：最近邻追踪 ——
        if self._last_pos is not None:
            if cands:
                pos = self._pick(cands)
                self._dot_seen_t = now
                return pos
            # 闪烁熄灭帧：累积掩码兜底（放宽面积，容忍移动拖尾）
            if self._masks:
                acc = np.maximum.reduce(self._masks)
                pos = self._pick(self._cc_filter(acc, 2, self.dot_max_area * 3,
                                                 side=10 ** 6))
                if pos is not None:
                    return pos
            if now - self._dot_seen_t < 1.5:  # 短暂遮挡：沿用末位位置
                return self._last_pos
            self._last_pos = None  # 彻底丢失 → 重新捕获
            self._acq = None
            return None
        # —— 未锁定：闪烁判别初始捕获 ——
        pos = self._acquire(cands, now)
        if pos is not None:
            self._dot_seen_t = now
        return pos

    def _dot_candidates(self, roi):
        """小而紧凑的高饱和亮黄斑点（玩家/NPC 点）。
        米黄底色被 S 阈值滤除；平台黄线被面积/边长滤除。"""
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (self._h_lo, self.DOT_S_MIN, self.DOT_V_MIN),
                           (self._h_hi, 255, 255))
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        out = []
        for i in range(1, n):
            a = int(stats[i][cv2.CC_STAT_AREA])
            bw = int(stats[i][cv2.CC_STAT_WIDTH])
            bh = int(stats[i][cv2.CC_STAT_HEIGHT])
            if 2 <= a <= self.dot_max_area and bw <= self.DOT_SIDE \
                    and bh <= self.DOT_SIDE:
                out.append((float(cents[i][0]), float(cents[i][1]), a))
        return out, mask

    def _cc_filter(self, mask, a_min, a_max, side=DOT_SIDE):
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        return [(float(cents[i][0]), float(cents[i][1]),
                 int(stats[i][cv2.CC_STAT_AREA]))
                for i in range(1, n)
                if a_min <= stats[i][cv2.CC_STAT_AREA] <= a_max
                and stats[i][cv2.CC_STAT_WIDTH] <= side
                and stats[i][cv2.CC_STAT_HEIGHT] <= side]

    def _pick(self, cands):
        if not cands:
            return None
        lx, ly = self._last_pos
        return min(cands, key=lambda c: (c[0] - lx) ** 2 + (c[1] - ly) ** 2)[:2]

    def _acquire(self, cands, now):
        """初始捕获：玩家点闪烁（出现率 0.15~0.85）、NPC 常亮（≈1.0）。
        12 帧窗口内按位置聚类统计出现率，优先选闪烁簇。"""
        if self._acq is None or now - self._acq["t0"] > 2.5:
            self._acq = {"t0": now, "frames": []}
        self._acq["frames"].append(cands)
        frames = self._acq["frames"]
        if len(frames) < 12:
            return None
        pts = {}
        for fs in frames:
            seen = set()
            for cx, cy, a in fs:
                key = (int(cx) // 5, int(cy) // 5)
                if key in seen:
                    continue
                seen.add(key)
                e = pts.setdefault(key, [0.0, 0.0, 0, 0])
                e[0] += cx;
                e[1] += cy;
                e[2] += 1;
                e[3] += a
        total = len(frames)
        best, best_score = None, -1.0
        for (_, _), (sx, sy, cnt, sa) in pts.items():
            ratio = cnt / total
            if 0.15 <= ratio <= 0.85:  # 闪烁 → 玩家
                score = 2.0 + min(sa / max(1, cnt), 40) / 100
            elif ratio > 0.85:  # 常亮 → 疑似 NPC
                score = 0.5
            else:
                score = 0.0
            if score > best_score:
                best_score = score
                best = (sx / cnt, sy / cnt)
        self._acq = None
        return best

    def auto_sample(self, frames):
        """多帧采样玩家黄点颜色：只取小而紧凑的高饱和亮黄斑块像素中位色
        （旧版对整个 ROI 取黄色像素中位色，可能混入平台线颜色）"""
        x, y, w, h = self.minimap
        if w <= 4 or not frames:
            return None
        pixels = []
        for f in frames:
            if f is None:
                continue
            H, W = f.shape[:2]
            x2, y2 = min(x + w, W), min(y + h, H)
            if x < 0 or y < 0 or x2 <= x or y2 <= y:
                continue
            roi = f[y:y2, x:x2]
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            m = cv2.inRange(hsv, (15, 100, 150), (40, 255, 255))
            n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
            for i in range(1, n):
                a = stats[i][cv2.CC_STAT_AREA]
                if 2 <= a <= 60 and stats[i][cv2.CC_STAT_WIDTH] <= 8 \
                        and stats[i][cv2.CC_STAT_HEIGHT] <= 8:
                    pixels.append(roi[m == i])
        if not pixels:
            return None
        med = np.median(np.vstack(pixels), axis=0).astype(int)
        self.dot_color = np.array(med, dtype=np.int16)
        self._update_hue()
        return med.tolist()

    # ================= 停止标记（休息安全点） =================
    def near_stop(self, pos, tol=8):
        if self._label is None or pos is None or self._stop_idx < 0:
            return False
        x, y = int(pos[0]), int(pos[1])
        h, w = self._label.shape
        if not (0 <= x < w and 0 <= y < h):
            return False
        x0, x1 = max(0, x - tol), min(w, x + tol + 1)
        y0, y1 = max(0, y - tol), min(h, y + tol + 1)
        return bool((self._label[y0:y1, x0:x1] == self._stop_idx).any())

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
        cands = self._candidates(x, y, self.search_range)
        if not cands:
            cands = self._candidates(x, y, int(self.search_range * 1.8))
        if not cands:
            return self._lost(x, now)
        self._last_seen_t = now
        d, idx, mx, my = self._choose(cands, x, now)
        self._last_mark = (mx, my)
        hh, v, act, nm = self._codes[idx]
        cmd = self._dispatch(idx, mx, my, hh, v, act, nm, x, y, now)
        if cmd.dir:
            self._cur_dir = cmd.dir
        elif cmd.stop:
            self._cur_dir = 0
        return cmd

    def _candidates(self, x, y, r):
        lab = self._label
        if lab is None:
            return []
        h, w = lab.shape
        x0, x1 = max(0, x - r), min(w, x + r + 1)
        y0, y1 = max(0, y - r), min(h, y + r + 1)
        if x0 >= x1 or y0 >= y1:
            return []
        win = lab[y0:y1, x0:x1]
        ys, xs = np.nonzero(win >= 0)
        if len(xs) == 0:
            return []
        dx = (xs + x0 - x).astype(np.float32)
        dy = (ys + y0 - y).astype(np.float32)
        d = np.sqrt(dx * dx + dy * dy)
        order = np.argsort(d)[:24]
        return [(float(d[i]), int(win[ys[i], xs[i]]),
                 int(xs[i] + x0), int(ys[i] + y0)) for i in order]

    def _choose(self, cands, x, now):
        """方向迟滞选择：身后标记按距离加重惩罚；已消费的跳跃/传送标记跳过"""
        if len(self._consumed) > 400:
            self._consumed = {k: t for k, t in self._consumed.items() if t > now}
        active, consumed = [], []
        for c in cands:
            d, idx, mx, my = c
            hh, v, act, nm = self._codes[idx]
            if act in CONSUMABLE and \
                    self._consumed.get((idx, mx // 5, my // 5), 0) > now:
                consumed.append(c)
            else:
                active.append(c)
        pool = active or consumed

        def score(c):
            d, idx, mx, my = c
            s = d
            if self._cur_dir:
                behind = (mx - x) * self._cur_dir
                if behind < -BEHIND_FREE:
                    s += (-behind - BEHIND_FREE) * 2.5
            return s

        return min(pool, key=score)

    def _lost(self, x, now):
        if now - self._last_seen_t < LOST_MOMENTUM and self._cur_dir:
            return RouteCmd(dir=self._cur_dir, status="… 穿越标记间隙")
        if self._last_mark:
            dx = self._last_mark[0] - x
            if abs(dx) > 3:
                return RouteCmd(dir=1 if dx > 0 else -1, status="↩ 返回路线")
        return RouteCmd(dir=0, status="⚠ 偏离路线，原地等待")

    def _dispatch(self, idx, mx, my, hh, v, act, nm, x, y, now):
        if act in CONSUMABLE:
            self._consumed[(idx, mx // 5, my // 5)] = now + CONSUME_T
        if act == "goal":
            if now - self._goal_t > 8:
                self.laps += 1
                self._goal_t = now
            return RouteCmd(status=f"🏁 终点 · 已完成 {self.laps} 圈")
        if act == "stop":
            return RouteCmd(stop=True, status="⏸ 停止点")
        if act == "teleport":
            self._reset_climb()
            if now - self._tp_t > 1.5:
                self._tp_t = now
                return RouteCmd(teleport=(v or hh),
                                dir={"left": -1, "right": 1}.get(hh),
                                status=f"✨ {nm}")
            return RouteCmd(status=f"✨ {nm}(冷却)")
        if act is None and v in ("up", "down"):
            return self._climb(v, x, y, mx, now, nm)
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

    def _climb(self, way, x, y, mx, now, nm):
        if self._phase == "climb":
            if now - self._move_t < 0.6:
                self._t0 = now
                return RouteCmd(climb=way, status=f"🪢 攀爬中[{nm}]")
            if now - self._t0 > 1.2:
                self._reset_climb()
                return RouteCmd(status="🪢 攀爬结束")
            return RouteCmd(climb=way, status=f"🪢 攀爬中[{nm}]")
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
        if abs(x - mx) > self.grab_tol:
            self._phase = "align"
            return RouteCmd(dir=1 if mx > x else -1,
                            status=f"🪢 对准绳子…[{nm}]")
        self._phase, self._t0, self._y0 = "grab", now, y
        if way == "up":
            return RouteCmd(jump=True, climb="up", status="🦘+🪢 跳跃抓绳")
        return RouteCmd(climb="down", status="🪢 挂绳下滑")