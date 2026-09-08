# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:06
# @Author  : yy
# @File    : action_controller.py
# @Software: MxdAutoLvup

"""DirectInput 扫描码模拟 + 连续移动状态机

作用：把「决策结果」翻译成真实的键盘输入，发到游戏窗口。
为什么用 pydirectinput：普通的 keybd_event / PostMessage 对部分
游戏（含冒险岛怀旧服）无效，DirectInput 扫描码方式是兼容性最好的
后台按键方案。

模块内两个类分工：
  ActionController  —— 「点按类」瞬态动作：攻击 / 跳跃 / 喝药 / 抓绳 / 组合键
  MovementController—— 「持续按住」移动状态机：左/右/上/下 的按住与松开，
                        是角色匀速行走与爬绳的底层支持。
"""
import time
import pydirectinput

pydirectinput.PAUSE = 0.02      # 两次按键之间最小间隔（秒），防止过快丢键
pydirectinput.FAILSAFE = False  # 关闭鼠标移到屏幕角自动终止的安全机制（后台运行用）


class ActionController:
    """点按类动作（攻击/跳跃/喝药/抓绳）"""
    def __init__(self):
        self._cd = {}  # 冷却表：tag -> 上次触发时间（用于按键冷却控制）

    def tap(self, key, hold=0.05):
        """快速点按一个键：按下 → 保持 hold 秒 → 松开"""
        if not key:
            return
        pydirectinput.keyDown(key)
        time.sleep(hold)
        pydirectinput.keyUp(key)

    def hold(self, key, seconds):
        """按住一个键持续 seconds 秒（如转身微调方向）"""
        if not key:
            return
        pydirectinput.keyDown(key)
        time.sleep(seconds)
        pydirectinput.keyUp(key)

    def cooldown_ok(self, tag, cd) -> bool:
        """冷却检查：距上次触发（tag 标签）已超过 cd 秒则放行并记录时间。

        用于攻击/喝药/拾取等需要限频的动作，防止同一帧内重复按键。
        返回 True 表示本次允许执行，同时刷新冷却起点。
        """
        now = time.time()
        if now - self._cd.get(tag, 0.0) >= cd:
            self._cd[tag] = now
            return True
        return False

    def combo(self, main_key, assist_key=None, hold=0.3):
        """组合键：按住 assist_key 期间点按 main_key（下跳=↓+跳 / 传送=方向+技能）

        实现顺序：按下辅助键 → 点按主键 → 保持辅助键到 hold 时长 → 松开辅助键。
        这样能保证“主键按下时辅助键已按住”，兼容需要同时按的游戏判定。
        """
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
        self._left = self._right = self._up = self._down = None  # 绑定的键值
        self._h = None      # 当前按住的水平键（左/右）
        self._v = None      # 当前按住的竖直键（爬绳用 上/下）

    def bind(self, keys):
        """绑定按键配置；水平键变化时先全部松开，防止旧键卡住"""
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
            return  # 方向没变 → 不发键，避免频繁 keyDown/keyUp 抖动
        if self._h:
            pydirectinput.keyUp(self._h)  # 先松开旧键
        self._h = key
        if self._h:
            pydirectinput.keyDown(self._h)  # 再按住新键

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