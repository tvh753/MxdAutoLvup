# -*- coding: utf-8 -*-
# @Time    : 26/8/26 20:00
# @Author  : yy
# @File    : detector.py
# @Software: MxdAutoLvup

"""目标识别模块：OpenCV 多模板匹配 + NMS 去重

用途：在游戏画面中定位【怪物】与【玩家】。
原理：
  1. 每个模板转灰度（灰度匹配对光照更鲁棒，且计算更快）；
  2. 用 TM_CCOEFF_NORMED 在整帧（或检测区域）做滑动窗口匹配，
     得到每个像素位置的相似度响应图；
  3. 阈值过滤出所有可能命中点，再经 NMS（非极大值抑制）合并
     重叠框（同一怪物会被滑动窗口反复命中，需要去重）；
  4. 输出怪物中心坐标 (cx, cy) 与置信度，供决策层使用。
"""
import cv2
import numpy as np
from core.imio import imread_u


class Template:
    """单个识别模板（加载后缓存灰度图与尺寸）"""

    def __init__(self, name, path):
        self.name, self.path = name, path
        img = imread_u(path, cv2.IMREAD_COLOR)  # 原 cv2.imread（中文路径安全版）
        if img is None:
            raise FileNotFoundError(f"模板读取失败(文件不存在或解码异常): {path}")
        # 同时缓存彩色图与灰度图：
        #   - self.img  (BGR彩色): 供 find_pic 做彩色模板匹配使用
        #   - self.gray (灰度):    供 find_all 做灰度匹配使用（更快、抗光照）
        self.img = img
        self.gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        self.h, self.w = self.gray.shape[:2]


class TemplateDetector:
    """模板库管理 + 多模板匹配入口"""

    def __init__(self):
        self.templates = {}  # name -> Template

    def load(self, name, path):
        """加载一个模板（重复同名则覆盖）"""
        self.templates[name] = Template(name, path)

    def clear(self):
        """清空全部模板（重载配置 / 删除地图包时调用）"""
        self.templates.clear()

    def replace_all(self, templates_dict):
        """v21: 原子替换全部模板（避免 clear+逐个 load 时被遍历线程看到中间态）"""
        self.templates = templates_dict

    def find_pic(self, name, scene_bgr, similarity=0.9, offset=(0, 0)):
        """v20: 用户提供的 find_pic 算法（彩色 TM_CCORR_NORMED + 斜边去重）

        与原 find_all 区别：
          - 彩色匹配（非灰度），用 TM_CCORR_NORMED（非 TM_CCOEFF_NORMED）
          - 去重用斜边平方（hypotenuse_sqr = h² + w²）作为距离阈值，
            两匹配点距离平方 <= 模板对角线平方 → 视为同一目标，只留第一个

        参数:
            name: 模板名
            scene_bgr: 游戏画面截图（BGR）
            similarity: 相似度阈值，默认 0.9
            offset: (x1, y1) 场景裁剪偏移，匹配坐标会加上 offset 还原到整帧坐标系

        返回: [(cx, cy, conf, w, h), ...] 中心点列表（坐标已还原到整帧坐标系）
        """
        tpl = self.templates.get(name)
        if tpl is None or scene_bgr is None:
            return []
        x1, y1 = offset
        img_template = tpl.img
        height, width = img_template.shape[:2]
        sh, sw = scene_bgr.shape[:2]
        if sh < height or sw < width:
            return []

        # 斜边平方作为去重距离阈值（模板对角线平方）
        hypotenuse_sqr = height ** 2 + width ** 2

        # 彩色模板匹配
        template_res = cv2.matchTemplate(scene_bgr, img_template, cv2.TM_CCORR_NORMED)
        res = np.where(template_res >= similarity)
        res_zip = zip(res[0], res[1])  # (y, x) 对

        center_point_list = []
        for a in res_zip:
            b = a[::-1]  # 转成 (x, y)
            # 中心点 = (x1 + b[0] + width//2, y1 + b[1] + height//2)
            center_point = (int(x1 + b[0] + width // 2),
                            int(y1 + b[1] + height // 2))
            if len(center_point_list) == 0:
                center_point_list.append(center_point)
            else:
                not_append = False
                for i in center_point_list:
                    hypotenuse_sqr_point = (center_point[0] - i[0]) ** 2 + \
                                           (center_point[1] - i[1]) ** 2
                    if hypotenuse_sqr_point <= hypotenuse_sqr:
                        not_append = True
                if not not_append:
                    center_point_list.append(center_point)

        # 转成 (cx, cy, conf, w, h) 格式，兼容下游 _box_of
        result = []
        for cx, cy in center_point_list:
            # 反推匹配点分数
            tx = cx - x1 - width // 2
            ty = cy - y1 - height // 2
            conf = float(template_res[ty, tx]) if 0 <= ty < template_res.shape[0] \
                and 0 <= tx < template_res.shape[1] else 0.9
            result.append((cx, cy, conf, width, height))
        return result

    def find_all(self, name, threshold=0.8, scene_bgr=None, scene_gray=None,
                 offset=(0, 0), max_results=30):
        """返回 [(cx, cy, conf, w, h), ...]，坐标已还原到整帧坐标系

        参数：
            name        —— 模板名（须先 load）
            threshold   —— 匹配置信度阈值（0~1，越高越严格）
            scene_bgr / scene_gray —— 待检测画面；两者传其一即可
                                 （外部可预先转好灰度并传 scene_gray，省一次转换）
            offset      —— 检测区域左上角在整帧中的坐标（区域检测时用于还原）
            max_results —— 最多返回的命中数（按置信度从高到低）
        """
        tpl = self.templates.get(name)
        if tpl is None:
            return []
        if scene_gray is None:
            if scene_bgr is None:
                return []
            scene_gray = cv2.cvtColor(scene_bgr, cv2.COLOR_BGR2GRAY)

        sh, sw = scene_gray.shape[:2]
        if sh < tpl.h or sw < tpl.w:
            return []  # 画面比模板还小，肯定匹配不到

        res = cv2.matchTemplate(scene_gray, tpl.gray, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= threshold)
        # 收集所有超过阈值的候选框：[x, y, w, h, conf]
        boxes = [[int(x) + offset[0], int(y) + offset[1], tpl.w, tpl.h, float(res[y, x])]
                 for x, y in zip(xs, ys)]
        boxes = self._nms(boxes, 0.3)  # IoU>0.3 的重叠框合并为最高置信度的一个
        boxes = sorted(boxes, key=lambda b: -b[4])[:max_results]
        return [(b[0] + b[2] // 2, b[1] + b[3] // 2, b[4], b[2], b[3]) for b in boxes]

    @staticmethod
    def _nms(boxes, iou_thresh):
        """非极大值抑制（NMS）：把互相重叠的候选框去重，只保留置信度最高的

        流程：按置信度从高到低排序，依次取一个框，
        若它与已保留的某个框 IoU（交并比）超过阈值，则丢弃；
        否则保留。防止同一个目标被滑动窗口重复检出。
        """
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