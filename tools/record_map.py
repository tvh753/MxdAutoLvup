# -*- coding: utf-8 -*-
# @Time    : 26/9/18 22:27
# @Author  : yy
# @File    : record_map.py
# @Software: MxdAutoLvup

"""地图录制工具：玩家在游戏里走一圈，自动拼出 map.png

与参考项目 routeRecorder 的区别：
  · 不用键盘监听画路线，只拼地图
  · 复用项目的 FastWindowCapture 截图
  · 用 config.json 里的小地图 ROI

用法：
    .venv\\Scripts\\python.exe tools\\record_map.py --map 南部森林训练场Ⅲ

结束：
    切到工具窗口按 'q' 保存并退出
"""
import argparse
import os
import sys
import time

# 项目根目录加入 sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, ROOT)

import cv2
import numpy as np

from core.config_manager import ConfigManager
from core.window_capture import FastWindowCapture, WindowCapture



def detect_border_rows(mini_bgr, var_thresh=12, max_skip=10):
    """检测小地图上下纯色边框的行数

    原理：边框行是纯色（整行像素几乎一样），方差极小。
    返回 (top_skip, bottom_skip)
    """
    gray = cv2.cvtColor(mini_bgr, cv2.COLOR_BGR2GRAY)
    h = gray.shape[0]
    row_std = gray.std(axis=1)
    top = 0
    while top < min(max_skip, h) and row_std[top] < var_thresh:
        top += 1
    bottom = 0
    while bottom < min(max_skip, h) and row_std[h - 1 - bottom] < var_thresh:
        bottom += 1
    return top, bottom

def detect_border(mini_bgr, var_thresh=15, max_border=20):
    """检测小地图上下左右的纯色边框宽度

    原理：边框行/列的像素方差极小（纯色）。
    返回 (top, bottom, left, right) 边框像素宽度。
    """
    h, w = mini_bgr.shape[:2]
    gray = cv2.cvtColor(mini_bgr, cv2.COLOR_BGR2GRAY)
    row_var = gray.var(axis=1)  # 每行的方差
    col_var = gray.var(axis=0)  # 每列的方差

    # 从上往下找：方差 < 阈值 视为边框
    top = 0
    while top < min(max_border, h) and row_var[top] < var_thresh:
        top += 1
    # 从下往上
    bottom = 0
    while bottom < min(max_border, h) and row_var[h - 1 - bottom] < var_thresh:
        bottom += 1
    # 从左往右
    left = 0
    while left < min(max_border, w) and col_var[left] < var_thresh:
        left += 1
    # 从右往左
    right = 0
    while right < min(max_border, w) and col_var[w - 1 - right] < var_thresh:
        right += 1

    return top, bottom, left, right


# ==================== 图像工具 ====================
def find_pattern_sqdiff(img, pattern, mask=None, last_result=None,
                        local_search_radius=50, global_threshold=0.4):
    """带掩膜的 SQDIFF_NORMED 模板匹配（照搬参考项目）

    返回 ((loc_x, loc_y), score, is_local)
    """
    ih, iw = img.shape[:2]
    ph, pw = pattern.shape[:2]
    # padding 保证模板不超出
    if ih < ph or iw < pw:
        img = cv2.copyMakeBorder(img, 0, max(0, ph - ih),
                                 0, max(0, pw - iw),
                                 cv2.BORDER_CONSTANT, value=0)

    # 局部搜索优先
    if last_result is not None and global_threshold > 0.0:
        lx, ly = last_result
        x0 = max(0, lx - local_search_radius)
        y0 = max(0, ly - local_search_radius)
        x1 = min(img.shape[1], lx + local_search_radius + pw)
        y1 = min(img.shape[0], ly + local_search_radius + ph)
        roi = img[y0:y1, x0:x1]
        if roi.shape[0] >= ph and roi.shape[1] >= pw:
            try:
                res = cv2.matchTemplate(roi, pattern,
                                        cv2.TM_SQDIFF_NORMED, mask=mask)
                min_val, _, min_loc, _ = cv2.minMaxLoc(res)
                if min_val < global_threshold:
                    return ((x0 + min_loc[0], y0 + min_loc[1]),
                            float(min_val), True)
            except cv2.error:
                pass

    # 全图搜索
    try:
        res = cv2.matchTemplate(img, pattern, cv2.TM_SQDIFF_NORMED, mask=mask)
        res = np.nan_to_num(res, nan=1.0, posinf=1.0, neginf=1.0)
        min_val, _, min_loc, _ = cv2.minMaxLoc(res)
    except cv2.error:
        return (0, 0), 1.0, False
    return min_loc, float(min_val), False


def ensure_map_capacity(img_map, x, y, h, w, pad=30):
    """确保 img_map 足够大，容纳 (x, y, h, w) 区域

    返回 (new_map, (expand_left, expand_top))
    """
    map_h, map_w = img_map.shape[:2]
    expand_top = max(0, pad - y)
    expand_left = max(0, pad - x)
    expand_bottom = max(0, y + h + pad - map_h)
    expand_right = max(0, x + w + pad - map_w)

    if not (expand_top or expand_bottom or expand_left or expand_right):
        return img_map, (0, 0)

    new_h = map_h + expand_top + expand_bottom
    new_w = map_w + expand_left + expand_right
    new_map = np.zeros((new_h, new_w, 3), dtype=np.uint8)
    new_map[expand_top:expand_top + map_h,
            expand_left:expand_left + map_w] = img_map
    return new_map, (expand_left, expand_top)

def trim_black_border(mini_bgr):
    """裁掉小地图左右黑边（按列平均亮度找内容区）"""
    if mini_bgr is None or mini_bgr.size == 0:
        return mini_bgr
    gray = cv2.cvtColor(mini_bgr, cv2.COLOR_BGR2GRAY)
    col_mean = gray.mean(axis=0)
    valid = np.where(col_mean > 20)[0]
    if len(valid) < 5:
        return mini_bgr
    x0, x1 = int(valid[0]), int(valid[-1]) + 1
    if x1 - x0 < 5:
        return mini_bgr
    return mini_bgr[:, x0:x1]

# ==================== SIFT 特征匹配 ====================
_sift = cv2.ORB_create(nfeatures=500)  # 全局只创建一次（创建开销大）

def match_offset(img_a_bgr, img_b_bgr):
    """找 img_b 相对 img_a 的偏移量 (dx, dy)

    返回：img_b 左上角在 img_a 坐标系里的位置
    返回 None 表示匹配失败（特征点不足）

    原理：
      · 用 SIFT 提取两图特征点
      · KNN + 比率测试筛好匹配
      · 取最佳匹配对：p1（img_a 里）对应 p2（img_b 里）
      · 偏移 = p1 - p2
    """
    if img_a_bgr is None or img_b_bgr is None:
        return None

    gray_a = cv2.cvtColor(img_a_bgr, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(img_b_bgr, cv2.COLOR_BGR2GRAY)

    kp_a, des_a = _sift.detectAndCompute(gray_a, None)
    kp_b, des_b = _sift.detectAndCompute(gray_b, None)
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

    # 取最佳匹配
    best = min(good, key=lambda x: x.distance)
    p1 = kp_a[best.queryIdx].pt
    p2 = kp_b[best.trainIdx].pt
    dx = p1[0] - p2[0]
    dy = p1[1] - p2[1]
    return int(round(dx)), int(round(dy))

def find_yellow_dot(mini_bgr, color_bgr):
    """在小地图里找玩家黄点（精确颜色匹配）"""
    mask = cv2.inRange(mini_bgr,
                       np.array(color_bgr, dtype=np.uint8),
                       np.array(color_bgr, dtype=np.uint8))
    coords = cv2.findNonZero(mask)
    if coords is None or len(coords) < 4:
        return None
    pts = coords.reshape(-1, 2)
    return (int(pts[:, 0].mean()), int(pts[:, 1].mean()))


def clean_color_pixels(img, lower_hsv, upper_hsv, replace=(0, 0, 0)):
    """把 HSV 范围内面积 > 10 的连通域替换为指定颜色（涂红点）"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, lower_hsv, upper_hsv)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] > 10:
            img[labels == i] = replace


# ==================== 主流程 ====================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", required=True,
                        help="地图包目录名（输出 maps/<name>/map.png）")
    parser.add_argument("--window", default=None,
                        help="游戏窗口关键字（默认从 config.json 读）")
    parser.add_argument("--fps", type=int, default=10,
                        help="录制帧率，默认 10")
    args = parser.parse_args()

    # ---- 读配置 ----
    cfg = ConfigManager().cfg
    window_title = args.window or cfg.get("window_title", "")
    if not window_title:
        print("[错误] 未指定窗口，config.json 里也没 window_title")
        return

    mm = cfg.get("patrol", {}).get("minimap", {})
    x, y, w, h = (int(mm.get("x", 0)), int(mm.get("y", 0)),
                  int(mm.get("w", 0)), int(mm.get("h", 0)))
    if w < 5 or h < 5:
        print("[错误] 未校准小地图（config.json → patrol.minimap）")
        print("请先在控制台里点「校准小地图」")
        return
    print(f"[配置] 小地图 ROI: x={x} y={y} w={w} h={h}")

    player_dot_color = cfg.get("patrol", {}).get("player_dot_color",
                                                  [0, 128, 255])
    print(f"[配置] 玩家点颜色(BGR): {player_dot_color}")

    # ---- 输出路径 ----
    pack_dir = os.path.join(ROOT, "maps", args.map)
    if not os.path.isdir(pack_dir):
        print(f"[错误] 地图包目录不存在: {pack_dir}")
        print(f"       请先在控制台里「新增」地图包")
        return
    out_path = os.path.join(pack_dir, "map.png")
    if os.path.isfile(out_path):
        ans = input(f"[警告] {out_path} 已存在，覆盖？(y/n): ").strip().lower()
        if ans != "y":
            print("已取消")
            return

    # ---- 绑定窗口 ----
    print(f"[截图] 绑定窗口: {window_title}")
    try:
        cap = FastWindowCapture()
    except ImportError:
        cap = WindowCapture()
    if not cap.bind(window_title):
        print(f"[错误] 无法绑定窗口: {window_title}")
        return

    # ---- 主循环 ----
    img_map = None
    loc_mm_last = None
    frame_count = 0
    fps_limit = max(1, args.fps)
    t_last_info = time.time()

    print()
    print("=" * 60)
    print("  请在游戏里【走一圈】，把地图上所有位置都走遍")
    print("  工具会自动把小地图拼起来")
    print()
    print(f"  实时预览已保存到: maps\\{args.map}\\map_preview.png")
    print(f"  走完后切回本窗口，按 Ctrl+C 保存并退出")
    print("=" * 60)
    print()

    try:
        while True:
            t_start = time.time()
            frame = cap.screenshot()
            if frame is None:
                time.sleep(0.05)
                continue

            # 裁小地图 ROI
            H, W = frame.shape[:2]
            x2, y2 = min(x + w, W), min(y + h, H)
            mini = frame[y:y2, x:x2].copy()
            if mini.size == 0:
                time.sleep(0.05)
                continue

            frame_count += 1

            # ★ 首帧也要涂黄点，避免拼进地图
            pm = find_yellow_dot(mini, player_dot_color)
            if pm is not None:
                px, py = pm
                r = 6
                cx0, cx1 = max(0, px - r), min(w, px + r)
                cy0, cy1 = max(0, py - r), min(h, py + r)
                mini[cy0:cy1, cx0:cx1] = (0, 0, 0)

            # ---- 首帧：新建画布，把 mini 放中间 ----
            if img_map is None:
                mh, mw = mini.shape[:2]
                # 画布初始大小 = mini 的 4 倍
                canvas_h = mh * 4
                canvas_w = mw * 4
                img_map = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
                # 首帧放在画布中央
                cx = (canvas_w - mw) // 2
                cy = (canvas_h - mh) // 2
                img_map[cy:cy + mh, cx:cx + mw] = mini
                last_mini = mini.copy()
                loc_mm_last = (cx, cy)
                print(f"[首帧] 画布={canvas_w}x{canvas_h} "
                      f"mini={mw}x{mh} 位置=({cx},{cy})")
                continue

            # ---- SIFT 匹配：当前 mini 相对上一帧 mini 的偏移 ----
            offset = match_offset(last_mini, mini)
            if offset is None:
                if frame_count % 20 == 0:
                    print(f"[匹配] 特征不足，跳过本帧")
                time.sleep(1.0 / fps_limit)
                continue

            dx, dy = offset

            # ---- 判断小地图是否真的滚动了 ----
            # 玩家静止时 dx=dy=0（小地图完全重叠）
            if abs(dx) < 2 and abs(dy) < 2:
                if frame_count % 20 == 0:
                    print(f"[匹配] 小地图未滚动 (dx={dx} dy={dy})")
                time.sleep(1.0 / fps_limit)
                continue

            # ---- 计算当前 mini 在画布上的新位置 ----
            new_x = loc_mm_last[0] + dx
            new_y = loc_mm_last[1] + dy
            ph, pw = mini.shape[:2]
            mh, mw = img_map.shape[:2]

            # ---- 扩展画布（新位置超出边界时）----
            if new_x < 0 or new_y < 0 or \
               new_x + pw > mw or new_y + ph > mh:
                expand_left = max(0, -new_x + 30)
                expand_top = max(0, -new_y + 30)
                expand_right = max(0, new_x + pw + 30 - mw)
                expand_bottom = max(0, new_y + ph + 30 - mh)

                new_h = mh + expand_top + expand_bottom
                new_w = mw + expand_left + expand_right
                new_canvas = np.zeros((new_h, new_w, 3), dtype=np.uint8)
                new_canvas[expand_top:expand_top + mh,
                           expand_left:expand_left + mw] = img_map
                img_map = new_canvas
                # 坐标修正
                new_x += expand_left
                new_y += expand_top
                loc_mm_last = (loc_mm_last[0] + expand_left,
                               loc_mm_last[1] + expand_top)
                if frame_count % 20 == 0:
                    print(f"  → 画布扩展到 {new_w}x{new_h}")

            # ---- 找黄点并涂黑（不复制到画布）----
            pm = find_yellow_dot(mini, player_dot_color)
            if pm is not None:
                px, py = pm
                r = 7
                cx0, cx1 = max(0, px - r), min(pw, px + r)
                cy0, cy1 = max(0, py - r), min(ph, py + r)
                mini[cy0:cy1, cx0:cx1] = (0, 0, 0)

            # ---- 贴到画布：只覆盖黑色像素 ----
            slice_region = img_map[new_y:new_y + ph, new_x:new_x + pw]
            if slice_region.shape[0] == ph and slice_region.shape[1] == pw:
                black_mask2 = np.all(slice_region == [0, 0, 0], axis=2)
                slice_region[black_mask2] = mini[black_mask2]

            # ---- 更新状态 ----
            last_mini = mini.copy()
            loc_mm_last = (new_x, new_y)

            # ---- 定期清红点 ----
            if frame_count % 5 == 0:
                clean_color_pixels(img_map, (0, 100, 100), (8, 255, 255))
                clean_color_pixels(img_map, (172, 100, 100), (180, 255, 255))

            # ---- 每 20 帧打印进度 + 保存预览 ----
            if frame_count % 20 == 0:
                mh, mw = img_map.shape[:2]
                print(f"[进度] 帧={frame_count} "
                      f"offset=({dx},{dy}) 位置=({new_x},{new_y}) "
                      f"画布={mw}x{mh}")
                preview_path = os.path.join(pack_dir, "map_preview.png")
                ok, buf = cv2.imencode(".png", img_map)
                if ok:
                    buf.tofile(preview_path)

            # ---- 限速 ----
            dt = time.time() - t_start
            target = 1.0 / fps_limit
            if dt < target:
                time.sleep(target - dt)

    except KeyboardInterrupt:
        print("\n[中断] 收到 Ctrl+C，保存地图")

    # ---- 保存 ----
    if img_map is None:
        print("[错误] 没有采集到任何帧")
        return

    # ★ 1. 宽度对齐到游戏小地图 ROI 宽度（内容居中）
    target_w = int(mm.get("w", 0))
    if target_w > 5 and img_map.shape[1] > target_w:
        gray = cv2.cvtColor(img_map, cv2.COLOR_BGR2GRAY)
        cols_with = np.where(gray.max(axis=0) > 20)[0]
        if len(cols_with) > 0:
            cx_center = (int(cols_with[0]) + int(cols_with[-1])) // 2
            x0 = cx_center - target_w // 2
            x0 = max(0, min(x0, img_map.shape[1] - target_w))
            img_map = img_map[:, x0:x0 + target_w]
            print(f"[对齐] 宽度裁到 {target_w}px，内容居中")

    # ★ 2. 上下裁掉纯黑边
    gray = cv2.cvtColor(img_map, cv2.COLOR_BGR2GRAY)
    rows_with = np.where(gray.max(axis=1) > 20)[0]
    if len(rows_with) > 0:
        y0 = int(rows_with[0])
        y1 = int(rows_with[-1]) + 1
        img_map = img_map[y0:y1, :]
        print(f"[裁剪] 高度裁到 {img_map.shape[0]}px")

    ok, buf = cv2.imencode(".png", img_map)
    if ok:
        buf.tofile(out_path)
    print(f"\n✅ 已保存: {out_path}")
    print(f"   尺寸: {img_map.shape[1]}x{img_map.shape[0]}")
    print()
    print("下一步：")
    print(f"  1. 控制台里加载地图包「{args.map}」")
    print(f"  2. 重新绘制路线（底图是这张 map.png）")
    print(f"  3. 启动挂机，定位应该稳了")
    print()
    print("注意：这张 map.png 和游戏小地图 1:1，")
    print("      不需要 minimap_wz.png，也不需要 content_w 配置")


#python.exe tools\record_map.py --map 南部森林训练场Ⅲ

if __name__ == "__main__":
    main()