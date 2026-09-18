# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:24
# @Author  : yy
# @File    : color_route.py
# @Software: MxdAutoLvup

"""小地图 / 大 map 颜色路径导航

==================== 玩家定位 ====================
大 map 模式（推荐，异步线程）：
  ① 名字条模板在游戏画面内定位玩家 → (PX, PY)
  ② 以玩家为中心裁 300×300 小块
  ③ 小块 Canny 边缘 → 在 map.png 上做金字塔匹配（0.25x 粗搜 + 原图精搜）
  ④ 玩家 map 坐标 = 小块 map 位置 + (玩家在块内位置 × scale) + 手动偏移

小地图模式（兼容旧版）：
  · 黄点模板匹配 + 滚动补偿，小地图太模糊时不推荐使用

==================== 路线指令 ====================
路线图上的颜色 → 三元组 (lr, ud, act)
  · lr = -1 左走 / 1 右走 / 0 停 / None 无
  · ud = "up" 爬绳 / "down" 下绳 / None 无
  · act = "jump" / "teleport" / "start" / "goal" / "stop" / None

爬绳不做状态机：路线画灰色像素 → ud="up" → 键盘按住 ↑ 自然吸附
"""
import time
import random
import threading
import cv2
import numpy as np

# ============ 常量 ============
DEFAULT_DOT_COLOR = (0, 128, 255)  # 默认玩家点 BGR（黄色）
MOVE_EPS = 1.5  # 位置变化阈值（像素）
NEAREST_TOL = 100  # 颜色最近邻匹配容差
LOST_MOMENTUM = 0.8  # 定位丢失后惯性保持时长（秒）
CONSUME_T = 2.5  # 消耗品冷却基础时长
BEHIND_FREE = 6  # 反向选择惩罚的免罚区（像素）

# 颜色 → 三元组表
# (BGR, lr, ud, act, 中文名)
COLOR_CODE = [
    ((255, 0, 0), -1, None, None, "左走"),
    ((0, 0, 255), 1, None, None, "右走"),
    ((255, 127, 0), -1, None, "jump", "左跳"),
    ((0, 255, 255), 1, None, "jump", "右跳"),
    ((127, 255, 0), None, "down", "jump", "下跳"),
    ((255, 0, 255), None, None, "jump", "原地跳"),
    ((0, 255, 127), 0, 0, "start", "起点"),
    ((255, 255, 0), None, None, "goal", "终点"),
    ((255, 0, 127), None, "up", "teleport", "上瞬移"),
    ((127, 0, 255), None, "down", "teleport", "下瞬移"),
    ((0, 127, 0), -1, None, "teleport", "左瞬移"),
    ((139, 69, 19), 1, None, "teleport", "右瞬移"),
]

# 上下方向专用颜色表（爬绳/下绳）
COLOR_CODE_UP_DOWN = [
    ((127, 127, 127), None, "up", None, "上爬绳"),
    ((255, 255, 127), None, "down", None, "下爬绳"),
]


# 兼容旧接口：route_painter.py 等仍按 RAW_CODES 遍历调色板
def _lr_to_str(lr):
    """-1/1/0 → 'left'/'right'/'stop'，用于兼容旧 RAW_CODES 的字符串方向字段"""
    return {-1: "left", 1: "right", 0: "stop"}.get(lr, None)


def _binarize_bg(img, thresh=50):
    """把亮度 < thresh 的像素涂黑，让 mini 和 map 的背景都变纯黑"""
    if img is None or img.size == 0:
        return img
    out = img.copy()
    gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
    out[gray < thresh] = 0
    return out


RAW_CODES = [
    (bgr, _lr_to_str(lr), ud, act, nm)
    for (bgr, lr, ud, act, nm) in (COLOR_CODE + COLOR_CODE_UP_DOWN)
]

CONSUMABLE = {"jump", "teleport"}


class RouteCmd:
    """路线指令三元组 + 状态显示"""
    __slots__ = ("lr", "ud", "act", "status", "label")

    def __init__(self, lr=None, ud=None, act=None, status="", label=""):
        self.lr = lr  # -1 左 / 1 右 / 0 停 / None 无指令
        self.ud = ud  # "up" / "down" / None
        self.act = act  # "jump" / "teleport" / "start" / "goal" / "stop" / None
        self.status = status
        self.label = label


class ColorRouteNavigator:
    """颜色路线导航器（GUI / 引擎共用）

    大 map 模式下自动启动定位线程：主循环把帧塞进 _loc_frame_slot，
    定位线程异步匹配后写入 _loc_latest；player_pos 立即返回最新结果。
    """

    # 爬绳标记在 _label_ud 里的索引（0=上爬绳, 1=下爬绳）
    _ROPE_IDXS = (0, 1)

    def __init__(self):
        """初始化颜色路线导航器

        字段分 5 组：
          1. 配置（configure 热重载可改）
          2. 路线数据（load / _load_one 填充）
          3. 大 map 定位状态（player_pos 每帧更新）
          4. 小地图模式兼容字段（旧接口保留）
          5. 巡逻状态（step 每帧更新）
        """
        # ============ 1. 配置（configure 热重载）============
        self.minimap = (0, 0, 0, 0)  # 小地图 ROI 区域 (x, y, w, h)
        self.search_range = 10  # 巡逻时搜索最近路标的半径（像素）
        self.grab_tol = 4  # 爬绳水平对准容差（像素）
        self.ui_y_start = 690  # 游戏画面底部 y（名字条搜索下界）
        self._player_tpl_np = None  # 名字条模板（numpy BGR）
        self.map_offset_x = 0.0  # map 坐标 X 偏移（GUI 微调用）
        self.map_offset_y = 0.0  # map 坐标 Y 偏移
        self.on_log = None  # 日志回调 fn(msg, level)
        # 兼容旧 configure 接口（当前逻辑未使用，保留字段防 AttributeError）
        self.dot_color = np.array(DEFAULT_DOT_COLOR, np.int16)
        self.tolerance = 80
        self.dot_max_area = 40
        self.dot_tpl_path = None
        self._dot_tpl_cache = None
        self._dot_tpl_path_cached = None
        self.dot_match_sim = 0.9
        self.dot_match_method = 0
        self._h_lo, self._h_hi = 25, 35

        # ============ 2. 路线数据 ============
        self.routes = []  # 所有路线图（多路线循环）
        self.idx_routes = 0  # 当前路线索引
        self.route = None  # 当前路线图
        self._map_bgr = None  # 完整地图（大 map 模式）
        self._map_gray = None  # map 灰度缓存
        self._label = None  # 颜色 → COLOR_CODE 索引的标签图
        self._label_ud = None  # 颜色 → COLOR_CODE_UP_DOWN 的标签图
        self._marks_xy = None  # 路标点坐标（下采样），供 off_route_distance
        self.laps = 0  # 完成圈数
        self._has_start = False  # 路线是否包含起点标记
        self._reverse_mode = False  # 反向返回起点模式
        self._goal_skip_until = -99.0
        self._stop_idx = -1  # 停止标记索引（保留，新路线不用）
        self._has_stop = False

        # ============ 3. 大 map 定位状态 ============
        self._map_bgr = None  # 底图（已缩放到游戏显示尺寸）
        self._mm_gray = None  # 底图灰度缓存
        self._last_global_pos = None  # 上一帧 map 坐标（失败时兜底）
        self._last_dot_pos = None  # 上一帧黄点位置（防跳）
        self.map_offset_x = 0.0  # 手动偏移 X
        self.map_offset_y = 0.0  # 手动偏移 Y
        self.ui_y_start = 690  # 游戏画面底部 y
        self._player_tpl_np = None  # 名字条模板（只画攻击框用）

        # ============ 4. 小地图模式（旧接口保留）============
        self._base = None  # 滚动补偿底图
        self._shift = None  # 当前滚动位移
        self._shift_t = -99.0
        self._bad_since = 0.0
        self._shift_log_t = -99.0
        self._last_pos = None
        self._dot_seen_t = -99.0

        # ============ 5. 巡逻状态 ============
        self._move_t = time.time()  # 上次移动时间（停滞检测）
        self._step_pos = None  # 上一帧位置
        self._walk_t0 = None  # 看门狗起点时间
        self._walk_p0 = None  # 看门狗起点位置
        self._cur_dir = 0  # 当前朝向：-1 左 / 1 右
        self._last_mark = None
        self._last_seen_t = -99.0
        self._consumed = {}  # 消耗品/爬绳冷却池 {(idx, x//5, y//5): expire}
        self._ret = None  # 回归状态机
        self._jump_t = 0.0  # 上次跳跃时间
        self._tp_t = 0.0  # 上次瞬移时间
        self._goal_t = 0.0  # 上次到终点时间
        self._dir_hold_until = 0.0  # 方向锁定到期时间（防止左右抖动）
        self._dir_hold_side = 0  # 锁定的方向：-1 左 / 1 右 / 0 未锁

        self.is_on_ladder = False
        self._ladder_prev = None

        # 根据 dot_color 预计算 HSV 色相范围（小地图模式用）
        self._update_hue()

    # ==================== 配置 ====================
    @staticmethod
    def _parse_color(c):
        """容错解析颜色 → (B,G,R) 三元组；无法解析返回 None"""
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

    def configure(self, minimap=None, dot_color=None, tolerance=None,
                  search_range=None, grab_tol=None, dot_max_area=None,
                  dot_s_min=None, dot_v_min=None, dot_tpl_path=None,
                  dot_match_sim=None, dot_match_method=None,
                  ui_y_start=None, player_tpl_np=None,
                  map_offset_x=None, map_offset_y=None):
        """配置热重载入口（GUI / 引擎共用）"""
        if minimap and minimap[2] > 4 and minimap[3] > 4:
            nm = tuple(int(v) for v in minimap)
            if nm != self.minimap:
                self.minimap = nm
                self._last_pos = None
                self._shift = None
                self._ret = None
        if dot_color is not None:
            parsed = self._parse_color(dot_color)
            if parsed is not None:
                self.dot_color = np.array(parsed, np.int16)
                self._update_hue()
            else:
                self._log(f"玩家点颜色配置无效({dot_color!r})，已忽略", "warn")
        if tolerance is not None:
            self.tolerance = int(tolerance)
        if search_range is not None:
            self.search_range = max(3, int(search_range))
        if grab_tol is not None:
            self.grab_tol = max(2, int(grab_tol))
        if dot_max_area is not None:
            self.dot_max_area = max(8, int(dot_max_area))
        if dot_s_min is not None:
            self.DOT_S_MIN = max(10, min(255, int(dot_s_min)))
        if dot_v_min is not None:
            self.DOT_V_MIN = max(30, min(255, int(dot_v_min)))
        if dot_tpl_path is not None and dot_tpl_path != self.dot_tpl_path:
            self.dot_tpl_path = dot_tpl_path
            self._dot_tpl_cache = None
            self._dot_tpl_path_cached = None
            self._last_pos = None
        if dot_match_sim is not None:
            self.dot_match_sim = max(0.1, min(1.0, float(dot_match_sim)))
        if dot_match_method is not None:
            self.dot_match_method = max(0, min(2, int(dot_match_method)))
        # 大 map 参数
        if ui_y_start is not None:
            self.ui_y_start = max(0, int(ui_y_start))
        if player_tpl_np is not None:
            self._player_tpl_np = player_tpl_np
        if map_offset_x is not None:
            self.map_offset_x = float(map_offset_x)
        if map_offset_y is not None:
            self.map_offset_y = float(map_offset_y)

    def _update_hue(self):
        """根据玩家点 BGR 计算 HSV 色相范围（小地图模式用）"""
        px = np.array(self.dot_color, np.uint8).reshape(1, 1, 3)
        h = int(cv2.cvtColor(px, cv2.COLOR_BGR2HSV)[0, 0, 0])
        self._h_lo, self._h_hi = max(0, h - 5), min(179, h + 5)

    def _log(self, msg, level="warn"):
        if self.on_log:
            self.on_log(msg, level)

    # ==================== 状态查询 ====================
    @property
    def ready(self):
        """路线是否就绪：大 map 模式不依赖小地图"""
        if self._label is None:
            return False
        if self._map_bgr is not None:
            return True
        return self.minimap[2] > 4

    @property
    def phase(self):
        """保留兼容：永远返回 'none'"""
        return "none"

    @property
    def has_stop(self):
        return self._has_stop

    def stuck_seconds(self):
        """巡逻停滞秒数"""
        return max(0.0, time.time() - self._move_t) if self._step_pos is not None else 0.0

    def touch(self):
        """巡逻停滞看门狗：外部调用重置计时器"""
        self._move_t = time.time()

    def _reset_climb(self):
        """爬绳状态清理（新版由颜色驱动，保留空方法兼容调用）"""
        pass

    # ==================== 路线加载 ====================
    def load(self, route_bgr, map_bgr=None):
        """加载路线

        参数:
            route_bgr: 单张 numpy 或列表（多路线循环）
            map_bgr:   完整地图 numpy 或 None（大 map 模式必填）
        """
        if route_bgr is None:
            return
        if isinstance(route_bgr, list):
            self.routes = [r for r in route_bgr if r is not None]
        else:
            self.routes = [route_bgr]
        if not self.routes:
            return

        # 接收 map
        self._map_bgr = map_bgr
        self._map_gray = (cv2.cvtColor(map_bgr, cv2.COLOR_BGR2GRAY)
                          if map_bgr is not None else None)

        # 清定位缓存
        self._last_valid_pos = None
        self._last_dot_pos = None
        self._minimap_size_warned = False

        self.is_on_ladder = False
        self._ladder_prev = None

        self.idx_routes = 0
        self._load_one(self.routes[0])

    def _load_one(self, route_bgr):
        """加载单条路线：把颜色像素映射为标记索引，缓存标记点坐标

        流程:
          ① 用 COLOR_CODE / COLOR_CODE_UP_DOWN 把每个像素映射到"最近的标记索引"
          ② 下采样后缓存所有标记点坐标（供 off_route_distance 快速查询）
          ③ 重置巡逻/定位状态（切路线时从头开始）
        """
        if route_bgr is None:
            return
        self.route = route_bgr
        h, w = route_bgr.shape[:2]
        img = route_bgr.astype(np.int16)

        def _make_label(table):
            """把颜色像素映射到表中最近邻索引，-1 表示无标记

            做法：每个像素对表中所有颜色求 L1 距离，取最近的那个；
            距离超过 NEAREST_TOL 视为无标记（-1）。
            """
            colors = np.array([list(c[::-1]) for (c, *_) in table], np.int16)
            dist = np.zeros((h, w, len(colors)), np.int32)
            for ch in range(3):
                dist += np.abs(img[:, :, ch:ch + 1] - colors[None, None, :, ch])
            best, bestd = dist.argmin(2), dist.min(2)
            lab = np.full((h, w), -1, np.int8)
            hit = bestd < NEAREST_TOL
            lab[hit] = best[hit].astype(np.int8)
            return lab

        # 生成两个标签图：普通标记 + 上下标记（爬绳/下绳）
        self._label = _make_label(COLOR_CODE)
        self._label_ud = _make_label(COLOR_CODE_UP_DOWN)

        # 下采样标记点，供 off_route_distance 快速查询（最多 2000 个）
        ys, xs = np.nonzero((self._label >= 0) | (self._label_ud >= 0))
        if len(xs):
            step = max(1, len(xs) // 2000)
            self._marks_xy = np.stack([xs[::step], ys[::step]], 1).astype(np.float32)
        else:
            self._marks_xy = None

        # 起点标记是否存在（用于"到终点后反向返回"逻辑）
        self._has_start = bool((self._label == 6).any())  # 索引 6 = 起点 (0,255,127)

        # ---- 重置巡逻状态 ----
        self.laps = 0
        self._consumed.clear()
        self._cur_dir = 0
        self._last_mark = None
        self._last_pos = None
        self._ret = None
        self._shift = None
        self._dir_hold_until = 0.0
        self._dir_hold_side = 0

        self._mm_gray = None
        self._last_global_pos = None
        self._last_dot_pos = None

        # ---- 重置定位状态 ----
        self._last_global_pos = None
        self._last_dot_pos = None
        self._last_success_t = -99.0
        self._minimap_size_warned = False

        self.is_on_ladder = False
        self._ladder_prev = None

    # ==================== 玩家定位（大 map 模式）====================
    def _find_player_in_frame(self, frame_bgr):
        """名字条模板在游戏画面内定位玩家（只跑灰度，与 t_locate.py 一致）

        为什么要去掉白遮罩/直方图均衡：
          · 它们能提升"字条明暗对比差"的场景，但游戏画面里同时有怪物名、
            聊天文字、UI 文字，容易被误匹配到这些固定位置
          · 结果：名字条位置锁死 / 偏差 → 裁剪块走样 → 匹配分数掉
        只跑灰度，与 t_locate.py 完全一致。
        """
        tpl = self._player_tpl_np
        if tpl is None:
            return None
        th, tw = tpl.shape[:2]
        H, W = frame_bgr.shape[:2]
        if th > H or tw > W:
            return None

        # 搜索下界 = ui_y_start 与 0.66*H 中较小者
        SEARCH_BOTTOM = min(H, self.ui_y_start, int(H * 0.66))
        if SEARCH_BOTTOM <= th:
            return None
        search = frame_bgr[0:SEARCH_BOTTOM, :].copy()
        search[0:150, 0:200] = 0  # 左上角小地图涂黑

        f_gray = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
        t_gray = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)

        try:
            res = cv2.matchTemplate(f_gray, t_gray, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
        except cv2.error:
            return None

        if mv < 0.35:
            return None
        cx = ml[0] + tw // 2
        cy = ml[1] + th // 2
        # 名字条在角色脚下 → 玩家中心上移 30px
        return (float(cx), float(cy - 30))

    def _find_dot_in_minimap(self, minimap):
        """在小地图内找玩家黄点（HSV + 连通域 + 上一帧优先）

        改进点：
          · 打印所有候选（诊断）
          · 有多个候选时，选离上一帧黄点最近的那个（防跳）
        """
        if minimap is None or minimap.size == 0:
            return None

        hsv = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)
        # 黄色范围
        mask = cv2.inRange(hsv, (22, 180, 200), (38, 255, 255))
        mask = cv2.dilate(mask, np.ones((2, 2), np.uint8), iterations=1)

        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        if n <= 1:
            return None

        # 收集所有候选（面积 3~200 的黄色团）
        cands = []
        for i in range(1, n):
            a = int(stats[i, cv2.CC_STAT_AREA])
            if 3 <= a <= 200:
                cands.append((float(cents[i][0]), float(cents[i][1]), a))
        if not cands:
            return None

        # ★ 有上一帧位置 → 选离它最近的（防跳）
        prev = getattr(self, "_last_dot_pos", None)
        if prev is not None:
            cands.sort(key=lambda c: abs(c[0] - prev[0]) + abs(c[1] - prev[1]))
        else:
            # 首帧：选面积最大的（黄点通常比噪点大）
            cands.sort(key=lambda c: -c[2])

        cx, cy, _ = cands[0]
        self._last_dot_pos = (cx, cy)
        return (int(round(cx)), int(round(cy)))

    # ==================== 玩家定位（小地图模式，兼容）====================
    def _load_dot_tpl(self):
        """加载小地图黄点模板图片（带缓存）"""
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
            self._log(f"⚠ 小地图黄点模板读取失败: {path}", "warn")
            self._dot_tpl_cache = None
            self._dot_tpl_path_cached = path
            return None
        self._dot_tpl_cache = img
        self._dot_tpl_path_cached = path
        return img

    def p_capture(self, frame_bgr, x1, y1, x2, y2):
        """裁剪小地图区域"""
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
        """模板匹配定位小地图玩家点"""
        if template_img is None:
            return None
        find_img = self.p_capture(frame_bgr, x1, y1, x2, y2)
        if find_img is None:
            return None
        th, tw = template_img.shape[:2]
        fh, fw = find_img.shape[:2]
        if th > fh or tw > fw:
            return None
        if method == 0:
            res = cv2.matchTemplate(find_img, template_img, cv2.TM_CCORR_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= similarity:
                return (int(max_loc[0] + tw // 2),
                        int(max_loc[1] + th // 2), float(max_val))
            return None
        elif method == 1:
            res = cv2.matchTemplate(find_img, template_img, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= similarity:
                return (int(max_loc[0] + tw // 2),
                        int(max_loc[1] + th // 2), float(max_val))
            return None
        elif method == 2:
            res = cv2.matchTemplate(find_img, template_img, cv2.TM_SQDIFF_NORMED)
            min_val, _, min_loc, _ = cv2.minMaxLoc(res)
            score = 1.0 - float(min_val)
            if score >= similarity:
                return (int(min_loc[0] + tw // 2),
                        int(min_loc[1] + th // 2), score)
            return None
        return None

    def _match_minimap_in_map(self, mini_bgr):
        """小地图在底图上匹配（mask 只比地形）"""
        if self._map_bgr is None or mini_bgr is None or mini_bgr.size == 0:
            return None

        # 底图灰度缓存
        if self._mm_gray is None:
            self._mm_gray = cv2.cvtColor(self._map_bgr, cv2.COLOR_BGR2GRAY)
        m_gray = self._mm_gray

        mini_gray = cv2.cvtColor(mini_bgr, cv2.COLOR_BGR2GRAY)
        mh, mw = m_gray.shape
        mh_mini, mw_mini = mini_gray.shape
        if mh_mini > mh or mw_mini > mw:
            return None

        # mask：只让彩色地形参与
        hsv = cv2.cvtColor(mini_bgr, cv2.COLOR_BGR2HSV)
        S = hsv[:, :, 1]
        V = hsv[:, :, 2]
        mask = ((S > 40) & (V > 40)).astype(np.uint8) * 255
        if int((mask > 0).sum()) < 50:
            return None

        # 局部搜索优先
        last = getattr(self, "_last_mm_loc", None)
        if last is not None:
            lx, ly = last
            R = 30
            sx0 = max(0, lx - R)
            sy0 = max(0, ly - R)
            sx1 = min(mw, lx + R + mw_mini)
            sy1 = min(mh, ly + R + mh_mini)
            roi = m_gray[sy0:sy1, sx0:sx1]
            if roi.shape[0] >= mh_mini and roi.shape[1] >= mw_mini:
                try:
                    res = cv2.matchTemplate(roi, mini_gray,
                                            cv2.TM_SQDIFF_NORMED, mask=mask)
                    res = np.nan_to_num(res, nan=1.0, posinf=1.0, neginf=1.0)
                    min_val, _, min_loc, _ = cv2.minMaxLoc(res)
                    if min_val < 0.3:
                        loc = (sx0 + min_loc[0], sy0 + min_loc[1])
                        self._last_mm_loc = loc
                        return loc[0], loc[1]
                except cv2.error:
                    pass

        # 全图搜索
        try:
            res = cv2.matchTemplate(m_gray, mini_gray,
                                    cv2.TM_SQDIFF_NORMED, mask=mask)
            res = np.nan_to_num(res, nan=1.0, posinf=1.0, neginf=1.0)
            min_val, _, min_loc, _ = cv2.minMaxLoc(res)
        except cv2.error:
            return None

        if min_val > 0.5:
            return None

        self._last_mm_loc = min_loc
        return int(min_loc[0]), int(min_loc[1])

    def _clean_minimap_dots(self, mini_bgr, dot_xy=None):
        """涂掉小地图上的黄点/红点（用中值色填充）

        参数:
            mini_bgr: 小地图 BGR
            dot_xy:   已知的黄点坐标 (x, y)，直接用圆形涂掉更可靠
        """
        if mini_bgr is None or mini_bgr.size == 0:
            return mini_bgr
        out = mini_bgr.copy()
        mask = np.zeros(mini_bgr.shape[:2], np.uint8)

        # ① 黄点：用已知坐标画圆涂
        if dot_xy is not None:
            cv2.circle(mask, (int(dot_xy[0]), int(dot_xy[1])),
                       8, 255, -1)

        # ② 红点：HSV 检测
        hsv = cv2.cvtColor(mini_bgr, cv2.COLOR_BGR2HSV)
        mask |= cv2.inRange(hsv, (0, 130, 100), (10, 255, 255))
        mask |= cv2.inRange(hsv, (170, 130, 100), (180, 255, 255))

        if not mask.any():
            return mini_bgr
        # 用中值滤波后的图填充
        median = cv2.medianBlur(mini_bgr, 9)
        out[mask > 0] = median[mask > 0]
        return out

    def _player_pos_by_map(self, frame_bgr):
        """玩家定位：小地图在底图上匹配 + 黄点相对位置

        前提：底图已对齐游戏小地图显示尺寸（加载时自动缩放）
        """
        if frame_bgr is None or self._map_bgr is None:
            return None

        # ① 裁小地图 ROI
        x, y, w, h = self.minimap
        if w < 5 or h < 5:
            return None
        H, W = frame_bgr.shape[:2]
        x2, y2 = min(x + w, W), min(y + h, H)
        mini = frame_bgr[y:y2, x:x2].copy()  # ★ copy 一份，避免污染原帧
        if mini.size == 0:
            return self._last_global_pos

        # ② 黄点在小地图内的位置（先用原图找）
        # 找黄点
        dot = self._find_dot_in_minimap(mini)
        if dot is None:
            return self._last_global_pos
        # ★ 涂掉黄点和红点，再送去匹配
        mini_for_match = self._clean_minimap_dots(mini, dot_xy=dot)
        loc = self._match_minimap_in_map(mini_for_match)
        dot_x, dot_y = dot

        # ★ 涂掉黄点（不参与匹配）
        cv2.circle(mini, (dot_x, dot_y), 6, (0, 0, 0), -1)

        # ★ 清理黄点+红点后再匹配
        mini_clean = self._clean_minimap_dots(mini, dot_xy=dot)

        # ③ 小地图在底图上匹配
        loc = self._match_minimap_in_map(mini_clean)
        if loc is None:
            return None
        mm_x, mm_y = loc

        # ④ 合成玩家坐标（1:1）
        gx = mm_x + dot_x + self.map_offset_x
        gy = mm_y + dot_y + self.map_offset_y
        return (float(gx), float(gy))

    def _trim_black_border(self, mini_bgr):
        """自动裁掉小地图左右黑边框

        做法：按列求平均亮度，从两端向内找到第一列亮度 > 20 的位置。
        游戏小地图左右常有深色/黑色边，和 map 内容区不一致，
        裁剪后 mini 尺寸才能和 map 对齐。
        """
        if mini_bgr is None or mini_bgr.size == 0:
            return mini_bgr
        gray = cv2.cvtColor(mini_bgr, cv2.COLOR_BGR2GRAY)
        col_mean = gray.mean(axis=0)
        # 亮度 > 20 的列视为有内容
        valid = np.where(col_mean > 20)[0]
        if len(valid) < 5:
            return mini_bgr
        x0 = int(valid[0])
        x1 = int(valid[-1]) + 1
        if x1 - x0 < 5:
            return mini_bgr
        return mini_bgr[:, x0:x1].copy()  # ★ 加 copy

    def player_pos(self, frame_bgr):
        """玩家定位入口

        · 大 map 模式：小地图在底图上模板匹配（同步，5ms）
        · 小地图模式：黄点模板匹配
        """
        if self._map_bgr is not None:
            pos = self._player_pos_by_map(frame_bgr)
            if pos is not None:
                self._last_global_pos = pos
            return self._last_global_pos

        # 小地图模式（旧版兼容）
        pos = self._player_pos_raw(frame_bgr)
        if pos is None:
            return None
        if self._base is None:
            return pos
        now = time.time()
        if now - self._shift_t > 0.7:
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
                    self._log("⚠ 小地图与录制底图对不上", "warn")
        if self._shift is None:
            return pos
        dx, dy = self._shift
        return (pos[0] - dx, pos[1] - dy)

    def _player_pos_raw(self, frame_bgr):
        """小地图模式：黄点模板匹配玩家位置"""
        x, y, w, h = self.minimap
        if w <= 4 or h <= 4 or frame_bgr is None:
            return None
        now = time.time()
        tpl = self._load_dot_tpl()
        if tpl is None:
            if now - getattr(self, "_tpl_no_cfg_log_t", -99.0) > 30.0:
                self._tpl_no_cfg_log_t = now
                self._log("⚠ 未配置小地图黄点模板路径（dot_tpl_path）", "warn")
            return None
        H, W = frame_bgr.shape[:2]
        x2, y2 = min(x + w, W), min(y + h, H)
        if x < 0 or y < 0 or x2 <= x or y2 <= y:
            return None
        result = self.p_findpic(frame_bgr, x, y, x2, y2, tpl,
                                similarity=self.dot_match_sim,
                                method=self.dot_match_method)
        if result is None:
            if now - getattr(self, "_match_fail_log_t", -99.0) > 30.0:
                self._match_fail_log_t = now
                self._log(f"⚠ 小地图模板匹配未命中（阈值 {self.dot_match_sim}）", "warn")
            if self._last_pos is not None and now - self._dot_seen_t > 2.0:
                self._last_pos = None
            return None
        cx, cy, score = result
        new_pos = (float(cx), float(cy))
        self._last_pos = new_pos
        self._dot_seen_t = now
        return new_pos

    def auto_sample(self, frames):
        """多帧采样小地图玩家黄点颜色"""
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
            self._log("校准未找到任何黄色像素", "warn")
            return None
        med = np.median(np.vstack(pixels), axis=0).astype(np.float64)
        if np.isnan(med).any():
            self._log("玩家点颜色采样异常(NaN)", "warn")
            return None
        med = np.clip(med, 0, 255).astype(int)
        self.dot_color = np.array(med, dtype=np.int16)
        self._update_hue()
        med_u8 = np.clip(med, 0, 255).astype(np.uint8).reshape(1, 1, 3)
        hsv_val = cv2.cvtColor(med_u8, cv2.COLOR_BGR2HSV)[0, 0]
        self._log(f"🎨 校准采样: BGR=({med[0]},{med[1]},{med[2]}) "
                  f"HSV=({hsv_val[0]},{hsv_val[1]},{hsv_val[2]})", "info")
        return med.tolist()

    # ==================== 偏离 / 停止检测 ====================
    def off_route_distance(self, pos):
        """玩家点离最近路线标记的距离（px）"""
        if self._marks_xy is None or pos is None:
            return 0.0
        p = np.array([pos[0], pos[1]], np.float32)
        return float(np.sqrt(((self._marks_xy - p) ** 2).sum(1)).min())

    def near_stop(self, pos, tol=8):
        """玩家是否靠近停止标记（保留接口，新路线不再使用停止点）"""
        if self._label is None or pos is None or self._stop_idx < 0:
            return False
        x, y = int(pos[0]), int(pos[1])
        h, w = self._label.shape
        if not (0 <= x < w and 0 <= y < h):
            return False
        x0, x1 = max(0, x - tol), min(w, x + tol + 1)
        y0, y1 = max(0, y - tol), min(h, y + tol + 1)
        return bool((self._label[y0:y1, x0:x1] == self._stop_idx).any())

    # ==================== 巡逻主逻辑 ====================
    def step(self, pos, now=None):
        now = float(now if now is not None else time.time())
        if not self.ready:
            return RouteCmd(status="未加载颜色路线")
        if pos is None:
            return RouteCmd(status="🧭 定位玩家点中…")

        if (self._step_pos is None or
                abs(pos[0] - self._step_pos[0]) >= MOVE_EPS or
                abs(pos[1] - self._step_pos[1]) >= MOVE_EPS):
            self._move_t = now
        self._step_pos = pos

        # ★ is_on_ladder 必须在 dispatch 之前更新
        if self._ladder_prev is not None:
            dx = abs(pos[0] - self._ladder_prev[0])
            dy = abs(pos[1] - self._ladder_prev[1])
            if self.is_on_ladder:
                if dx > 3:
                    self.is_on_ladder = False
            else:
                if dx < 3 and dy > 1:
                    self.is_on_ladder = True
        self._ladder_prev = pos

        x, y = int(round(pos[0])), int(round(pos[1]))
        cmd = self._dispatch(x, y, now)

        if cmd.ud in ("up", "down"):
            self._move_t = now
        if cmd.lr:
            self._cur_dir = cmd.lr
        self._watchdog(cmd, pos, now)
        return cmd

    def _watchdog(self, cmd, pos, now):
        """移动指令下跟踪点长期静止 → 疑似锁定静态点 → 丢弃重锁"""
        if not cmd.lr:
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
            self._ret = None
            self._log("⚠ 跟踪点在移动指令下长期静止", "warn")

    def _nearest_mark(self, x, y):
        """找最近的路线标记点"""
        if self._marks_xy is None or len(self._marks_xy) == 0:
            return None
        d = np.sqrt(((self._marks_xy - np.float32((x, y))) ** 2).sum(1))
        i = int(d.argmin())
        return float(self._marks_xy[i][0]), float(self._marks_xy[i][1])

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

    def _locked_dir(self, want_dir, now, hold_sec=1.5):
        """方向锁定：新方向和当前锁定方向不同时，需等锁定到期才允许换

        参数:
            want_dir: 想要的方向 (-1 / 1)
            now:      当前时间
            hold_sec: 每次锁定持续多久（秒）
        返回:
            实际输出的方向
        """
        # 未锁定 or 已到期 → 采纳新方向并锁定
        if now >= self._dir_hold_until or self._dir_hold_side == 0:
            self._dir_hold_side = want_dir
            self._dir_hold_until = now + hold_sec
            return want_dir

        # 锁定中，方向一致 → 继续
        if want_dir == self._dir_hold_side:
            return want_dir

        # 锁定中，方向不同 → 沿用锁定方向（等下次到期再换）
        return self._dir_hold_side

    def _lost(self, x, y, now, force_ret=False):
        """定位丢失 / 无可用标记时的回归逻辑（分级升级脱困）

        脱困升级链：
          ① 有目标 → 朝它走
          ② 走不通 2 次 → 换个方向
          ③ 再走不通 2 次 → 尝试跳跃（可能异层）
          ④ 仍不行 → 随机脱困（左右跳）
        """
        # 惯性：短时间内仍按上次方向走
        if not force_ret and now - self._last_seen_t < LOST_MOMENTUM \
                and self._cur_dir:
            return RouteCmd(lr=self._cur_dir, status="… 穿越标记间隙")

        if self._ret is None:
            tgt = self._nearest_mark(x, y)
            if tgt is None:
                return RouteCmd(lr=0, status="⚠ 路线图无可用标记")
            self._ret = {"tgt": tgt, "p": (x, y), "t": now, "flips": 0}

        r = self._ret

        # ★ 位置变化 2px 以上 → 说明在走，重置计时（不升级脱困）
        if abs(x - r["p"][0]) >= 2 or abs(y - r["p"][1]) >= 2:
            r["p"], r["t"] = (x, y), now
        elif now - r["t"] > 6.0:
            # ★ 6 秒没走出去 → 升级脱困手段
            r["t"], r["flips"] = now, r["flips"] + 1

            if r["flips"] <= 2:
                # ① 第 1~2 次：换方向（另一侧标记）
                alt = self._flip_target(x, y, r["tgt"])
                if alt:
                    r["tgt"] = alt
                    self._log("↩ 直线回归受阻，改道另一侧标记绕回", "info")
                else:
                    # 换向也要走锁定（避免下一帧又换回来）
                    new_d = -(self._dir_hold_side or self._cur_dir or 1)
                    self._dir_hold_side = new_d
                    self._dir_hold_until = now + 2.0  # 换向后锁 2 秒
                    self._log("↩ 回归受阻，反向沿路线绕行(环线)", "info")
            elif r["flips"] <= 4:
                # ② 第 3~4 次：跳跃脱困（可能异层）
                self._log("↗ 回归连续受阻，尝试跳跃脱困", "warn")
                self._jump_t = now
                return RouteCmd(lr=random.choice([-1, 1]), act="jump",
                                status="↗ 跳跃脱困")
            else:
                # ③ 第 5 次起：随机脱困（左右跳，最激进）
                r["flips"] = 0
                self._log("⚠ 回归持续失败，随机脱困", "warn")
                return self.random_cmd()

        # 朝目标走
        # 朝目标走（方向锁定 1.5 秒，避免左右抖动）
        dx = r["tgt"][0] - x
        if abs(dx) > 3:
            d = 1 if dx > 0 else -1
            d = self._locked_dir(d, now, hold_sec=1.5)
            return RouteCmd(lr=d, status="↩ 返回路线")

        # 目标在同 x 异层 → 跳一下
        if now - self._jump_t > 1.0:
            self._jump_t = now
            return RouteCmd(lr=0, act="jump", status="↗ 回归点在异层，跳跃尝试")

        return RouteCmd(lr=0, status="⚠ 偏离路线(异层)，尝试回层中")

    def _nearest_mark_of(self, label_img, table, x, y):
        """找 label_img 里离 (x, y) 最近的标记（曼哈顿距离）"""
        if label_img is None:
            return None
        r = max(3, int(self.search_range))
        h, w = label_img.shape
        x0, x1 = max(0, x - r), min(w, x + r + 1)
        y0, y1 = max(0, y - r), min(h, y + r + 1)
        if x0 >= x1 or y0 >= y1:
            return None
        win = label_img[y0:y1, x0:x1]
        ys, xs = np.nonzero(win >= 0)
        if len(xs) == 0:
            return None
        idx_arr = win[ys, xs]
        dx = (xs + x0 - x).astype(np.float32)
        dy = (ys + y0 - y).astype(np.float32)
        d = np.abs(dx) + np.abs(dy)
        order = np.argsort(d)[:24]
        best = None
        for i in order:
            idx = int(idx_arr[i])
            mx, my = int(xs[i] + x0), int(ys[i] + y0)
            ct = self._consumed.get((idx, mx // 5, my // 5), 0)
            if ct > time.time():
                continue
            lr, ud, act, nm = table[idx][1:]
            if best is None or d[i] < best["d"]:
                best = {"d": float(d[i]), "cmd": (lr, ud, act, nm, idx, mx, my)}
        return best

    def _dispatch(self, x, y, now):
        """参考项目 update_cmd_by_route：找最近普通色 + 最近上下色，谁近用谁

        互补只在 is_on_ladder（贴绳）时生效
        """
        cand_n = self._nearest_mark_of(self._label, COLOR_CODE, x, y)
        cand_u = self._nearest_mark_of(self._label_ud, COLOR_CODE_UP_DOWN, x, y)

        if cand_n is None and cand_u is None:
            return self._lost(x, y, now)

        if cand_n is not None and cand_u is not None:
            if cand_n["d"] <= cand_u["d"]:
                lr, ud, act, nm, idx, mx, my = cand_n["cmd"]
                # 互补：只在贴绳时，把 ud 补上
                if ud is None and self.is_on_ladder:
                    ud = cand_u["cmd"][1]
            else:
                lr, ud, act, nm, idx, mx, my = cand_u["cmd"]
                # 互补：只在贴绳时，把 lr 补上
                if lr is None and self.is_on_ladder:
                    lr = cand_n["cmd"][0]
        elif cand_n is not None:
            lr, ud, act, nm, idx, mx, my = cand_n["cmd"]
        else:
            lr, ud, act, nm, idx, mx, my = cand_u["cmd"]

        # 反向模式翻转
        if self._reverse_mode:
            lr = -lr if lr else lr
            ud = {"up": "down", "down": "up"}.get(ud, ud)

        # 消耗品冷却
        if act == "teleport":
            self._consumed[(idx, mx // 5, my // 5)] = now + 30.0

        # ---- 终点 ----
        if act == "goal":
            if self._has_start and not self._reverse_mode:
                self._reverse_mode = True
                self._goal_skip_until = now + 1.0
                self._log("↩ 到达终点，反向返回起点", "ok")
                return RouteCmd(status="↩ 反向返回起点")
            if self._has_start and self._reverse_mode:
                return RouteCmd(status="↩ 反向路线中…")
            if now - self._goal_t > 3:
                self._goal_t = now
                self.laps += 1
                if len(self.routes) > 1:
                    saved = self.laps
                    self.idx_routes = (self.idx_routes + 1) % len(self.routes)
                    self._load_one(self.routes[self.idx_routes])
                    self.laps = saved
                    self._log(f"🔄 路线切换 → {self.idx_routes + 1}/{len(self.routes)}",
                              "info")
            return RouteCmd(status=f"🏁 终点 {self.laps} 圈")

        # ---- 起点 ----
        if act == "start":
            if self._has_start and self._reverse_mode:
                self._reverse_mode = False
                self.laps += 1
                self._goal_t = now
                self._log(f"🏁 回到起点 · 第 {self.laps} 圈", "ok")
            return RouteCmd(status="🚩 起点")

        # ---- 停止 ----
        if act == "stop":
            return RouteCmd(act="stop", status="⏸ 停止点")

        # ---- 瞬移 ----
        if act == "teleport":
            if now - self._tp_t > 1.5:
                self._tp_t = now
                return RouteCmd(lr=lr, ud=ud, act="teleport", status=f"✨ {nm}")
            return RouteCmd(status=f"✨ {nm}(冷却)")

        # ---- 跳跃 ----
        if act == "jump":
            if now - self._jump_t > 0.55:
                self._jump_t = now
                return RouteCmd(lr=lr, ud=ud, act="jump", label=nm, status=f"🦘 {nm}")
            return RouteCmd(lr=lr, ud=ud, label=nm, status=f"🚶 {nm}")

        # ---- 普通移动（爬绳就是 ud=up/down 由 MovementController 按住键）----
        return RouteCmd(lr=lr, ud=ud, label=nm, status=f"🚶 {nm}")

    # ==================== 休息安全点 ====================
    def nearest_rope_for_rest(self, player_x, player_y):
        """找到离玩家最近的绳子（用于休息安全点）"""
        lab = self._label_ud
        if lab is None:
            return None, None, None
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
                cy = (top_y + bottom_y) // 2
                d = ((cx - player_x) ** 2 + (cy - player_y) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best = (top_y, bottom_y, cx)
        return best

    def move_to_rope_cmd(self, player_x, player_y, rope, now):
        """生成走向绳子的控制指令（map 坐标系，容差放大）"""
        if rope is None or rope[0] is None:
            return RouteCmd(status="no_rope")
        top_y, bottom_y, cx = rope
        # ★ map 坐标容差：mini 4px × 15 倍 ≈ 60 map px
        MAP_TOL = 60
        dx = player_x - cx
        if abs(dx) > MAP_TOL:
            move_dir = 1 if dx < 0 else -1
            return RouteCmd(lr=move_dir,
                            status=f"走向绳子x…(x={player_x:.0f}→{cx})")
        if top_y <= player_y <= bottom_y:
            return RouteCmd(status="arrived")
        if player_y < top_y:
            return RouteCmd(ud="up",
                            status=f"上移到绳子…(y={player_y:.0f}→{top_y}-{bottom_y})")
        return RouteCmd(ud="down",
                        status=f"下移到绳子…(y={player_y:.0f}→{top_y}-{bottom_y})")

    # ==================== 随机脱困 ====================
    def random_cmd(self):
        """卡住时随机脱困指令（更激进版）

        ★ 必须有动作，不能什么都不做：
          · lr : 必选 -1 或 1（不用 0，避免原地不动）
          · ud : "down" / None
          · act: 必跳（不跳很难挣脱地形）
        """
        return RouteCmd(
            lr=random.choice([-1, 1]),
            ud=random.choice(["down", None]),
            act="jump",
            status="🎲 随机脱困",
        )

    # ==================== 滚动补偿（小地图模式）====================
    def set_base(self, base_bgr):
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
        """估计小地图相对录制底图的滚动位移"""
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
        if float(tpl.std()) < 8:
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

    # ==================== 攻击范围（兼容旧接口）====================
    def get_attack_range_box(self, player_pos, direction,
                             attack_range=160, skill_range=60):
        """计算玩家攻击范围矩形框（兼容旧调用）"""
        cx, cy = float(player_pos[0]), float(player_pos[1])
        ar = float(attack_range)
        sr = float(skill_range)
        x0 = cx - ar
        x1 = cx + ar
        y_top = cy - sr
        y_bot = cy
        y0, y1 = (y_top, y_bot) if y_top < y_bot else (y_bot, y_top)
        return (x0, y0, x1, y1)

    def is_monster_in_attack_range(self, monster_pos, player_pos,
                                   direction, attack_range=160):
        """矩形重叠检测怪物是否在攻击范围内（兼容旧调用）"""
        box = self.get_attack_range_box(player_pos, direction, attack_range)
        x0, y0, x1, y1 = box
        mx, my, mw, mh = monster_pos
        mx1, my1, mx2, my2 = mx, my, mx + mw, my + mh
        ix1 = max(x0, mx1)
        iy1 = max(y0, my1)
        ix2 = min(x1, mx2)
        iy2 = min(y1, my2)
        iw = max(0, ix2 - ix1)
        ih = max(0, iy2 - iy1)
        return (iw * ih > 0, iw * ih)
