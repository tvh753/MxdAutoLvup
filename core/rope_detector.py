# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:24
# @Author  : yy
# @File    : rope_detector.py
# @Software: MxdAutoLvup

"""主画面绳子/梯子识别：褐色细长垂直结构 → 爬绳前水平精调对位
（小地图缩放有误差，主画面绳子检测是最后一道对准保障）

识别原理：
  1. 冒险岛里绳子/梯子是「褐色」的长条，且方向竖直；
  2. 转 HSV 后按固定褐色区间取掩码；
  3. 形态学「开运算」（竖直 1x5 核）打断水平方向的干扰纹理；
  4. 连通域分析：筛选「又长又窄」的连通块（高≥min_len 且宽≤max_w），
     其中心 X 即绳子的对准坐标。
"""
import cv2


class RopeDetector:
    def __init__(self):
        self.hsv_lo = (5, 70, 60)     # 褐色 HSV（OpenCV H=H°/2），可按游戏微调
        self.hsv_hi = (28, 255, 255)
        self.min_len = 30             # 绳子最短长度 px
        self.max_w = 8                # 绳子最大宽度 px

    def find(self, frame_bgr, region=None):
        """检测画面中的绳子/梯子，返回 [(cx, top, bottom), ...]

        参数：
            frame_bgr —— 整帧 BGR 图像
            region    —— [x, y, w, h] 检测区域；None 则检测整帧
        返回：
            每个元素为 (绳子中心X, 绳子顶端Y, 绳子底端Y)，
            Y 坐标已还原到整帧坐标系。
        """
        if frame_bgr is None:
            return []
        x = y = 0
        img = frame_bgr
        if region and region[2] > 8 and region[3] > 8:
            rx, ry, rw, rh = region
            img = frame_bgr[ry:ry + rh, rx:rx + rw]
            x, y = rx, ry  # 区域偏移，用于把局部坐标还原到整帧
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.hsv_lo, self.hsv_hi)
        # 竖直开运算：去掉横向杂线（平台边缘、文字），保留细长竖直结构
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)))
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        ropes = []
        for i in range(1, n):
            bx, by, bw, bh, area = stats[i]
            # 绳子判据：足够高、足够窄、面积与高度匹配（细长条形）
            if bh >= self.min_len and bw <= self.max_w and area >= bh * 1.5:
                ropes.append((int(x + bx + bw // 2), int(y + by), int(y + by + bh)))
        return ropes