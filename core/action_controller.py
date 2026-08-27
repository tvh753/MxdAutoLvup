# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:06
# @Author  : yy
# @File    : action_controller.py
# @Software: MxdAutoLvup

"""DirectInput 扫描码模拟 + 连续移动状态机"""
import time
import pydirectinput

pydirectinput.PAUSE = 0.02
pydirectinput.FAILSAFE = False


class ActionController:
    """点按类动作（攻击/跳跃/喝药/抓绳）"""
    def __init__(self):
        self._cd = {}

    def tap(self, key, hold=0.05):
        if not key:
            return
        pydirectinput.keyDown(key)
        time.sleep(hold)
        pydirectinput.keyUp(key)

    def hold(self, key, seconds):
        if not key:
            return
        pydirectinput.keyDown(key)
        time.sleep(seconds)
        pydirectinput.keyUp(key)

    def cooldown_ok(self, tag, cd) -> bool:
        now = time.time()
        if now - self._cd.get(tag, 0.0) >= cd:
            self._cd[tag] = now
            return True
        return False

    def combo(self, main_key, assist_key=None, hold=0.3):
        """组合键：按住 assist_key 期间点按 main_key（下跳=↓+跳 / 传送=方向+技能）"""
        if assist_key:
            pydirectinput.keyDown(assist_key)
        if main_key:
            pydirectinput.keyDown(main_key)
            time.sleep(0.05)
            pydirectinput.keyUp(main_key)
        if assist_key:
            time.sleep(max(0.0, hold - 0.05))
            pydirectinput.keyUp(assist_key)


class MovementController:
    """连续移动状态机（卡顿修复核心）
    ============================================================
    旧方案每帧 keyDown→sleep(0.12)→keyUp：松键间隙角色停走，
    叠加截图耗时 → 走走停停。
    新方案只在【方向变化】瞬间发送 keyDown/keyUp，移动键持续
    按住 → 角色匀速行走；移动路径上零 sleep，引擎线程不阻塞。
    竖直键（上/下）独立管理，供爬绳使用。
    """
    def __init__(self):
        self._left = self._right = self._up = self._down = None
        self._h = None      # 当前按住的水平键
        self._v = None      # 当前按住的竖直键（爬绳）

    def bind(self, keys):
        l, r = keys.get("move_left"), keys.get("move_right")
        if (l, r) != (self._left, self._right):
            self.release_all()
        self._left, self._right = l, r
        self._up, self._down = keys.get("up"), keys.get("down")

    # ---- 水平方向 ----
    def set_dir(self, direction):
        """-1 左 / 0 停 / +1 右 / None 保持现状；仅状态变化时发键"""
        if direction is None:
            return
        key = {1: self._right, -1: self._left, 0: None}.get(direction)
        if key == self._h:
            return
        if self._h:
            pydirectinput.keyUp(self._h)
        self._h = key
        if self._h:
            pydirectinput.keyDown(self._h)

    # ---- 竖直（爬绳）----
    def set_climb(self, v):
        """'up' / 'down' 持续按住；None 松开"""
        key = self._up if v == "up" else (self._down if v == "down" else None)
        if key == self._v:
            return
        if self._v:
            pydirectinput.keyUp(self._v)
        self._v = key
        if self._v:
            pydirectinput.keyDown(key)

    def release_all(self):
        """任何停机路径必须调用，防止按键卡死导致角色失控"""
        for k in (self._h, self._v):
            if k:
                try:
                    pydirectinput.keyUp(k)
                except Exception:
                    pass
        self._h = self._v = None

    @property
    def h_dir(self):
        """当前水平方向：+1右 / -1左 / 0静止（战斗朝向判断用）"""
        if self._h and self._h == self._right:
            return 1
        if self._h and self._h == self._left:
            return -1
        return 0