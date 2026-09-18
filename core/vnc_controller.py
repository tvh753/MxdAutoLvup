# -*- coding: utf-8 -*-
# @Time    : 26/9/13 19:36
# @Author  : yy
# @File    : vnc_controller.py
# @Software: MxdAutoLvup

"""VNC 模式的键鼠控制器（v2：异步下发，对齐 ActionController）

设计原则：
  ① 与 ActionController / MovementController 保持相同方法签名，
     bot_engine 里的调用点几乎不用改；
  ② 避开有 Bug 的 vnc_key_press —— 用 keyDown + 延时 + keyUp 实现单击；
  ③ 支持"持续按键"（MovementController 场景）：VNC 不像 pydirectinput
     会自动保持按住状态，必须显式 keyDown / keyUp；
  ④ 双键组合（如抓绳时的 方向+↑+跳）按"先按下、再点按、最后松开"的
     顺序发送，匹配游戏判定时序。

★ v2 架构（与 action_controller.py 的 v2 一致）：
  · tap / combo 只入队，不阻塞主循环；
  · set_dir / set_climb 只更新目标状态；
  · 独立键盘线程以 30 FPS 调 tick() 下发真实 VNC 按键；
  · 主循环卡顿（例如小地图匹配 200ms）不会影响按键节奏。
"""
import time
import threading
import queue
import random      # ★ 新增


class VncActionController:
    """单击 / 组合键 / 冷却控制器（对应 ActionController）"""

    def __init__(self, vnc_capture):
        self.cap = vnc_capture
        self._cd = {}
        self._queue = queue.Queue(maxsize=64)  # 待发动作队列

    def cooldown_ok(self, name, seconds):
        now = time.time()
        if now - self._cd.get(name, 0) >= seconds:
            self._cd[name] = now
            return True
        return False

    def tap(self, key, hold=None):
        if not key:
            return
        try:
            self._queue.put_nowait(("tap", key, hold))
        except queue.Full:
            pass

    def combo(self, k1, k2, hold=None):
        if not k1 and not k2:
            return
        try:
            self._queue.put_nowait(("combo", k1, k2, hold))
        except queue.Full:
            pass

    # ---- 键盘线程调用 ----
    def tick(self):
        """处理队列中最多 3 个待发动作（避免单帧堵塞太久）"""
        for _ in range(3):
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            action = item[0]
            try:
                if action == "tap":
                    _, key, hold = item
                    # ★ 未指定 hold → 随机 30~80ms（人敲键盘不会每次一样）
                    if hold is None:
                        hold = random.uniform(0.03, 0.08)
                    self.cap.key_down(key)
                    time.sleep(hold)
                    self.cap.key_up(key)
                elif action == "combo":
                    _, k1, k2, hold = item
                    if hold is None:
                        hold = random.uniform(0.20, 0.30)
                    pre = random.uniform(0.015, 0.035)   # 两键间隔
                    gap = random.uniform(0.015, 0.035)   # 释放间隔
                    if k1:
                        self.cap.key_down(k1)
                        time.sleep(pre)
                    if k2:
                        self.cap.key_down(k2)
                    time.sleep(hold)
                    if k2:
                        self.cap.key_up(k2)
                    if k1:
                        time.sleep(gap)
                        self.cap.key_up(k1)
            except Exception:
                pass

    def release_all(self):
        """清空队列（停机时调用）"""
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass


class VncMovementController:
    """持续方向 / 爬绳控制器（对应 MovementController，v2：异步下发）"""

    def __init__(self, vnc_capture):
        self.cap = vnc_capture
        self.cfg = {}
        self._target_dir = 0
        self._target_climb = None
        self._dir_key = None
        self._climb_key = None
        self._lock = threading.Lock()

    def bind(self, keys):
        self.cfg = keys or {}

    def _k(self, name):
        v = self.cfg.get(name, "")
        return v if v else None

    # ---- 主循环调用：只更新目标（非阻塞）----
    def set_dir(self, d):
        with self._lock:
            self._target_dir = d or 0

    def set_climb(self, c):
        with self._lock:
            self._target_climb = c if c in ("up", "down") else None

    def release_all(self):
        """立即释放所有按键（停机/失焦时调用）"""
        with self._lock:
            self._target_dir = 0
            self._target_climb = None
        self._do_tick(force_release=True)

    # ---- 键盘线程调用 ----
    def tick(self):
        self._do_tick()

    def _do_tick(self, force_release=False):
        with self._lock:
            want_dir = 0 if force_release else self._target_dir
            want_climb = None if force_release else self._target_climb

        want_dir_key = None
        if want_dir == -1:
            want_dir_key = self._k("move_left")
        elif want_dir == 1:
            want_dir_key = self._k("move_right")

        want_climb_key = None
        if want_climb == "up":
            want_climb_key = self._k("up")
        elif want_climb == "down":
            want_climb_key = self._k("down")

        if want_dir_key != self._dir_key:
            try:
                if self._dir_key:
                    self.cap.key_up(self._dir_key)
                if want_dir_key:
                    self.cap.key_down(want_dir_key)
            except Exception:
                pass
            self._dir_key = want_dir_key

        if want_climb_key != self._climb_key:
            try:
                if self._climb_key:
                    self.cap.key_up(self._climb_key)
                if want_climb_key:
                    self.cap.key_down(want_climb_key)
            except Exception:
                pass
            self._climb_key = want_climb_key