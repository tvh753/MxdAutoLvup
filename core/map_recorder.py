# -*- coding: utf-8 -*-
"""地图录制器（集成到控制台）

把 tools/record_map.py 的逻辑封装成后台线程，
由 GUI 的「录制小地图」按钮启动，走完一圈后点「保存」写回地图包。

★ 关键：复用 engine 已有的 capture 实例，不新建 —— 避免与主循环
   抢窗口（windows_capture 库同一窗口只能有一个 capture）。
"""
import threading
import time

import cv2
import numpy as np

# ORB 特征匹配器（全局只建一次，创建开销大）
_orb = cv2.ORB_create(nfeatures=500)


# ==================== 图像工具 ====================
def _match_offset(img_a, img_b):
    """ORB 特征匹配，返回 img_b 相对 img_a 的偏移 (dx, dy) 或 None"""
    if img_a is None or img_b is None:
        return None
    ga = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY)
    kp_a, des_a = _orb.detectAndCompute(ga, None)
    kp_b, des_b = _orb.detectAndCompute(gb, None)
    if des_a is None or des_b is None or len(kp_a) < 4 or len(kp_b) < 4:
        return None
    bf = cv2.BFMatcher()
    try:
        matches = bf.knnMatch(des_a, des_b, k=2)
    except cv2.error:
        return None
    good = []
    for pair in matches:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < 0.75 * n.distance:
            good.append(m)
    if len(good) < 4:
        return None
    best = min(good, key=lambda x: x.distance)
    p1 = kp_a[best.queryIdx].pt
    p2 = kp_b[best.trainIdx].pt
    return int(round(p1[0] - p2[0])), int(round(p1[1] - p2[1]))

def _find_yellow(mini, color_bgr):
    """找黄点（BGR ±40 容差，比精确匹配稳）

    精确匹配（inRange 上下界相同）对像素级 BGR 漂移太敏感，
    给 40 的容差后几乎不会漏。
    """
    c = np.array(color_bgr, dtype=np.int32)
    lower = np.clip(c - 40, 0, 255).astype(np.uint8)
    upper = np.clip(c + 40, 0, 255).astype(np.uint8)
    mask = cv2.inRange(mini, lower, upper)
    coords = cv2.findNonZero(mask)
    if coords is None or len(coords) < 4:
        return None
    pts = coords.reshape(-1, 2)
    return (int(pts[:, 0].mean()), int(pts[:, 1].mean()))

def _clean_dots(mini, player_color):
    """涂掉小地图上的玩家黄点和其它玩家红点（用中值填充，不留黑块）

    ★ v30 修复：
      · 相比"直接涂黑"，改用**中值填充** → 保留周围地形，不留黑块
      · 同时处理红点（其它玩家）→ 之前完全没处理
      · 用整块 mask 判断，不依赖某一帧是否找到黄点
        → 之前某一帧找不到黄点，那一帧的黄点就原样拼进画布

    参数:
        mini:          ROI 裁剪后的小地图 BGR
        player_color:  玩家点 BGR（config.patrol.player_dot_color）
    返回:
        清洗后的小地图（BGR）
    """
    if mini is None or mini.size == 0:
        return mini
    out = mini.copy()
    mask = np.zeros(mini.shape[:2], np.uint8)

    # ① 黄点：BGR ±40 容差（保留用户当前调校）
    c = np.array(player_color, dtype=np.int32)
    lower = np.clip(c - 40, 0, 255).astype(np.uint8)
    upper = np.clip(c + 40, 0, 255).astype(np.uint8)
    mask |= cv2.inRange(mini, lower, upper)

    # ② 红点：HSV 检测（红色跨 0°/180°，需要两段）
    hsv = cv2.cvtColor(mini, cv2.COLOR_BGR2HSV)
    mask |= cv2.inRange(hsv, (0, 130, 100), (10, 255, 255))
    mask |= cv2.inRange(hsv, (170, 130, 100), (180, 255, 255))

    if not mask.any():
        return out

    # 膨胀一点，覆盖点边缘的抗锯齿像素
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)

    # ★ 关键：用中值滤波后的图填充（保留地形），而不是涂黑
    median = cv2.medianBlur(out, 9)
    out[mask > 0] = median[mask > 0]
    return out

# ==================== 录制器 ====================
class MapRecorder(threading.Thread):
    """拼图后台线程（复用外部 capture 实例）

    生命周期：
        rec = MapRecorder(capture=engine.capture, roi=..., player_color=..., log_fn=...)
        rec.start()
        ... 用户走一圈 ...
        rec.stop()
        rec.join(timeout=2)
        rec.save(out_path, target_w)   # 保存裁剪后结果
    """

    def __init__(self, capture, roi, player_color, log_fn):
        super().__init__(daemon=True, name="MapRecorder")
        self._cap = capture                              # ★ 复用 engine 的
        self.roi = tuple(int(v) for v in roi)            # (x, y, w, h)
        self.player_color = list(player_color)
        self.log = log_fn

        self._stop_flag = False
        self._frame_count = 0
        self._img_map = None                             # 拼接画布
        self._last_mini = None                           # 上一帧 mini
        self._loc_last = None                            # 上帧在画布上的位置

    # ---------------- 对外接口 ----------------
    def stop(self):
        self._stop_flag = True

    @property
    def frame_count(self):
        return self._frame_count

    def canvas_size(self):
        if self._img_map is None:
            return (0, 0)
        return (self._img_map.shape[1], self._img_map.shape[0])

    def get_result(self):
        return self._img_map

    def save(self, out_path, target_w):
        """保存拼图：宽度对齐到 target_w（居中）+ 上下裁黑边

        参数:
            out_path:  输出 png 路径
            target_w:  目标宽度（通常 = ROI 宽度）
        """
        if self._img_map is None:
            return False
        img = self._img_map

        # 宽度裁到 target_w，内容居中
        if target_w > 5 and img.shape[1] > target_w:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            cols = np.where(gray.max(axis=0) > 20)[0]
            if len(cols) > 0:
                cx = (int(cols[0]) + int(cols[-1])) // 2
                x0 = max(0, min(cx - target_w // 2,
                                img.shape[1] - target_w))
                img = img[:, x0:x0 + target_w]

        # 上下裁黑边
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        rows = np.where(gray.max(axis=1) > 20)[0]
        if len(rows) > 0:
            img = img[int(rows[0]):int(rows[-1]) + 1, :]

        ok, buf = cv2.imencode(".png", img)
        if ok:
            buf.tofile(out_path)
            return True
        return False

    # ---------------- 线程主循环 ----------------
    def run(self):
        try:
            # ★ 不新建 capture，直接用 engine 传入的
            if self._cap is None or not getattr(self._cap, "hwnd", None):
                self.log("❌ 录制：capture 未绑定窗口", "error")
                return
            self.log(f"📷 录制已启动：{getattr(self._cap, 'window_title', '?')}",
                     "ok")

            x, y, w, h = self.roi
            while not self._stop_flag:
                t_start = time.time()
                frame = self._cap.screenshot()
                if frame is None:
                    time.sleep(0.05)
                    continue
                H, W = frame.shape[:2]
                x2, y2 = min(x + w, W), min(y + h, H)
                mini = frame[y:y2, x:x2].copy()
                if mini.size == 0:
                    time.sleep(0.05)
                    continue
                self._frame_count += 1

                # ★ v30 修复：涂黄点 + 红点（中值填充，不留黑块）
                #   旧版用 [py-r:py+r, px-r:px+r] = (0,0,0) 涂黑 → 12×12 黑方块
                #   且红点完全没处理，黄点某帧找不到就残留
                mini_cleaned = _clean_dots(mini, self.player_color)

                # 调试：前 5 帧打印涂点情况（每 10 帧打一次也可以，这里先用前 5 帧）
                if self._frame_count <= 5:
                    pm = _find_yellow(mini, self.player_color)
                    if pm is not None:
                        self.log(f"📷 清理点 (黄 {pm})", "info")
                    else:
                        self.log(f"📷 清理点（未找到黄点，仅清红点）", "info")

                mini = mini_cleaned

                if self._img_map is None:
                    self._init_canvas(mini)
                else:
                    self._append(mini)

                # 10 FPS 限速
                dt = time.time() - t_start
                if dt < 0.1:
                    time.sleep(0.1 - dt)
        except Exception as e:
            import traceback
            self.log(f"录制异常: {e}\n{traceback.format_exc()}", "error")
        # ★ 不 close capture（归 engine 管）

    # ---------------- 内部实现 ----------------
    def _init_canvas(self, mini):
        """首帧：新建画布，mini 放中央"""
        mh, mw = mini.shape[:2]
        canvas_h, canvas_w = mh * 4, mw * 4
        self._img_map = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
        cx = (canvas_w - mw) // 2
        cy = (canvas_h - mh) // 2
        self._img_map[cy:cy + mh, cx:cx + mw] = mini
        self._last_mini = mini.copy()
        self._loc_last = (cx, cy)
        self.log(f"📷 首帧：画布 {canvas_w}x{canvas_h}", "info")

    def _append(self, mini):
        """后续帧：匹配偏移 → 扩展画布 → 覆盖黑像素"""
        offset = _match_offset(self._last_mini, mini)
        if offset is None:
            return
        dx, dy = offset
        # 位移 < 2px → 小地图没滚动，跳过
        if abs(dx) < 2 and abs(dy) < 2:
            return

        new_x = self._loc_last[0] + dx
        new_y = self._loc_last[1] + dy
        ph, pw = mini.shape[:2]
        mh, mw = self._img_map.shape[:2]

        # 画布不够 → 扩展
        if new_x < 0 or new_y < 0 or new_x + pw > mw or new_y + ph > mh:
            el = max(0, -new_x + 30)
            et = max(0, -new_y + 30)
            er = max(0, new_x + pw + 30 - mw)
            eb = max(0, new_y + ph + 30 - mh)
            nh, nw = mh + et + eb, mw + el + er
            new_canvas = np.zeros((nh, nw, 3), dtype=np.uint8)
            new_canvas[et:et + mh, el:el + mw] = self._img_map
            self._img_map = new_canvas
            new_x += el
            new_y += et
            self._loc_last = (self._loc_last[0] + el, self._loc_last[1] + et)

        # 只覆盖画布上的"黑色像素"
        slice_region = self._img_map[new_y:new_y + ph, new_x:new_x + pw]
        if slice_region.shape[0] == ph and slice_region.shape[1] == pw:
            black = np.all(slice_region == [0, 0, 0], axis=2)
            slice_region[black] = mini[black]

        self._last_mini = mini.copy()
        self._loc_last = (new_x, new_y)