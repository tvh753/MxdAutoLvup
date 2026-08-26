# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:06
# @Author  : yy
# @File    : action_controller.py
# @Software: MxdAutoLvup

"""DirectInput 扫描码模拟，兼容绝大多数游戏的反 PostMessage 检测"""
import time
import pydirectinput

pydirectinput.PAUSE = 0.02
pydirectinput.FAILSAFE = False


class ActionController:
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