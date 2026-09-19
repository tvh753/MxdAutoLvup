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


def _find_marks_mask(mini, player_color):
    """检测红点 + 黄点，返回二值掩码（255=标记像素）

    严格阈值 + 连通域面积/长宽比/紧凑度过滤，避免误判树叶。
    """
    hsv = cv2.cvtColor(mini, cv2.COLOR_BGR2HSV)

    # 黄点：纯黄（H 22~38，S/V 高）
    yellow = cv2.inRange(hsv, np.array([22, 180, 180]),
                         np.array([38, 255, 255]))
    # 红点：纯红（H 跨 0/180，用两段）
    red1 = cv2.inRange(hsv, np.array([0, 180, 180]),
                       np.array([6, 255, 255]))
    red2 = cv2.inRange(hsv, np.array([174, 180, 180]),
                       np.array([180, 255, 255]))

    raw = cv2.bitwise_or(yellow, cv2.bitwise_or(red1, red2))
    if raw.max() == 0:
        return raw

    # 连通域过滤：面积 10~200、长宽比 ≤ 2、紧凑度 ≥ 0.5
    n, labels, stats, _ = cv2.connectedComponentsWithStats(raw, 8)
    clean = np.zeros_like(raw)
    for i in range(1, n):
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        a = int(stats[i, cv2.CC_STAT_AREA])
        if not (10 <= a <= 200):
            continue
        if max(w, h) > 2 * max(1, min(w, h)):
            continue
        if a < 0.5 * w * h:
            continue
        clean[labels == i] = 255
    return clean


def _fill_marks(img, mask, max_iter=64):
    """迭代邻域填充标记像素（只改标记像素，其他一律不动）

    每轮用 3x3 邻域内非标记像素的均值填充标记边缘，重复到填满。
    """
    if mask is None or not mask.any():
        return img
    img = img.copy()
    mask = mask.astype(bool)
    kernel = np.ones((3, 3), np.float32)
    kernel[1, 1] = 0.0

    for _ in range(max_iter):
        if not mask.any():
            break
        valid = (~mask).astype(np.float32)
        cnt = cv2.filter2D(valid, -1, kernel,
                           borderType=cv2.BORDER_REPLICATE)
        acc = np.zeros_like(img, dtype=np.float32)
        for ch in range(3):
            acc[:, :, ch] = cv2.filter2D(
                img[:, :, ch].astype(np.float32) * valid, -1, kernel,
                borderType=cv2.BORDER_REPLICATE)
        fillable = mask & (cnt > 0)
        if not fillable.any():
            break
        for ch in range(3):
            img[:, :, ch][fillable] = np.clip(
                acc[:, :, ch][fillable] / cnt[fillable], 0, 255
            ).astype(np.uint8)
        mask[fillable] = False
    return img


# ==================== 录制器 ====================
class MapRecorder(threading.Thread):
    """拼图后台线程（复用外部 capture 实例）"""

    def __init__(self, capture, roi, player_color, log_fn):
        super().__init__(daemon=True, name="MapRecorder")
        self._cap = capture                              # ★ 复用 engine 的
        self.roi = tuple(int(v) for v in roi)            # (x, y, w, h)
        self.player_color = list(player_color)
        self.log = log_fn

        self._stop_flag = False
        self._frame_count = 0
        self._img_map = None                             # 拼接画布
        self._mark_mask = None                           # ★ 标记位置（待填充）
        self._last_mini = None                           # 上一帧（匹配图）
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
        """保存拼图：填充残留标记 → 宽度对齐 → 上下裁黑边"""
        if self._img_map is None:
            return False
        img = self._img_map.copy()

        # ★ 残留标记（玩家不动/首帧位置）用迭代邻域填充
        if self._mark_mask is not None and self._mark_mask.any():
            img = _fill_marks(img, self._mark_mask)

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

                # 标记掩码
                mask = _find_marks_mask(mini, self.player_color)
                # 匹配图：标记位置涂黑（只影响 ORB，不影响拼接）
                mini_match = mini.copy()
                if mask.max() > 0:
                    mini_match[mask > 0] = (0, 0, 0)

                if self._img_map is None:
                    self._init_canvas(mini, mask, mini_match)
                else:
                    self._append(mini, mask, mini_match)

                # 10 FPS 限速
                dt = time.time() - t_start
                if dt < 0.1:
                    time.sleep(0.1 - dt)
        except Exception as e:
            import traceback
            self.log(f"录制异常: {e}\n{traceback.format_exc()}", "error")

    # ---------------- 内部实现 ----------------
    def _init_canvas(self, mini, mini_mask, mini_match):
        """首帧：新建画布

        ★ 直接写原图（不涂黑），标记位置记录到 _mark_mask。
        """
        mh, mw = mini.shape[:2]
        canvas_h, canvas_w = mh * 4, mw * 4
        self._img_map = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
        self._mark_mask = np.zeros((canvas_h, canvas_w), dtype=bool)

        cx = (canvas_w - mw) // 2
        cy = (canvas_h - mh) // 2
        self._img_map[cy:cy + mh, cx:cx + mw] = mini          # ★ 原图
        if mini_mask.max() > 0:
            self._mark_mask[cy:cy + mh, cx:cx + mw] = (mini_mask > 0)

        self._last_mini = mini_match.copy()
        self._loc_last = (cx, cy)
        self.log(f"📷 首帧：画布 {canvas_w}x{canvas_h}", "info")

    def _append(self, mini, mini_mask, mini_match):
        """后续帧：匹配偏移 → 扩展画布 → 正常写入（含标记）

        标记位置只记录到 _mark_mask，等 save 阶段统一填充。
        """
        offset = _match_offset(self._last_mini, mini_match)
        if offset is None:
            return
        dx, dy = offset
        if abs(dx) < 2 and abs(dy) < 2:
            return

        new_x = self._loc_last[0] + dx
        new_y = self._loc_last[1] + dy
        ph, pw = mini.shape[:2]
        mh, mw = self._img_map.shape[:2]

        # 画布不够 → 扩展（同步扩展 _mark_mask）
        if new_x < 0 or new_y < 0 or new_x + pw > mw or new_y + ph > mh:
            el = max(0, -new_x + 30)
            et = max(0, -new_y + 30)
            er = max(0, new_x + pw + 30 - mw)
            eb = max(0, new_y + ph + 30 - mh)
            nh, nw = mh + et + eb, mw + el + er
            new_canvas = np.zeros((nh, nw, 3), dtype=np.uint8)
            new_canvas[et:et + mh, el:el + mw] = self._img_map
            self._img_map = new_canvas

            new_mark = np.zeros((nh, nw), dtype=bool)
            new_mark[et:et + mh, el:el + mw] = self._mark_mask
            self._mark_mask = new_mark

            new_x += el
            new_y += et
            self._loc_last = (self._loc_last[0] + el, self._loc_last[1] + et)

        # 只覆盖画布上的"黑色像素"（未填充区域），原图直接写入
        slice_region = self._img_map[new_y:new_y + ph, new_x:new_x + pw]
        slice_mark = self._mark_mask[new_y:new_y + ph, new_x:new_x + pw]
        if slice_region.shape[0] == ph and slice_region.shape[1] == pw:
            black = np.all(slice_region == [0, 0, 0], axis=2)
            cur_mark = mini_mask > 0
            # 写原图（含标记），不跳过任何像素
            slice_region[black] = mini[black]
            # 更新标记掩码：写入位置的标记状态 = 当前帧该位置是否标记
            slice_mark[black] = cur_mark[black]

        self._last_mini = mini_match.copy()
        self._loc_last = (new_x, new_y)