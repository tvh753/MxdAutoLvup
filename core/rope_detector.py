# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:24
# @Author  : yy
# @File    : rope_detector.py
# @Software: MxdAutoLvup

"""主画面绳子/梯子识别：褐色细长垂直结构 → 爬绳前水平精调对位
（小地图缩放有误差，主画面绳子检测是最后一道对准保障）"""
import cv2


class RopeDetector:
    def __init__(self):
        self.hsv_lo = (5, 70, 60)     # 褐色 HSV（OpenCV H=H°/2），可按游戏微调
        self.hsv_hi = (28, 255, 255)
        self.min_len = 30             # 绳子最短长度 px
        self.max_w = 8                # 绳子最大宽度 px

    def find(self, frame_bgr, region=None):
        """返回 [(cx, top, bottom), ...]"""
        if frame_bgr is None:
            return []
        x = y = 0
        img = frame_bgr
        if region and region[2] > 8 and region[3] > 8:
            rx, ry, rw, rh = region
            img = frame_bgr[ry:ry + rh, rx:rx + rw]
            x, y = rx, ry
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.hsv_lo, self.hsv_hi)
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)))
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        ropes = []
        for i in range(1, n):
            bx, by, bw, bh, area = stats[i]
            if bh >= self.min_len and bw <= self.max_w and area >= bh * 1.5:
                ropes.append((int(x + bx + bw // 2), int(y + by), int(y + by + bh)))
        return ropes