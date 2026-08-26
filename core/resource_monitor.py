# -*- coding: utf-8 -*-
# @Time    : 26/8/26 20:00
# @Author  : yy
# @File    : resource_monitor.py
# @Software: MxdAutoLvup

# -*- coding: utf-8 -*-
"""
状态条实时监测（HP红 / MP蓝 / EXP黄）
====================================
针对冒险岛底部状态栏样式设计：
    HP[070/070]   ← 红色渐变条
    MP[070/070]   ← 蓝色渐变条
    EXP[10.20%]   ← 黄色渐变条
识别管线：
  ROI 裁剪 → HSV 色相掩码（多区间）→ 列投票
  → 容忍断裂的最长连续段 → 填充比例 → EMA 平滑
"""
import cv2
import numpy as np

# OpenCV HSV：H∈[0,180)，S/V∈[0,255]
PRESETS = {
    "hp": [(0, 10), (170, 180)],  # 红：色相环绕 0°，必须两段
    "mp": [(100, 130)],  # 蓝
    "exp": [(18, 35)],  # 黄
}
SV_RANGE = (60, 255)  # S/V 下限：滤掉暗色空槽与白色文字
COL_RATIO = 0.25  # 列投票：列内 25% 像素命中才算有效列
GAP_TOL = 4  # 容忍连续 4 列断裂（数字文字造成的缺口）
EMA_ALPHA = 0.35  # 平滑系数：越大越灵敏、越易跳变


class BarMonitor:
    def __init__(self, kind="hp", region=(0, 0, 0, 0)):
        self.kind = kind
        self.region = tuple(int(v) for v in region)
        self._h_ranges = [tuple(r) for r in PRESETS.get(kind, PRESETS["hp"])]
        self._ema = None

    # ---------------- 配置 ----------------
    def set(self, region=None, kind=None):
        """热更新区域；kind 变化时重置为对应预设"""
        if region:
            self.region = tuple(int(v) for v in region)
        if kind and kind != self.kind:
            self.kind = kind
            self._h_ranges = [tuple(r) for r in PRESETS.get(kind, PRESETS["hp"])]
            self._ema = None

    @property
    def calibrated(self):
        x, y, w, h = self.region
        return w > 4 and h > 2

    # ---------------- 颜色自适配 ----------------
    def calibrate_color(self, frame_bgr):
        """满状态时调用：采样 ROI 内高饱和亮像素的色相中位数，
        将识别区间中心微调到实测颜色（应对皮肤/滤镜差异）。
        返回采样色相 H（日志展示用），失败返回 None。"""
        roi = self._roi(frame_bgr)
        if roi is None:
            return None
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = (hsv[..., 1] > 50) & (hsv[..., 2] > 50)  # 只要彩色亮像素
        if not mask.any():
            return None  # 框内没有彩色 → 框错了
        med = float(np.median(hsv[..., 0][mask]))
        if self.kind == "hp" and med > 90:  # 高位红 → 映射为负数统一计算
            med -= 180.0
        center = int(round(med))
        lo, hi = center - 8, center + 8
        if self.kind == "hp":  # 红色环绕 0/180 拆两段
            segs = [(max(0, lo), min(180, hi))]
            if lo < 0:
                segs.append((170, 180))
            if hi > 180:
                segs.append((0, hi - 180))
            self._h_ranges = [s for s in segs if s[0] < s[1]] or PRESETS["hp"]
        else:
            self._h_ranges = [(max(0, lo), min(180, hi))]
        self._ema = None
        return center + 180 if center < 0 else center

    # ---------------- 核心检测 ----------------
    def percentage(self, frame_bgr):
        """返回 0~100 的填充百分比；未校准 / ROI 越界返回 -1"""
        if not self.calibrated or frame_bgr is None:
            return -1.0
        roi = self._roi(frame_bgr)
        if roi is None:
            return -1.0
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        h, w = roi.shape[:2]
        # ① HSV 多区间色相掩码
        mask = np.zeros((h, w), dtype=np.uint8)
        sv_lo, sv_hi = SV_RANGE
        for lo, hi in self._h_ranges:
            mask |= cv2.inRange(hsv, (lo, sv_lo, sv_lo), (hi, sv_hi, sv_hi))
        # ② 列投票：每列命中像素数 ≥ 25% 视为有效列
        col = mask.sum(axis=0).astype(np.float32)
        valid = col >= max(1.0, h * COL_RATIO)
        # ③ 容忍断裂的最长连续段（数字文字不会打断填充判断）
        start, end = self._longest_run(valid, GAP_TOL)
        if end <= start:
            raw = 0.0
        elif start <= 2:  # 常态：段从条左端开始
            raw = end / w * 100.0
        else:  # 左端被图标/描边遮挡
            raw = (end - start) / (w - start) * 100.0
        # ④ EMA 平滑
        raw = min(100.0, max(0.0, raw))
        self._ema = raw if self._ema is None else \
            EMA_ALPHA * raw + (1 - EMA_ALPHA) * self._ema
        return round(self._ema, 1)

    # ---------------- 工具 ----------------
    def _roi(self, frame_bgr):
        x, y, w, h = self.region
        H, W = frame_bgr.shape[:2]
        x2, y2 = min(x + w, W), min(y + h, H)
        if x < 0 or y < 0 or x2 <= x or y2 <= y:
            return None
        return frame_bgr[y:y2, x:x2]

    @staticmethod
    def _longest_run(valid, gap_tol):
        """布尔数组中找最长连续 True 段 [start, end)，允许 gap_tol 个 False 打断。
        段长按跨度计算——数字缺口处条实际仍有填充，理应计入。"""
        n = len(valid)
        best_s = best_e = 0
        i = 0
        while i < n:
            if not valid[i]:
                i += 1
                continue
            j, last, gap = i, i, 0
            while j < n:
                if valid[j]:
                    last, gap = j, 0
                else:
                    gap += 1
                    if gap > gap_tol:
                        break
                j += 1
            if last + 1 - i > best_e - best_s:
                best_s, best_e = i, last + 1
            i = last + 1
        return best_s, best_e