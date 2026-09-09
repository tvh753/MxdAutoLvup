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

DEFAULT_DOT_COLOR = (0, 128, 255)  # BGR: HSV≈(30,255,255) 纯黄，匹配玩家黄点中心色
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
    DOT_S_MIN = 40  # 玩家点最低饱和度（目标HSV: S:40-255）
    DOT_V_MIN = 160  # 玩家点最低亮度（玩家黄点是最亮的黄色特征，V≥160过滤暗淡噪声）
    DOT_SIDE = 8  # 玩家点最大边长（滤掉平台黄线等长条）
    DOT_A_MIN = 3  # 玩家点最小面积（过滤1-2像素级噪声点）
    DOT_A_MAX = 18  # 玩家点最大面积（过滤平台边缘、地图装饰等大黄色块）

    def __init__(self):
        # ---- 配置 ----
        self.minimap = (0, 0, 0, 0)
        self.dot_color = np.array(DEFAULT_DOT_COLOR, np.int16)
        self.tolerance = 80
        self.dot_max_area = 40
        self.search_range = 10
        self.grab_tol = 4
        # v19: 小地图黄点模板匹配参数（用户自定义图片，用于模板匹配定位小地图黄点）
        self.dot_tpl_path = None  # 小地图黄点模板图片路径（由 BotEngine.reload_runtime 注入）
        self._dot_tpl_cache = None  # cv2.imread 缓存（路径变化时重载）
        self._dot_tpl_path_cached = None  # 上次缓存的路径
        self.dot_match_sim = 0.9  # 模板匹配相似度阈值
        self.dot_match_method = 0  # 0=TM_CCORR_NORMED, 1=TM_CCOEFF_NORMED, 2=TM_SQDIFF_NORMED
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
        self._h_lo, self._h_hi = 25, 35  # 玩家点目标色相：H:25-35（黄色窄带）
        self._no_cand_t = 0.0
        self._no_cand_log_t = -99.0
        self._probe_fail_log_t = -99.0
        self.debug_cands = []
        # v18: 锁定日志去重 — 仅在位置变化或超过 30 秒才打印，避免每帧刷屏
        self._last_lock_log = None  # (x, y, t)
        self._bgr_hit_log_t = -99.0  # Level 0/1 命中状态日志的最近打印时刻
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
        self._jump_at = 0.0  # 起跳时刻（空中保护窗口）
        self._phase_t = 0.0  # grab/climb 最近活跃时刻（定位丢失保护）
        self._phase_way = None  # 当前爬绳方向
        self._search_dir = 0  # 抓绳搜索方向：0=未搜索, -1=左, 1=右
        self._detach_sub = 0  # 脱离子阶段：0=按up, 1=按跳, 2=检查
        self._detach_pos = None  # 脱离阶段起始位置
        self._detach_retries = 0  # 脱离重试计数
        # 当前绳子段的顶端/底端 y 坐标、中心 x 坐标（进入 climb 阶段时计算并缓存）
        self._rope_top = None
        self._rope_bottom = None
        self._rope_cx = None  # 绳子中心 x，用于攀爬时 x 对齐
        self._climb_start_t = 0.0  # 进入攀爬阶段的时刻，用于超时保护
        self._climb_last_y = None  # 上一帧玩家 y，用于停滞/掉绳检测
        self._climb_stuck_t = 0.0  # y 停止变化的起始时刻，超过阈值视为掉绳
        self._detach_release = False  # detach 阶段先松开爬绳键的标志
        self._detach_t0 = 0.0  # detach 阶段按↑计时起点
        self._detach_retrigger = False  # detach 重新触发按↑的标志
        self._detach_time = 2.0  # 脱离绳子时按↑的持续时间（秒）
        # 爬绳停稳后的容错缓冲时间（秒）：
        # 仅作为绳子边界无法获取时的兜底逻辑——检测到玩家停止移动后，
        # 再等待此时长确保角色完全停稳，然后脱离绳子。
        self._climb_grace = 3.0
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
                  search_range=None, grab_tol=None, dot_max_area=None,
                  dot_s_min=None, dot_v_min=None, dot_tpl_path=None,
                  dot_match_sim=None, dot_match_method=None,
                  climb_grace=None, detach_time=None):
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
        # v15: 修复 dot_s_min / dot_v_min 参数未生效的bug
        if dot_s_min is not None:
            self.DOT_S_MIN = max(10, min(255, int(dot_s_min)))
        if dot_v_min is not None:
            self.DOT_V_MIN = max(30, min(255, int(dot_v_min)))
        # v19: 小地图黄点模板匹配参数
        if dot_tpl_path is not None and dot_tpl_path != self.dot_tpl_path:
            self.dot_tpl_path = dot_tpl_path
            self._dot_tpl_cache = None
            self._dot_tpl_path_cached = None
            self._last_pos = None  # 模板换了，重新锁定
        if dot_match_sim is not None:
            self.dot_match_sim = max(0.1, min(1.0, float(dot_match_sim)))
        if dot_match_method is not None:
            self.dot_match_method = max(0, min(2, int(dot_match_method)))
        # 爬绳停稳后的容错缓冲时间（秒）：可配置，范围 0.5~10.0，默认 3.0
        # 玩家在小地图上停止移动后，再等此时长才脱离绳子
        if climb_grace is not None:
            self._climb_grace = max(0.5, min(10.0, float(climb_grace)))
        if detach_time is not None:
            self._detach_time = max(0.5, min(10.0, float(detach_time)))

    def _update_hue(self):
        """根据玩家点BGR颜色计算HSV色相范围。
        目标HSV: H:25-35, S:40-255, V:120-255
        色相范围固定为±5，总宽10，匹配玩家黄点窄带。"""
        px = np.array(self.dot_color, np.uint8).reshape(1, 1, 3)
        h = int(cv2.cvtColor(px, cv2.COLOR_BGR2HSV)[0, 0, 0])
        self._h_lo, self._h_hi = max(0, h - 5), min(179, h + 5)

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
        # 如果之前处于爬绳状态（phase != "none"），重置移动计时，
        # 防止爬绳期间位置不变导致脱困误触发跳跃；
        # 非爬绳状态调用时不重置，保持正常脱困逻辑有效。
        was_climbing = self._phase != "none"
        self._phase, self._t0, self._y0, self._retries = "none", 0.0, None, 0
        self._grab_jumped = False
        self._jump_at = 0.0
        self._phase_t = 0.0
        self._phase_way = None
        self._search_dir = 0    # 抓绳搜索方向：0=未搜索, -1=左, 1=右
        self._detach_sub = 0  # 脱离子阶段：0=按up, 1=按跳, 2=检查
        self._detach_pos = None  # 脱离阶段起始位置
        self._detach_retries = 0  # 脱离重试计数
        # 当前绳子段的顶端/底端 y 坐标、中心 x 坐标（进入 climb 阶段时计算并缓存）
        self._rope_top = None
        self._rope_bottom = None
        self._rope_cx = None  # 绳子中心 x，用于攀爬时 x 对齐
        # 攀爬计时重置
        self._climb_start_t = 0.0
        self._climb_last_y = None
        self._climb_stuck_t = 0.0
        self._top_reached = False
        self._top_reach_t = 0.0
        # 脱离阶段状态重置
        self._detach_release = False
        self._detach_t0 = 0.0
        self._detach_retrigger = False
        if was_climbing:
            self._move_t = time.time()

    def back_to_align(self):
        # 空中上升期禁止打断：外部误判会导致↑被松开、跳空
        if self._phase == "grab" and self._grab_jumped \
                and time.time() - self._jump_at < 0.6:
            return
        # detach 阶段不打断：正在脱离绳子，打断会导致挂在绳上下不来
        if self._phase == "detach":
            return
        # detach 阶段不打断：正在脱离绳子，打断会导致挂在绳上下不来
        if self._phase == "detach":
            return
        if self._phase in ("grab", "climb", "search"):
            self._phase = "align"
            self._retries += 1

    def touch(self):
        self._move_t = time.time()
        self._reset_climb()

    def stuck_seconds(self):
        return max(0.0, time.time() - self._move_t) if self._step_pos is not None else 0.0

    # ================= 玩家定位 =================
    def _load_dot_tpl(self):
        """v19: 加载小地图黄点模板图片（带缓存）"""
        path = self.dot_tpl_path
        if not path:
            return None
        if path == self._dot_tpl_path_cached and self._dot_tpl_cache is not None:
            return self._dot_tpl_cache
        import os
        if not os.path.isfile(path):
            self._log(f"⚠ 小地图黄点模板文件不存在: {path}", "warn")
            self._dot_tpl_cache = None
            self._dot_tpl_path_cached = path
            return None
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            self._log(f"⚠ 小地图黄点模板读取失败（cv2.imread 返回 None）: {path}", "warn")
            self._dot_tpl_cache = None
            self._dot_tpl_path_cached = path
            return None
        self._dot_tpl_cache = img
        self._dot_tpl_path_cached = path
        return img

    def p_capture(self, frame_bgr, x1, y1, x2, y2):
        """v19: 从已截取的整张窗口图中裁剪小地图区域（替代原版 pyautogui 前台截图）

        与用户原版 p_capture 区别：
          - 不再用 pyautogui.screenshot（前台截图，要求游戏窗口在最前）
          - 改用项目 WindowCapture 已经截好的 frame_bgr，直接 numpy 切片
          - 不需要 win_x/win_y 偏移（frame_bgr 已经是窗口内坐标系）
        返回：BGR 格式的 numpy 数组（与原版一致）
        """
        if frame_bgr is None:
            return None
        H, W = frame_bgr.shape[:2]
        x1 = max(0, min(int(x1), W))
        y1 = max(0, min(int(y1), H))
        x2 = max(0, min(int(x2), W))
        y2 = max(0, min(int(y2), H))
        if x2 <= x1 or y2 <= y1:
            return None
        return frame_bgr[y1:y2, x1:x2].copy()

    def p_findpic(self, frame_bgr, x1, y1, x2, y2, template_img,
                  similarity=0.9, method=0):
        """v19: 模板匹配定位玩家（适配后台窗口）

        与用户原版 p_findpic 区别：
          - template 参数从「路径」改为「已加载的 numpy 图像」，避免每帧 imread
          - 内部截图改为 p_capture(frame_bgr, ...)（不再用 pyautogui）
          - 返回值：相对于 (x1, y1) 裁剪区域左上角的中心点坐标 (cx, cy)
            注意：这里不再像原版那样加回 x1（因为 _player_pos_raw 期望
            返回「小地图 roi 内的相对坐标」用于 NAV 面板绘制）

        参数:
            frame_bgr: 整张窗口截图（BGR）
            x1, y1, x2, y2: 小地图在窗口中的绝对坐标
            template_img: cv2.imread 加载的 BGR 模板图像
            similarity: 相似度阈值，默认 0.9
            method: 0=TM_CCORR_NORMED, 1=TM_CCOEFF_NORMED, 2=TM_SQDIFF_NORMED

        返回: (cx, cy, score) 匹配中心点（相对于 x1,y1 的坐标）+ 分数；
              未匹配返回 None
        """
        if template_img is None:
            return None
        find_img = self.p_capture(frame_bgr, x1, y1, x2, y2)
        if find_img is None:
            return None
        th, tw = template_img.shape[:2]
        fh, fw = find_img.shape[:2]
        if th > fh or tw > fw:
            return None  # 模板比目标区域还大，无法匹配
        if method == 0:
            res = cv2.matchTemplate(find_img, template_img, cv2.TM_CCORR_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            if max_val >= similarity:
                cx = int(max_loc[0] + tw // 2)
                cy = int(max_loc[1] + th // 2)
                return (cx, cy, float(max_val))
            return None
        elif method == 1:
            res = cv2.matchTemplate(find_img, template_img, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            if max_val >= similarity:
                cx = int(max_loc[0] + tw // 2)
                cy = int(max_loc[1] + th // 2)
                return (cx, cy, float(max_val))
            return None
        elif method == 2:
            res = cv2.matchTemplate(find_img, template_img, cv2.TM_SQDIFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            # SQDIFF: 越小越相似，0=完全一致
            # 为了和 similarity 语义一致（越大越相似），把相似度转成 (1 - min_val)
            score = 1.0 - float(min_val)
            if score >= similarity:
                cx = int(min_loc[0] + tw // 2)
                cy = int(min_loc[1] + th // 2)
                return (cx, cy, score)
            return None
        return None

    def _player_pos_raw(self, frame_bgr):
        """v19: 玩家定位 — 改用用户自定义模板匹配 (p_findpic)

        工作流程：
          1) 加载小地图黄点模板图片（带缓存，路径变化才重载）
          2) 在小地图区域做 cv2.matchTemplate 模板匹配
          3) 匹配分数 >= similarity 阈值 → 锁定玩家位置（小地图内相对坐标）
          4) 绿十字在 NAV 面板用 pos * scale 直接绘制

        返回: (cx, cy) 小地图内相对坐标；未匹配返回 None

        优势：
          - 不依赖黄点颜色校准，用户给一张玩家小图模板就行
          - 后台窗口也能跑（不依赖 pyautogui 前台截图）
          - 无需 probe 试探定位，匹配成功即锁定
        """
        x, y, w, h = self.minimap
        if w <= 4 or h <= 4 or frame_bgr is None:
            return None
        now = time.time()

        # ---- 步骤1: 加载玩家模板 ----
        tpl = self._load_dot_tpl()
        if tpl is None:
            # 模板未配置：30秒提示一次
            if now - getattr(self, "_tpl_no_cfg_log_t", -99.0) > 30.0:
                self._tpl_no_cfg_log_t = now
                self._log("⚠ 未配置小地图黄点模板路径（dot_tpl_path），"
                          "请设置小地图黄点模板图片路径", "warn")
            return None

        # ---- 步骤2: 模板匹配定位玩家 ----
        # x2, y2 是小地图在 frame_bgr 中的右下角坐标
        H, W = frame_bgr.shape[:2]
        x2, y2 = min(x + w, W), min(y + h, H)
        if x < 0 or y < 0 or x2 <= x or y2 <= y:
            return None

        result = self.p_findpic(frame_bgr, x, y, x2, y2, tpl,
                                similarity=self.dot_match_sim,
                                method=self.dot_match_method)
        if result is None:
            # 匹配失败：30秒提示一次
            if now - getattr(self, "_match_fail_log_t", -99.0) > 30.0:
                self._match_fail_log_t = now
                self._log(f"⚠ 模板匹配未命中（阈值 {self.dot_match_sim}），"
                          f"可能原因: ①模板图片与当前小地图风格不符 "
                          f"②玩家在小地图外 ③相似度阈值过高", "warn")
            # 丢失时间过长：清掉 last_pos
            if self._last_pos is not None and now - self._dot_seen_t > 2.0:
                self._last_pos = None
            return None

        cx, cy, score = result
        new_pos = (float(cx), float(cy))
        self._last_pos = new_pos
        self._dot_seen_t = now

        # 位置变化或距上次锁定日志超过 30 秒才打印，避免每帧刷屏
        should_log = (self._last_lock_log is None
                      or abs(self._last_lock_log[0] - cx) >= 2
                      or abs(self._last_lock_log[1] - cy) >= 2
                      or now - self._last_lock_log[2] > 30.0)
        if should_log:
            self._last_lock_log = (float(cx), float(cy), now)
            # self._log(f"🎯 模板匹配锁定小地图黄点: ({cx},{cy}) 分数={score:.3f}", "info")

        # debug_cands 用于 NAV 面板上画候选小圈（这里只有一个，画玩家点本身）
        self.debug_cands = [(float(cx), float(cy))]
        return new_pos

    def _dot_candidates(self, roi):
        """v18: 参考项目重写 — BGR精确匹配优先，HSV兜底

        返回: (cands, mask, level)
          level=0: 精确BGR匹配（玩家点本身，最高优先级）
          level=1: BGR±2容差匹配
          level=2: BGR±4容差 → 连通组件去噪
          level=3: HSV兜底

        日志策略（v18降噪）:
          本函数不再每帧打印日志，状态变化由调用方 _player_pos_raw 控制。
          避免每帧刷屏浪费控制台和用户阅读成本。
        """
        h_val, w_val = roi.shape[:2]
        dot_bgr = tuple(int(x) for x in self.dot_color.tolist())  # 目标BGR颜色

        # ==================== Level 0: 精确BGR匹配（最高优先级）====================
        # 注意: cv2.findNonZero 在不同 OpenCV 版本返回 shape 可能是 (N,1,2) 或 (N,2)
        # 必须用 reshape(-1, 2) 规整形状，否则 mean(axis=0) 后再索引会
        # 抛出 "invalid index to scalar variable" 错误
        mask0 = cv2.inRange(roi, np.array(dot_bgr, np.uint8), np.array(dot_bgr, np.uint8))
        coords0 = cv2.findNonZero(mask0)
        if coords0 is not None and len(coords0) >= 4:
            pts0 = coords0.reshape(-1, 2)  # 强制 (N, 2)
            cx, cy = float(pts0[:, 0].mean()), float(pts0[:, 1].mean())
            return [(cx, cy, int(len(pts0)))], mask0, 0

        # ==================== Level 1: BGR±2容差匹配 ====================
        tol = 2
        lo = np.array([max(0, c - tol) for c in dot_bgr], np.uint8)
        hi = np.array([min(255, c + tol) for c in dot_bgr], np.uint8)
        mask1 = cv2.inRange(roi, lo, hi)
        coords1 = cv2.findNonZero(mask1)
        if coords1 is not None and len(coords1) >= 4 and len(coords1) <= 200:
            # 候选不多时（≤200像素），取质心即可
            pts1 = coords1.reshape(-1, 2)  # 强制 (N, 2)，避免索引异常
            cx, cy = float(pts1[:, 0].mean()), float(pts1[:, 1].mean())
            return [(cx, cy, int(len(pts1)))], mask1, 1

        # ==================== Level 2: BGR±4容差 → 连通组件去噪 ====================
        tol2 = 4
        lo2 = np.array([max(0, c - tol2) for c in dot_bgr], np.uint8)
        hi2 = np.array([min(255, c + tol2) for c in dot_bgr], np.uint8)
        mask2 = cv2.inRange(roi, lo2, hi2)
        n2, _, stats2, cents2 = cv2.connectedComponentsWithStats(mask2, 8)
        out2 = []
        for i in range(1, n2):
            a = int(stats2[i][cv2.CC_STAT_AREA])
            bw = int(stats2[i][cv2.CC_STAT_WIDTH])
            bh = int(stats2[i][cv2.CC_STAT_HEIGHT])
            # 面积: 3-30, 宽高比: 接近1(≤3)
            if 3 <= a <= 30 and bw <= 10 and bh <= 10 and max(bw, bh) > 0:
                ratio = max(bw, bh) / min(bw, bh)
                if ratio <= 3.0:
                    out2.append((float(cents2[i][0]), float(cents2[i][1]), a))

        # 如果精确/容差BGR都没找到，走HSV兜底，但只取最亮的Top 5
        if not out2:
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            mask3 = cv2.inRange(hsv, (self._h_lo, self.DOT_S_MIN, self.DOT_V_MIN),
                                (self._h_hi, 255, 255))
            n3, _, stats3, cents3 = cv2.connectedComponentsWithStats(mask3, 8)
            hsv_cands = []
            for i in range(1, n3):
                a = int(stats3[i][cv2.CC_STAT_AREA])
                bw = int(stats3[i][cv2.CC_STAT_WIDTH])
                bh = int(stats3[i][cv2.CC_STAT_HEIGHT])
                if 3 <= a <= 30 and bw <= 10 and bh <= 10:
                    cx, cy = int(cents3[i][0]), int(cents3[i][1])
                    v_val = int(hsv[min(cy, h_val - 1), min(cx, w_val - 1), 2])
                    hsv_cands.append((float(cents3[i][0]), float(cents3[i][1]), a, v_val))
            # 按亮度排序，取Top 5
            hsv_cands.sort(key=lambda c: c[3], reverse=True)
            if hsv_cands:
                out2 = [(c[0], c[1], c[2]) for c in hsv_cands[:5]]
                return out2, mask3, 3

        # 限制最多返回15个候选
        if len(out2) > 15:
            out2 = out2[:15]

        return out2, mask2, 2

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
        if not p:
            return 0
        if p["phase"] == "walkA":
            return p["dir"]
        if p["phase"] == "walkB":
            return -p["dir"]
        return 0

    def probe_walking(self):
        """试探定位移动段进行中（引擎此间不打断，保证定位质量）"""
        return bool(self._probe is not None
                    and self._probe.get("phase") in ("walkA", "walkB"))

    def _probe_step(self, cands, now):
        """v18: 试探定位状态机（日志已大幅降噪）。

        保留 probe 逻辑作为 Level 2/3 多候选场景的兜底定位手段。
        正常情况下 Level 0/1 精确BGR匹配已经直接锁定，probe 不会被触发。
        只有颜色偏差较大、走 HSV 兜底且出现多候选时才会进入 probe。

        日志策略：所有阶段诊断只在每 8 秒打印一次，避免循环失败时刷屏。
        """
        p = self._probe
        ph = p["phase"]
        # v18: 节流计时器（每个 probe 实例独立）
        if "_diag_t" not in p:
            p["_diag_t"] = 0.0
        diag_ok = now - p["_diag_t"] > 8.0

        if ph == "base":
            p["base"].extend((c[0], c[1]) for c in cands)
            if len(p["base"]) >= 3:
                p["_base_sample"] = p["base"][:5]
            if now - p["t0"] >= 0.45:
                p["phase"], p["t0"] = "walkA", now
                if diag_ok:
                    p["_diag_t"] = now
                    sample = p.get("_base_sample") or p["base"][:5]
                    self._log(f"  探针base→walkA: {len(p['base'])}个基线候选"
                              f"(采样{len(sample)})，开始去程", "info")
            return None
        if ph == "walkA":
            p["a"].extend((c[0], c[1]) for c in cands)
            if len(p["a"]) == 30 or len(p["a"]) == 60 or now - p["t0"] >= 0.9:
                p["_a_sample"] = p["a"][:5]
            if now - p["t0"] >= 0.9:
                p["phase"], p["t0"] = "walkB", now
                if diag_ok:
                    p["_diag_t"] = now
                    self._log(f"  探针walkA→walkB: 收集{len(p['a'])}帧候选", "info")
            return None
        p["b"].extend((c[0], c[1]) for c in cands)
        if now - p["t0"] < 0.9:
            return None

        # ---- 角色运动检测：base→walkA 候选位置最大位移 ----
        base_arr = np.array(p["base"], np.float32)
        a_arr = np.array(p["a"], np.float32)
        if len(base_arr) and len(a_arr):
            max_shift = 0.0
            for ax, ay in p["a"]:
                d = float(np.sqrt(((base_arr - (ax, ay)) ** 2).sum(1)).min())
                if d > max_shift:
                    max_shift = d
            # 最大位移 < 1.0px → 角色未响应移动指令
            if max_shift < 1.0:
                if diag_ok:
                    p["_diag_t"] = now
                    self._log(f"  ⚠ 角色未移动: base→walkA 最大位移{max_shift:.2f}px（<1.0px）"
                              f"｜可能 ①按键绑定错(方向={p['dir']}) ②游戏失焦 "
                              f"③被墙卡 ④小地图未滚动(玩家在地图中央属正常)", "warn")
                p["try"] += 1
                p["phase"], p["t0"] = "base", now
                p["base"], p["a"], p["b"] = [], [], []
                if p["try"] % 2 == 1:
                    p["jump"] = True
                if p["try"] >= 6 and now - self._probe_fail_log_t > 20:
                    self._probe_fail_log_t = now
                    self._log("试探定位多次未找到移动点：若小地图随玩家滚动，"
                              "玩家点坐标不变是正常的，建议重新校准小地图颜色", "warn")
                return None

        # ---- 往返一致性评估（运动阈值2.0px，方向一致性1.5px） ----
        BASE_MOVE_MIN = 2.0
        BASE_DIR_MIN = 1.5
        RETURN_MAX = 6.0
        base = np.array(p["base"], np.float32)
        B = np.array(p["b"], np.float32)
        d = p["dir"]
        best, best_score = None, 0.0
        if len(base) and len(p["a"]) and len(B):
            for ax, ay in p["a"]:
                dist = np.sqrt(((base - (ax, ay)) ** 2).sum(1))
                j = int(dist.argmin())
                bx, by = float(base[j][0]), float(base[j][1])
                if dist[j] < BASE_MOVE_MIN or (ax - bx) * d < BASE_DIR_MIN:
                    continue
                if float(np.sqrt(((B - (bx, by)) ** 2).sum(1)).min()) > RETURN_MAX:
                    continue
                score = float(dist[j])
                if self._marks_xy is not None:
                    off = self.off_route_distance((bx, by))
                    score *= 2.0 if off < 40 else (0.3 if off > 80 else 1.0)
                if score > best_score:
                    best_score, best = score, (bx, by)

        # 失败原因诊断（节流打印）
        if best is None and diag_ok:
            p["_diag_t"] = now
            if len(base) == 0:
                reason = "base阶段无候选"
            elif len(p["a"]) == 0:
                reason = "walkA阶段无候选"
            elif len(B) == 0:
                reason = "walkB阶段无候选"
            else:
                # 统计各类失败原因
                move_counts = {"no_move": 0, "wrong_dir": 0, "no_return": 0}
                for ax, ay in p["a"]:
                    dist = np.sqrt(((base - (ax, ay)) ** 2).sum(1))
                    j = int(dist.argmin())
                    if dist[j] < BASE_MOVE_MIN:
                        move_counts["no_move"] += 1
                    elif (ax - float(base[j][0])) * d < BASE_DIR_MIN:
                        move_counts["wrong_dir"] += 1
                    else:
                        b_dist = float(np.sqrt(((B - (float(base[j][0]), float(base[j][1]))) ** 2).sum(1)).min())
                        if b_dist > RETURN_MAX:
                            move_counts["no_return"] += 1
                reason = f"运动统计{move_counts}"
            self._log(f"  探针评估失败: {reason} | 阈值 H=[{self._h_lo},{self._h_hi}] "
                      f"S=[{self.DOT_S_MIN},255] V=[{self.DOT_V_MIN},255]", "warn")

        if best is not None:
            # 锁定点距路线>100px 拒绝
            if self._marks_xy is not None:
                off = self.off_route_distance(best)
                if off > 100:
                    if diag_ok:
                        p["_diag_t"] = now
                        self._log(f"  锁定点距路线{off:.0f}px太远(>100)，拒绝，重试", "warn")
                    p["try"] += 1
                    p["phase"], p["t0"] = "base", now
                    p["base"], p["a"], p["b"] = [], [], []
                    if p["try"] % 2 == 1:
                        p["jump"] = True
                    if p["try"] >= 6 and now - self._probe_fail_log_t > 20:
                        self._probe_fail_log_t = now
                        self._log("试探定位多次未找到移动点", "warn")
                    return None
            self._probe = None
            self._last_pos = best
            self._dot_seen_t = now
            self._shift_t = -99.0
            self._log(f"🧭 玩家点已锁定(往返试探) ({best[0]:.0f},{best[1]:.0f})", "ok")
            if self._marks_xy is not None and (self._shift is None or
                                               abs(self._shift[0]) + abs(self._shift[1]) < 6):
                off = self.off_route_distance(best)
                if off > 80:
                    self._log(f"⚠ 新锁定点距路线 {off:.0f}px 偏远", "warn")
            return best
        p["try"] += 1  # 失败 → 换方向重试；隔次起跳脱困（防卡死死循环）
        p["phase"], p["t0"] = "base", now
        p["base"], p["a"], p["b"] = [], [], []
        if p["try"] % 2 == 1:
            p["jump"] = True
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
        """多帧采样玩家黄点颜色：按目标HSV: H:25-35, S:40-255, V:120-255 校准。
        采样阈值略宽于检测阈值，确保能捕获到真实玩家点颜色。"""
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
            # 校准阈值: H:22-38, S:30-255, V:140-255 (略宽于检测阈值，确保能采到)
            m = cv2.inRange(hsv, (22, 30, 140), (38, 255, 255))
            if not m.any():
                continue
            n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
            for i in range(1, n):
                a = int(stats[i][cv2.CC_STAT_AREA])
                if 2 <= a <= 60 and stats[i][cv2.CC_STAT_WIDTH] <= 8 \
                        and stats[i][cv2.CC_STAT_HEIGHT] <= 8:
                    sel = roi[labels == i]
                    if sel.size:
                        pixels.append(sel)
        if not pixels:
            self._log("校准未找到任何黄色像素，将保留默认颜色", "warn")
            return None
        med = np.median(np.vstack(pixels), axis=0).astype(np.float64)
        if np.isnan(med).any():
            self._log("玩家点颜色采样异常(NaN)，保留原颜色", "warn")
            return None
        med = np.clip(med, 0, 255).astype(int)
        self.dot_color = np.array(med, dtype=np.int16)
        self._update_hue()
        # v13: 输出校准结果（正确显示HSV）
        hsv_val = cv2.cvtColor(med.reshape(1, 1, 3), cv2.COLOR_BGR2HSV)[0, 0]
        self._log(f"🎨 校准采样: BGR=({med[0]},{med[1]},{med[2]}) "
                  f"HSV=({hsv_val[0]},{hsv_val[1]},{hsv_val[2]}) "
                  f"→ 检测阈值: H=[{self._h_lo},{self._h_hi}] S=[{self.DOT_S_MIN},255] V=[{self.DOT_V_MIN},255]",
                  "info")
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
        now = float(now if now is not None else time.time())
        if not self.ready:
            return RouteCmd(status="未加载颜色路线")
        if pos is None:
            # 抓绳/攀爬中定位短暂丢失：保持按↑别松（松开=掉绳）
            if self._phase in ("grab", "climb") and now - self._phase_t < 2.5:
                return RouteCmd(climb="up",
                                status="🪢 攀爬中(定位暂失)…")
            if self._phase == "search" and now - self._phase_t < 2.5:
                return RouteCmd(dir=self._search_dir,
                                status="🪢 搜索绳子(定位暂失)…")
            self._reset_climb()
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
            ct = self._consumed.get((idx, mx // 5, my // 5), 0)
            if act in CONSUMABLE and ct > now:
                # 长冷却标记（>CONSUME_T*2）不参与回退，防止绳子重复抓取
                if ct - now > CONSUME_T * 2:
                    continue
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
        now = float(now)
        if act in CONSUMABLE:
            self._consumed[(idx, mx // 5, my // 5)] = now + 30.0
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
            return self._climb(v, x, y, mx, my, now, nm, idx)
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

    def _rope_extent(self, idx, mx, my):
        """获取当前绳子标记段的顶端 y、底端 y 和中心 x 坐标（小地图/路线图坐标系）。

        原理：路线图 label map 中，同一根绳子的所有像素共享同一个标记索引 idx。
        通过连通域分析找到包含标记点 (mx, my) 的那根绳子段，返回其上下边界和中心 x。

        参数:
            idx: 绳子在 label map 中的标记索引（RAW_CODES 下标）
            mx, my: 当前绳子标记点的坐标（用于定位是哪根绳子）

        返回:
            (top_y, bottom_y, center_x) 或 (None, None, None)（找不到时）
        """
        lab = self._label
        if lab is None or idx is None:
            return None, None, None
        h, w = lab.shape
        if not (0 <= mx < w and 0 <= my < h):
            return None, None, None
        # 该绳子标记的二值图
        mask = (lab == idx).astype(np.uint8)
        if not mask.any():
            return None, None, None
        # 连通域分析（8 连通），分离多根同色绳子
        num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if num <= 1:  # 只有背景，无连通域
            return None, None, None
        comp_id = int(labels[my, mx])
        if comp_id == 0:  # 标记点落在背景上（异常）
            return None, None, None
        # stats[comp_id] = (x, y, w, h, area)
        # y 即顶端，y+h-1 即底端，x + w//2 即中心 x
        x0, y0, rw, rh, _ = stats[comp_id]
        return int(y0), int(y0 + rh - 1), int(x0 + rw // 2)

    def _nearest_rope_x(self, idx, player_x):
        """找到离玩家 x 最近的同色绳子中心 x（处理多根绳子场景）。

        参数:
            idx: 绳子标记索引
            player_x: 玩家当前 x 坐标

        返回:
            最近绳子的中心 x，找不到时返回 None
        """
        lab = self._label
        if lab is None or idx is None:
            return None
        mask = (lab == idx).astype(np.uint8)
        if not mask.any():
            return None
        num, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if num <= 1:
            return None
        best_x, best_dist = None, float("inf")
        for i in range(1, num):
            x0, _, rw, _, _ = stats[i]
            cx = int(x0 + rw // 2)
            d = abs(cx - player_x)
            if d < best_dist:
                best_dist, best_x = d, cx
        return best_x

    # 绳子标记 idx（RAW_CODES 下标）
    _ROPE_IDXS = (13, 14)  # 13=上爬绳, 14=下爬绳

    def nearest_rope_for_rest(self, player_x, player_y):
        """找到离玩家最近的绳子，返回 (top_y, bottom_y, center_x)。

        用于本轮挂机结束后，寻路到最近绳子作为安全休息点。
        遍历所有绳子标记（上爬绳/下爬绳），计算每个连通域到玩家的距离。

        参数:
            player_x, player_y: 玩家当前坐标（小地图坐标系）

        返回:
            (top_y, bottom_y, center_x) 或 (None, None, None)
        """
        lab = self._label
        if lab is None:
            return None, None, None
        h, w = lab.shape
        best = None
        best_dist = float("inf")
        for idx in self._ROPE_IDXS:
            mask = (lab == idx).astype(np.uint8)
            if not mask.any():
                continue
            num, labels, stats, _ = cv2.connectedComponentsWithStats(
                mask, connectivity=8)
            if num <= 1:
                continue
            for i in range(1, num):
                x0, y0, rw, rh, _ = stats[i]
                cx = int(x0 + rw // 2)
                top_y = int(y0)
                bottom_y = int(y0 + rh - 1)
                # 用绳子中心点到玩家的距离作为判断依据
                cy = (top_y + bottom_y) // 2
                d = ((cx - player_x) ** 2 + (cy - player_y) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best = (top_y, bottom_y, cx)
        return best

    def move_to_rope_cmd(self, player_x, player_y, rope, now):
        """生成走向绳子的控制指令。

        参数:
            player_x, player_y: 玩家坐标
            rope: (top_y, bottom_y, center_x)
            now: 当前时间

        返回:
            RouteCmd（对齐 x + 上下移动到绳子 y 区间）
            到达绳子 y 区间时返回 status="arrived"
        """
        if rope is None or rope[0] is None:
            return RouteCmd(status="no_rope")
        top_y, bottom_y, cx = rope
        # x 对齐
        dx = player_x - cx
        if abs(dx) > self.grab_tol:
            move_dir = 1 if dx < 0 else -1
            return RouteCmd(dir=move_dir,
                            status=f"走向绳子x…(x={player_x:.0f}→{cx})")
        # x 已对齐，检查 y 是否在绳子两端之间
        if top_y <= player_y <= bottom_y:
            return RouteCmd(status="arrived")
        # y 不在区间，上下移动
        if player_y < top_y:
            return RouteCmd(climb="up",
                            status=f"上移到绳子…(y={player_y:.0f}→{top_y}-{bottom_y})")
        else:
            return RouteCmd(climb="down",
                            status=f"下移到绳子…(y={player_y:.0f}→{top_y}-{bottom_y})")

    def _climb_down(self, x, y, mx, my, now, nm, idx=None):
        """下爬：先对齐绳子 x，然后直接往下走，到达绳子底端即结束。

        核心逻辑（基于坐标）：
          1. 获取绳子底端 y 和中心 x
          2. 若玩家 x 未对齐绳子 x（差距 > grab_tol），先横向移动对齐
             —— 确保人物在绳子正上方，才能准确下滑
          3. 对齐后按住↓往下爬
          4. 玩家 y >= 绳子底端 y - 容差(2px) → 下爬结束，继续寻路

        容差小（2px）：地图本身小，容差过大会导致判断不准。
        掉绳不用管：下爬时即使掉了也无所谓，直接继续寻路即可。
        """
        now = float(now)

        # 获取绳子边界和中心 x（只算一次并缓存）
        if self._rope_bottom is None or self._rope_cx is None:
            self._rope_top, self._rope_bottom, self._rope_cx = \
                self._rope_extent(idx, mx, my)

        target_y = self._rope_bottom
        rope_x = self._rope_cx if self._rope_cx is not None else mx

        # —— 步骤1: x 对齐 —— 玩家 x 与绳子中心 x 差距超过 grab_tol 时先横向移动
        if rope_x is not None and abs(x - rope_x) > self.grab_tol:
            move_dir = 1 if rope_x > x else -1
            return RouteCmd(
                dir=move_dir, climb="down",
                status=f"🪢 对齐绳子x…(x={x}, 绳x={rope_x})")

        # —— 步骤2 & 3: 到达底端则结束，否则继续往下爬 ——
        if target_y is not None:
            # 方向感知脱离：下爬 y 增大，y >= 底端y - 2 即到达
            if y >= target_y - 2:
                self._reset_climb()
                if idx is not None:
                    self._consumed[(idx, mx // 5, my // 5)] = now + 30.0
                return RouteCmd(status="🪢 下爬结束，继续寻路")
            return RouteCmd(
                climb="down",
                status=f"🪢 下滑中[{nm}](y={y:.0f}, 底端y={target_y})")

        # 兜底：绳子边界无法获取时，直接往下走，超时后结束
        self._t0 = float(self._t0)
        if self._t0 <= 0:
            self._t0 = now
        if now - self._t0 > 8.0:
            self._reset_climb()
            if idx is not None:
                self._consumed[(idx, mx // 5, my // 5)] = now + 30.0
            return RouteCmd(status="🪢 下滑超时，继续寻路")
        return RouteCmd(climb="down", status=f"🪢 下滑中[{nm}]")

    def _climb(self, way, x, y, mx, my, now, nm, idx=None):
        # 下爬与上爬是两套不同逻辑：
        #   - down → _climb_down：直接往下走，无需抓绳/脱离
        #   - up  → 对准x → 抓绳+攀爬(持续按↑) → 顶端后继续按↑爬出
        if way == "down":
            return self._climb_down(x, y, mx, my, now, nm, idx)

        now = float(now)
        self._t0 = float(self._t0)
        self._move_t = float(self._move_t)
        self._jump_at = float(self._jump_at)

        # ===================== grab_climb 抓绳+攀爬阶段 =====================
        # 核心思路（参考 MapleStoryAutoLevelUp）：
        #   在绳子区域内持续按↑，不需要复杂的抓绳/脱离状态切换。
        #   - 持续按↑爬绳
        #   - y 2秒未变化 → 没抓到绳子 → 跳+↑重试
        #   - x 偏离 → 横向修正
        #   - y <= 顶端y → 到达顶端，继续按↑ 1.5秒爬出绳子 → 结束
        if self._phase in ("grab", "climb"):
            self._phase_t, self._phase_way = now, way

            # 1. 获取绳子边界（只算一次并缓存）
            if self._rope_top is None or self._rope_cx is None:
                self._rope_top, self._rope_bottom, self._rope_cx = \
                    self._rope_extent(idx, mx, my)
            target_y = self._rope_top  # 绳子顶端 y
            rope_x = self._rope_cx if self._rope_cx is not None else mx

            # 2. x 对齐修正（攀爬中持续保持与绳子 x 对齐）
            climb_dir = None
            if rope_x is not None:
                dx = x - rope_x
                if abs(dx) > self.grab_tol:
                    climb_dir = 1 if dx < 0 else -1

            # 3. 顶端检测：到达顶端后继续按↑ 1.5秒爬出绳子
            top_tol = 3
            if target_y is not None and y <= target_y + top_tol:
                if not getattr(self, "_top_reached", False):
                    self._top_reached = True
                    self._top_reach_t = now
                elif now - self._top_reach_t >= 1.5:
                    # 已按↑ 1.5秒，角色应该已经爬出绳子
                    self._reset_climb()
                    if idx is not None:
                        self._consumed[(idx, mx // 5, my // 5)] = now + 30.0
                    return RouteCmd(
                        status=f"🪢 爬绳结束(y={y:.0f}≤顶端{target_y:.0f})，继续寻路")
                # 顶端阶段：持续按↑爬出绳子
                top_flag = f"顶端✓({now - self._top_reach_t:.1f}/1.5s)"
                return RouteCmd(climb="up", dir=climb_dir,
                                status=f"🪢 爬出绳子中[{nm}]({top_flag})")

            # 4. 抓绳失败检测：y 2秒未变化 → 跳+↑重试
            if self._y0 is not None and abs(y - self._y0) < 1:
                if now - self._t0 > 2.0:
                    # 找离玩家最近的绳子中心 x，校准人物位置
                    near_x = self._nearest_rope_x(idx, x)
                    if near_x is not None and abs(x - near_x) > self.grab_tol:
                        move_dir = 1 if near_x > x else -1
                        self._t0 = now
                        self._y0 = y
                        self._jump_at = now
                        return RouteCmd(dir=move_dir, climb="up", jump=True,
                                        status=f"⚠ 抓绳未果(y2s未变)，校准x→{near_x}跳抓…")
                    # x 已对齐但仍抓不到：跳+↑重试
                    self._t0 = now
                    self._y0 = y
                    self._jump_at = now
                    return RouteCmd(dir=0, climb="up", jump=True,
                                    status="⚠ 抓绳未果(y2s未变)，跳抓重试…")
            else:
                # y 有变化（在爬），更新计时
                self._y0 = y
                self._t0 = now

            # 5. 超时保护：攀爬超过 15 秒强制结束
            climb_elapsed = now - self._climb_start_t if self._climb_start_t > 0 else 0.0
            if climb_elapsed > 15.0:
                self._reset_climb()
                if idx is not None:
                    self._consumed[(idx, mx // 5, my // 5)] = now + 30.0
                return RouteCmd(status="🪢 攀爬超时，强制结束…")

            # 6. 正常攀爬：持续按↑
            status = f"🪢 攀爬中[{nm}](y={y:.0f}, 顶端y={target_y})"
            if climb_dir is not None:
                status += f" x修正→{rope_x}"
            return RouteCmd(climb="up", dir=climb_dir, status=status)

        # ===================== align 对准绳子 x =====================
        if abs(x - mx) > self.grab_tol:
            self._phase = "align"
            return RouteCmd(dir=1 if mx > x else -1,
                            status=f"🪢 对准绳子…[{nm}]")

        # —— 到位：进入抓绳+攀爬阶段 ——
        self._phase = "grab"
        self._t0, self._y0 = now, y
        self._climb_start_t = now
        self._search_dir = 0
        # 重置绳子边界缓存
        self._rope_top = None
        self._rope_bottom = None
        self._rope_cx = None
        return RouteCmd(dir=0, climb="up", status="🪢 开始抓绳+攀爬")

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

    # ================= 攻击范围计算（参考项目v16改进）=================
    # 用于判断怪物是否在玩家攻击范围内，替代简单距离判定

    def get_attack_range_box(self, player_pos, direction, attack_range=160, skill_range=60):
        """v20: 重新定义玩家攻击范围矩形框

        以玩家中心 (cx, cy) 为锚点：
          - 水平方向：左到 cx - attack_range，右到 cx + attack_range
          - 垂直方向：从 cy 向下延伸 skill_range（玩家脚下区域，冒险岛站在地面攻击前方怪物）

        参数:
            player_pos: (cx, cy) 玩家中心坐标
            direction: 朝向（+1右/-1左），本版本不再用于限制方向，攻击框左右对称
            attack_range: 水平攻击距离（像素）
            skill_range: 垂直向下的攻击范围（像素）
        返回:
            (x0, y0, x1, y1) 攻击矩形框的左上角和右下角（y0 < y1 保证合法）
        """
        cx, cy = float(player_pos[0]), float(player_pos[1])
        ar = float(attack_range)
        sr = float(skill_range)
        x0 = cx - ar
        x1 = cx + ar
        # v20.1: 垂直方向向上延伸（玩家头顶区域，符合冒险岛怪物多在上方平台）
        y_top = cy - sr
        y_bot = cy
        # 确保 y0 < y1，矩形合法
        y0, y1 = (y_top, y_bot) if y_top < y_bot else (y_bot, y_top)
        return (x0, y0, x1, y1)

    def is_monster_in_attack_range(self, monster_pos, player_pos, direction, attack_range=160):
        """v16: 判断怪物是否在攻击范围内（参考项目 get_nearest_monster 逻辑）

        使用矩形重叠检测，比简单距离判定更准确。

        参数:
            monster_pos: (mx, my, mw, mh) 怪物位置和尺寸
            player_pos: (cx, cy) 玩家中心
            direction: +1(右) / -1(左)
            attack_range: 攻击范围
        返回:
            (in_range, overlap_area) 是否在范围内及重叠面积
        """
        box = self.get_attack_range_box(player_pos, direction, attack_range)
        x0, y0, x1, y1 = box

        mx, my, mw, mh = monster_pos
        mx1, my1, mx2, my2 = mx, my, mx + mw, my + mh

        # 计算矩形重叠
        ix1 = max(x0, mx1)
        iy1 = max(y0, my1)
        ix2 = min(x1, mx2)
        iy2 = min(y1, my2)

        iw = max(0, ix2 - ix1)
        ih = max(0, iy2 - iy1)
        overlap = iw * ih

        return (overlap > 0, overlap)