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
        self._probe = None  # 试探定位状态机
        self._h_lo, self._h_hi = 20, 33
        self._no_cand_t = 0.0
        self._no_cand_log_t = -99.0
        self._probe_fail_log_t = -99.0
        self.debug_cands = []
        # 锁定有效性看门狗
        self._walk_t0 = None
        self._walk_p0 = None
        # ---- 滚动补偿（大地图小地图随玩家滚动，标记是录制时坐标） ----
        self._base = None
        self._shift = None  # (dx,dy)：底图内容相对当前帧的位移
        self._shift_t = -99.0
        self._bad_since = 0.0
        self._shift_log_t = -99.0
        # ---- 回归路线状态（防卡楔） ----
        self._ret = None
        self._step_pos = None  # step 内部坐标(底图系)，与跟踪器 live 坐标分离
        # ---- 爬绳状态机 ----
        self._phase = "none"
        self._t0 = 0.0
        self._y0 = None
        self._retries = 0
        self._grab_jumped = False
        self._backoff_dir = 1
        self._grab_jumped = False
        self._backoff_dir = 1
        self._jump_at = 0.0  # 起跳时刻（空中保护窗口）
        self._phase_t = 0.0  # grab/climb 最近活跃时刻（定位丢失保护）
        self._phase_way = None  # 当前爬绳方向
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
            nm = tuple(int(v) for v in minimap)
            if nm != self.minimap:
                self.minimap = nm
                self._last_pos = None
                self._probe = None
                self._shift = None
                self._ret = None
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
        self._probe = None
        self._walk_t0 = None
        self._ret = None
        self._shift = None
        self._reset_climb()

    def _reset_climb(self):
        self._phase, self._t0, self._y0, self._retries = "none", 0.0, None, 0
        self._grab_jumped = False
        self._jump_at = 0.0
        self._phase_t = 0.0
        self._phase_way = None

    def back_to_align(self):
        # 空中上升期禁止打断：外部误判会导致↑被松开、跳空
        if self._phase == "grab" and self._grab_jumped \
                and time.time() - self._jump_at < 0.6:
            return
        if self._phase in ("grab", "climb"):
            self._phase = "align"
            self._retries += 1

    def touch(self):
        self._move_t = time.time()
        self._reset_climb()

    def stuck_seconds(self):
        return max(0.0, time.time() - self._move_t) if self._step_pos is not None else 0.0

    # ================= 玩家定位 =================
    def _player_pos_raw(self, frame_bgr):
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

        # —— 未锁定：试探移动定位（v7，替代闪烁判别，低帧率可靠） ——
        if self._probe is None:
            self._probe = {"phase": "base", "t0": now, "dir": -1, "try": 0,
                           "base": [], "walk": []}
            self._log("🧭 试探定位：控制角色短暂左右移动以识别玩家点"
                      "（低帧率下比闪烁判别可靠）", "info")
        return self._probe_step(cands, now)

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

    # ================= 试探移动定位（v7） =================
    def _probe_dir(self):
        p = self._probe
        return p["dir"] if p and p["phase"] == "walk" else 0

    def _probe_step(self, cands, now):
        p = self._probe
        if p["phase"] == "base":  # 静止取基线
            p["base"].extend((c[0], c[1]) for c in cands)
            if now - p["t0"] >= 0.45:
                p["phase"], p["t0"] = "walk", now
                p["dir"] = -1 if p["try"] % 2 == 0 else 1
            return None
        p["walk"].extend((c[0], c[1]) for c in cands)
        if now - p["t0"] < 0.8:
            return None
        # 评估：找“离开了基线位置”的点 = 玩家（NPC 静止，位移≈0）
        base = np.array(p["base"], np.float32) if p["base"] \
            else np.zeros((0, 2), np.float32)
        best, best_score = None, 2.5
        for wx, wy in p["walk"]:
            if len(base):
                d = np.sqrt(((base - (wx, wy)) ** 2).sum(1))
                j = int(d.argmin())
                disp, bx = float(d[j]), float(base[j][0])
            else:  # 基线期无任何点：新出现的即玩家
                disp, bx = 30.0, wx - 10 * p["dir"]
            if disp < 2.5:
                continue
            bonus = 2.0 if (wx - bx) * p["dir"] > 0 else 1.0  # 移动方向与指令一致加分
            if disp * bonus > best_score:
                best_score, best = disp * bonus, (wx, wy)
        if best is not None:
            self._probe = None
            self._last_pos = best
            self._dot_seen_t = now
            self._log(f"🧭 玩家点已锁定(试探) ({best[0]:.0f},{best[1]:.0f})", "ok")
            return best
        p["try"] += 1  # 未找到移动点 → 换方向重试
        p["phase"], p["t0"] = "base", now
        p["base"], p["walk"] = [], []
        if p["try"] >= 6 and now - self._probe_fail_log_t > 20:
            self._probe_fail_log_t = now
            self._log("试探定位多次未找到移动点：角色可能被卡住，持续重试中"
                      "（长期无效请检查小地图区域框选）", "warn")
        return None

    def _nearest_mark(self, x, y):
        if self._marks_xy is None or len(self._marks_xy) == 0:
            return None
        d = np.sqrt(((self._marks_xy - np.float32((x, y))) ** 2).sum(1))
        i = int(d.argmin())
        return float(self._marks_xy[i][0]), float(self._marks_xy[i][1])

    def _watchdog(self, cmd, pos, now):
        """移动指令下跟踪点长期静止 → 锁到了静态物 → 丢弃重锁。
        阈值 5.0s > 回归换向 3.5s：真被墙卡时先换向绕行，别急着扔正确锁定"""
        if not cmd.dir:
            self._walk_t0 = None
            return
        if (self._walk_t0 is None or
                abs(pos[0] - self._walk_p0[0]) >= 2 or
                abs(pos[1] - self._walk_p0[1]) >= 2):
            self._walk_t0, self._walk_p0 = now, pos
            return
        if now - self._walk_t0 > 5.0:
            self._walk_t0 = None
            self._last_pos = None
            self._step_pos = None
            self._probe = None
            self._ret = None
            self._log("⚠ 跟踪点在移动指令下长期静止，疑似锁定到静态黄点，"
                      "重新试探定位", "warn")

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
            # 抓绳/攀爬中定位短暂丢失：保持↑按住别松（松开=掉绳）
            if self._phase in ("grab", "climb") and now - self._phase_t < 2.5:
                return RouteCmd(climb=self._phase_way,
                                status="🪢 攀爬中(定位暂失)…")
            self._reset_climb()
            if self._probe is not None:
                return RouteCmd(dir=self._probe_dir(), status="🔍 试探移动定位…")
            return RouteCmd(status="🧭 定位玩家点中…")
        if (self._step_pos is None or
                abs(pos[0] - self._step_pos[0]) >= MOVE_EPS or
                abs(pos[1] - self._step_pos[1]) >= MOVE_EPS):
            self._move_t = now
        self._step_pos = pos
        x, y = int(round(pos[0])), int(round(pos[1]))
        cands = self._candidates(x, y, self.search_range)
        if not cands:
            cands = self._candidates(x, y, int(self.search_range * 1.8))
        if not cands:
            cmd = self._lost(x, y, now)
            self._watchdog(cmd, pos, now)  # ★ 盲区也查错锁（v7 遗漏）
            if cmd.dir:
                self._cur_dir = cmd.dir
            return cmd
        self._last_seen_t = now
        self._ret = None  # 已回标记附近，清回归状态
        d, idx, mx, my = self._choose(cands, x, now)
        self._last_mark = (mx, my)
        hh, v, act, nm = self._codes[idx]
        cmd = self._dispatch(idx, mx, my, hh, v, act, nm, x, y, now)
        if cmd.dir:
            self._cur_dir = cmd.dir
        elif cmd.stop:
            self._cur_dir = 0
        self._watchdog(cmd, pos, now)
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

    def _lost(self, x, y, now):
        # 短时穿越标记间隙：方向惯性
        if now - self._last_seen_t < LOST_MOMENTUM and self._cur_dir:
            return RouteCmd(dir=self._cur_dir, status="… 穿越标记间隙")
        # —— 回归路线：直线受阻自动换目标/换向，不再对着墙死跳 ——
        if self._ret is None:
            tgt = self._nearest_mark(x, y)
            if tgt is None:
                return RouteCmd(dir=0, status="⚠ 路线图无可用标记")
            self._ret = {"tgt": tgt, "p": (x, y), "t": now, "flips": 0}
        r = self._ret
        if abs(x - r["p"][0]) >= 2 or abs(y - r["p"][1]) >= 2:
            r["p"], r["t"] = (x, y), now  # 有位移，刷新
        elif now - r["t"] > 3.5:  # 3.5s 没挪窝=受阻
            r["t"], r["flips"] = now, r["flips"] + 1
            if r["flips"] <= 2:
                alt = self._flip_target(x, y, r["tgt"])
                if alt:
                    r["tgt"] = alt
                    self._log("↩ 直线回归受阻，改道另一侧标记绕回", "info")
                else:
                    self._cur_dir = -(self._cur_dir or 1)
                    self._log("↩ 回归受阻，反向沿路线绕行(环线)", "info")
            else:
                r["flips"] = 0
                r["tgt"] = self._nearest_mark(x, y) or r["tgt"]
        dx = r["tgt"][0] - x
        if abs(dx) > 3:
            d = 1 if dx > 0 else -1
            return RouteCmd(dir=d, status="↩ 返回路线")
        if now - self._jump_t > 1.0:  # x已对齐但目标在异层→跳，别干站
            self._jump_t = now
            return RouteCmd(dir=0, jump=True, status="↗ 回归点在异层，跳跃尝试")
        return RouteCmd(dir=0, status="⚠ 偏离路线(异层)，尝试回层中")

    def _flip_target(self, x, y, cur):
        """换一个在另一侧的近标记（环线绕回用）"""
        if self._marks_xy is None:
            return None
        d = np.sqrt(((self._marks_xy - np.float32((x, y))) ** 2).sum(1))
        cur_side = (cur[0] - x) >= 0
        for i in np.argsort(d)[:80]:
            mx, my = self._marks_xy[i]
            if ((mx - x) >= 0) != cur_side and d[i] < 200:
                return (float(mx), float(my))
        return None

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
            self._phase_t, self._phase_way = now, way
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
        # —— 抓绳确认 ——
        if self._phase == "grab":
            self._phase_t, self._phase_way = now, way
            # 已上绳：y 高于起点≥2px 且排除起跳弧线（没抓住会落回 y≈y0）
            if self._y0 is not None and y < self._y0 - 2 and \
                    (not self._grab_jumped or now - self._jump_at > 0.5):
                self._phase, self._t0 = "climb", now
                return RouteCmd(climb=way, status="🪢 已上绳")
            if now - self._t0 > 1.6:
                self._retries += 1
                if self._retries >= 6:
                    self._reset_climb()
                    return RouteCmd(status="⚠ 抓绳多次失败，暂停该动作")
                self._backoff_dir = 1 if self._retries % 2 else -1
                self._phase, self._t0 = "backoff", now
                return RouteCmd(dir=0, status="🪢 抓绳未果，退开重试")
            if way == "up":
                if not self._grab_jumped:
                    # 先按住↑ 0.5s：站在绳底直接吸附，多数情况无需跳
                    if now - self._t0 < 0.5:
                        return RouteCmd(dir=0, climb="up",
                                        status="🪢 按住↑探测绳底…")
                    self._grab_jumped = True
                    self._jump_at = now
                    return RouteCmd(dir=0, climb="up", jump=True,
                                    status="🦘+🪢 原地跳抓绳")
                return RouteCmd(dir=0, climb="up", status="🪢 抓绳中…")
            return RouteCmd(dir=0, climb="down", status="🪢 挂绳下滑")
        # —— 对准绳子 x ——
        if abs(x - mx) > self.grab_tol:
            self._phase = "align"
            return RouteCmd(dir=1 if mx > x else -1,
                            status=f"🪢 对准绳子…[{nm}]")
        # —— 到位：先停步再抓 ——
        self._phase, self._t0, self._y0, self._grab_jumped = "grab", now, y, False
        return RouteCmd(dir=0, status="🪢 停步准备抓绳")


    def set_base(self, base_bgr):
        """注入录制时的小地图底图（滚动补偿基准）"""
        self._base = base_bgr
        self._shift = None
        self._bad_since = 0.0

    def _roi(self, frame_bgr):
        if frame_bgr is None:
            return None
        x, y, w, h = self.minimap
        if w <= 4 or h <= 4:
            return None
        H, W = frame_bgr.shape[:2]
        x2, y2 = min(x + w, W), min(y + h, H)
        if x < 0 or y < 0 or x2 <= x or y2 <= y:
            return None
        return frame_bgr[y:y2, x:x2]

    def _scroll_shift(self, roi):
        """估计当前小地图相对录制底图的滚动位移。
        底图中央 55% 区域作模板，在当前帧里找它挪到了哪 → 位移量。
        匹配置信度低/位移过大 → None（维持上次偏移）。"""
        if self._base is None:
            return None
        bh, bw = self._base.shape[:2]
        rh, rw = roi.shape[:2]
        if (bh, bw) != (rh, rw):
            return None
        ph, pw = int(rh * 0.55), int(rw * 0.55)
        py, px = (rh - ph) // 2, (rw - pw) // 2
        tpl = cv2.cvtColor(self._base[py:py + ph, px:px + pw],
                           cv2.COLOR_BGR2GRAY)
        if float(tpl.std()) < 8:  # 底图太平坦，不可靠
            return None
        live = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(live, tpl, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(res)
        if mv < 0.55:
            return None
        dx, dy = ml[0] - px, ml[1] - py
        if abs(dx) > rw // 3 or abs(dy) > rh // 3:
            return None
        return (dx, dy)

    def player_pos(self, frame_bgr):
        pos = self._player_pos_raw(frame_bgr)
        if pos is None:
            return None
        if self._base is None:
            return pos
        now = time.time()
        if now - self._shift_t > 0.7:  # 限频：约每2帧估一次
            self._shift_t = now
            roi = self._roi(frame_bgr)
            s = self._scroll_shift(roi) if roi is not None else None
            if s is not None:
                self._shift, self._bad_since = s, 0.0
            else:
                if self._bad_since == 0.0:
                    self._bad_since = now
                if self._shift is not None and now - self._bad_since > 3.0 \
                        and now - self._shift_log_t > 20.0:
                    self._shift_log_t = now
                    self._log("⚠ 小地图与录制底图对不上（可能滚出录制范围），"
                              "请让角色回到录制路线附近", "warn")
        if self._shift is None:
            return pos
        dx, dy = self._shift
        self.debug_cands = [(cx - dx, cy - dy) for (cx, cy) in self.debug_cands]
        return (pos[0] - dx, pos[1] - dy)  # live → 底图坐标系