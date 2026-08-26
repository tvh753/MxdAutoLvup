# -*- coding: utf-8 -*-
# @Time    : 26/8/26 20:00
# @Author  : yy
# @File    : detector.py
# @Software: MxdAutoLvup

import cv2
import numpy as np


class Template:
    def __init__(self, name, path):
        self.name, self.path = name, path
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(f"模板读取失败: {path}")
        self.gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)  # 灰度匹配抗光照
        self.h, self.w = self.gray.shape[:2]


class TemplateDetector:
    def __init__(self):
        self.templates = {}

    def load(self, name, path):
        self.templates[name] = Template(name, path)

    def clear(self):
        self.templates.clear()

    def find_all(self, name, threshold=0.8, scene_bgr=None, scene_gray=None,
                 offset=(0, 0), max_results=30):
        """返回 [(cx, cy, conf, w, h), ...]，坐标已还原到整帧坐标系"""
        tpl = self.templates.get(name)
        if tpl is None:
            return []
        if scene_gray is None:
            if scene_bgr is None:
                return []
            scene_gray = cv2.cvtColor(scene_bgr, cv2.COLOR_BGR2GRAY)

        sh, sw = scene_gray.shape[:2]
        if sh < tpl.h or sw < tpl.w:
            return []

        res = cv2.matchTemplate(scene_gray, tpl.gray, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= threshold)
        boxes = [[int(x) + offset[0], int(y) + offset[1], tpl.w, tpl.h, float(res[y, x])]
                 for x, y in zip(xs, ys)]
        boxes = self._nms(boxes, 0.3)
        boxes = sorted(boxes, key=lambda b: -b[4])[:max_results]
        return [(b[0] + b[2] // 2, b[1] + b[3] // 2, b[4], b[2], b[3]) for b in boxes]

    @staticmethod
    def _nms(boxes, iou_thresh):
        if not boxes:
            return []
        keep, boxes = [], sorted(boxes, key=lambda b: -b[4])
        for b in boxes:
            drop = False
            for k in keep:
                x1, y1 = max(b[0], k[0]), max(b[1], k[1])
                x2 = y2_ = None  # placeholder
                x2 = min(b[0] + b[2], k[0] + k[2])
                y2_ = min(b[1] + b[3], k[1] + k[3])
                inter = max(0, x2 - x1) * max(0, y2_ - y1)
                union = b[2] * b[3] + k[2] * k[3] - inter
                if union > 0 and inter / union > iou_thresh:
                    drop = True; break
            if not drop:
                keep.append(b)
        return keep