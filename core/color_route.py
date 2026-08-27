# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:24
# @Author  : yy
# @File    : color_route.py
# @Software: MxdAutoLvup

"""小地图颜色路径导航 v5
v5 变更：
  ① 追击防脱线配套：off_route_distance() 玩家点离最近标记距离
  ② 玩家点定位放宽+兜底：闪烁率上限 0.85→0.92；无闪烁簇时唯一常亮候选
     兜底锁定；持续无候选 → 诊断日志（on_log 回调注入引擎日志）
  ③ 爬绳时序修正：到位先停步 0.15s → 原地跳+↑ 抓绳（旧版带着横移速度
     起跳容易冲过绳子）；抓绳失败 backoff 退开重试
  ④ 检测阈值放宽：S≥80 / V≥120，色相区间 ±10
"""
import time
import cv2
import numpy as np

DEFAULT_DOT_COLOR = (60, 230, 255)
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
    DOT_S_MIN = 80  # 玩家点最低饱和度（米黄底色 S≈40，被滤除）
    DOT_V_MIN = 120  # 玩家点最低亮度
    DOT_SIDE = 8  # 玩家点最大边长（滤掉平台黄线等长条）

    def __init__(self):
        # ---- 配置 ----
        self.minimap = (0, 0, 0, 0)
        self.dot_color = np.array(DEFAULT_DOT_COLOR, np.int16)
        self.tolerance = 80
        self.dot_max_area = 40
        self.search_range = 10
        self.grab_tol = 4
        # ---- 日志回调（引擎注入） ----
        self.on_log = None
        # ---- 路线 ----
        self.route = None
        self._label = None
        self._codes = []
        self._marks_xy = None  # 标记点坐标数组（off_route 距离计算）
        self.laps = 0
        self._stop_idx = -1
        self._has_stop = False
        # ---- 玩家定位运行时 ----
        self._masks = []
        self._last_pos = None
        self._move_t = time.time()
        self._dot_seen_t = -99.0
        self._acq = None
        self._h_lo, self._h_hi = 20, 33
        self._no_cand_t = 0.0
        self._no_cand_log_t = -99.0
        self.debug_cands = []
        # ---- 爬绳状态机 ----
        self._phase = "none"
        self._t0 = 0.0
        self._y0 = None
        self._retries = 0
        self._grab_jumped = False
        self._backoff_dir = 1
        # ---- 动作冷却 ----
        self._jump_t = 0.0
        self._tp_t = 0.0
        self._goal_t = 0.0
        # ---- 跟随运行时 ----
        self._cur_dir = 0
        self._last_mark = None
        self._last_seen_t = -99.0
        self._consumed = {}
        self._update_hue()

    @staticmethod
    def _parse_color(c):
        """容错解析玩家点颜色 → (B,G,R) 三元组；无法解析返回 None。
        合法形态：[b,g,r]/(b,g,r) 各 0~255；
        兼容 '60,230,255'、'[60, 230, 255]' 等字符串形态。
        任何越界值/超大整数/畸形类型 → None（不崩溃）。"""
        try:
            if isinstance(c, str):
                parts = c.strip().strip("()[]").replace(",", " ").split()
                vals = [int(round(float(x))) for x in parts]
            else:
                a = np.asarray(c, dtype=object).ravel()
                vals = [int(round(float(x))) for x in a[:3]]
            if len(vals) == 3 and all(0 <= v <= 255 for v in vals):
                return tuple(vals)
        except (ValueError, TypeError, OverflowError):
            pass
        return None

    # ================= 配置 =================
    def configure(self, minimap=None, dot_color=None, tolerance=None,
                  search_range=None, grab_tol=None, dot_max_area=None):
        if minimap and minimap[2] > 4 and minimap[3] > 4:
            self.minimap = tuple(int(v) for v in minimap)
        if dot_color is not None:
            parsed = self._parse_color(dot_color)
            if parsed is not None:
                self.dot_color = np.array(parsed, np.int16)
                self._update_hue()
            else:
                # 坏值不再崩溃：忽略 + 日志提示 + 保留当前颜色（默认黄）
                self._log(f"玩家点颜色配置无效({dot_color!r})，已忽略，"
                          "请重新「校准小地图」采样", "warn")
        if tolerance is not None:
            self.tolerance = int(tolerance)
        if search_range is not None:
            self.search_range = max(3, int(search_range))
        if grab_tol is not None:
            self.grab_tol = max(2, int(grab_tol))
        if dot_max_area is not None:
            self.dot_max_area = max(8, int(dot_max_area))

    def _update_hue(self):
        px = np.array(self.dot_color, np.uint8).reshape(1, 1, 3)
        h = int(cv2.cvtColor(px, cv2.COLOR_BGR2HSV)[0, 0, 0])
        self._h_lo, self._h_hi = max(0, h - 10), min(179, h + 10)

    def _log(self, msg, level="warn"):
        if self.on_log:
            self.on_log(msg, level)

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
        # 预存标记点（下采样控制数量），供 off_route_distance 使用
        ys, xs = np.nonzero(lab >= 0)
        if len(xs):
            step = max(1, len(xs) // 2000)
            self._marks_xy = np.stack([xs[::step], ys[::step]],
                                      1).astype(np.float32)
        else:
            self._marks_xy = None
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
        self._last_pos = None
        self._acq = None
        self._reset_climb()

    def _reset_climb(self):
        self._phase, self._t0, self._y0, self._retries = "none", 0.0, None, 0
        self._grab_jumped = False

    def back_to_align(self):
        if self._phase in ("grab", "climb"):
            self._phase = "align"
            self._retries += 1

    def touch(self):
        self._move_t = time.time()
        self._reset_climb()

    def stuck_seconds(self):
        return max(0.0, time.time() - self._move_t) if self._last_pos is not None else 0.0

    # ================= 玩家定位 =================
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
        # 无候选诊断（定位失败的直接原因反馈）
        if not cands:
            if self._no_cand_t == 0.0:
                self._no_cand_t = now
            elif now - self._no_cand_t > 3.0 and now - self._no_cand_log_t > 15.0:
                self._no_cand_log_t = now
                self._log("小地图内未检出玩家黄点：请重新「校准小地图」（会重新采样玩家点颜色），"
                          "或调大参数页「玩家点面积」", "warn")
        else:
            self._no_cand_t = 0.0
        self._masks.append(mask)
        if len(self._masks) > BLINK_FRAMES:
            self._masks.pop(0)
        # —— 已锁定：最近邻追踪 ——
        if self._last_pos is not None:
            if cands:
                self._dot_seen_t = now
                return self._pick(cands)
            if self._masks:  # 闪烁熄灭帧：累积掩码兜底
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
            self._last_pos = pos
            self._log(f"🧭 玩家点已锁定 ({pos[0]:.0f},{pos[1]:.0f})，开始路线导航", "ok")
        return pos

    def _dot_candidates(self, roi):
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
        """初始捕获：优先闪烁簇（玩家点闪烁率 0.15~0.92，NPC 常亮≈1.0）；
        无闪烁簇时，仅存在唯一常亮候选则兜底锁定（无 NPC 地图场景）"""
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
        blink, steady = [], []
        for (_, _), (sx, sy, cnt, sa) in pts.items():
            ratio = cnt / total
            pos = (sx / cnt, sy / cnt, sa / max(1, cnt))
            if 0.15 <= ratio <= 0.92:
                blink.append(pos)
            elif ratio > 0.92:
                steady.append(pos)
        self._acq = None
        if blink:
            best = max(blink, key=lambda p: p[2])  # 面积最大的闪烁簇
            return (best[0], best[1])
        if len(steady) == 1:  # 唯一常亮 → 兜底锁定
            self._log("玩家点定位：无闪烁簇，采用唯一常亮候选兜底锁定"
                      "（若位置有误请重新校准小地图）", "warn")
            return (steady[0][0], steady[0][1])
        return None

    def auto_sample(self, frames):
        """多帧采样玩家黄点颜色：只取小而紧凑的高饱和亮黄斑块像素中位色"""
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
            if not m.any():
                continue
            n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
            for i in range(1, n):
                a = int(stats[i][cv2.CC_STAT_AREA])
                if 2 <= a <= 60 and stats[i][cv2.CC_STAT_WIDTH] <= 8 \
                        and stats[i][cv2.CC_STAT_HEIGHT] <= 8:
                    sel = roi[labels == i]  # 用标签图选区（不是掩码）
                    if sel.size:  # 空选区不入列
                        pixels.append(sel)
        if not pixels:
            return None
        med = np.median(np.vstack(pixels), axis=0).astype(np.float64)
        if np.isnan(med).any():  # NaN 防线
            self._log("玩家点颜色采样异常(NaN)，保留原颜色", "warn")
            return None
        med = np.clip(med, 0, 255).astype(int)
        self.dot_color = np.array(med, dtype=np.int16)
        self._update_hue()
        return med.tolist()

    # ================= 偏离检测（追击限距配套） =================
    def off_route_distance(self, pos):
        """玩家点离最近路线标记的距离（小地图px）；无路线返回 0"""
        if self._marks_xy is None or pos is None:
            return 0.0
        p = np.array([pos[0], pos[1]], np.float32)
        return float(np.sqrt(((self._marks_xy - p) ** 2).sum(1)).min())

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
        if len(self._consumed) > 400:
            self._consumed = {k: t for k, t in self._consumed.items() if t > now}
        active, consumed = [], []
        for c in cands:
            d, idx, mx, my = c
            act = self._codes[idx][2]
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
        # —— 攀爬中：y 持续变化则按住，停滞超时交给下一标记 ——
        if self._phase == "climb":
            if now - self._move_t < 0.6:
                self._t0 = now
                return RouteCmd(climb=way, status=f"🪢 攀爬中[{nm}]")
            if now - self._t0 > 1.2:
                self._reset_climb()
                return RouteCmd(status="🪢 攀爬结束")
            return RouteCmd(climb=way, status=f"🪢 攀爬中[{nm}]")
        # —— 退避重试：反向走开 0.3s 再回来重新对位 ——
        if self._phase == "backoff":
            if now - self._t0 >= 0.3:
                self._phase = "align"
                return RouteCmd(dir=0, status="🪢 重新对位")
            return RouteCmd(dir=self._backoff_dir, status="🪢 退开重试")
        # —— 抓绳确认：y 变化 ≥2 = 已上绳 ——
        if self._phase == "grab":
            if self._y0 is not None and abs(y - self._y0) >= 2:
                self._phase, self._t0 = "climb", now
                return RouteCmd(climb=way, status="🪢 已上绳")
            if now - self._t0 > 0.9:
                self._retries += 1
                if self._retries >= 4:
                    self._reset_climb()
                    return RouteCmd(status="⚠ 抓绳多次失败，暂停该动作")
                self._backoff_dir = 1 if self._retries % 2 else -1
                self._phase, self._t0 = "backoff", now
                return RouteCmd(dir=0, status="🪢 抓绳未果，退开重试")
            # 停稳 0.15s 后原地起跳抓绳（只跳一次）；下绳则直接按住↓挂绳
            if way == "up":
                if not self._grab_jumped and now - self._t0 >= 0.15:
                    self._grab_jumped = True
                    return RouteCmd(dir=0, climb="up", jump=True,
                                    status="🦘+🪢 原地跳抓绳")
                return RouteCmd(dir=0, climb="up", status="🪢 抓绳中…")
            return RouteCmd(dir=0, climb="down", status="🪢 挂绳下滑")
        # —— 对准绳子 x ——
        if abs(x - mx) > self.grab_tol:
            self._phase = "align"
            return RouteCmd(dir=1 if mx > x else -1,
                            status=f"🪢 对准绳子…[{nm}]")
        # —— 到位：先停步再抓（带横移速度起跳容易冲过绳子） ——
        self._phase, self._t0, self._y0, self._grab_jumped = "grab", now, y, False
        return RouteCmd(dir=0, status="🪢 停步准备抓绳")