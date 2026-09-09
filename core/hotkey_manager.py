# -*- coding: utf-8 -*-
"""全局热键管理器

支持两种模式：
  1. 局部热键（默认）：Tkinter bind，仅在控制台聚焦时生效
  2. 全局热键：使用 keyboard 库注册系统级热键，任何窗口聚焦时都生效

使用方式：
  hk = HotkeyManager(app)          # app 是 tk.Tk 实例
  hk.bind("F8", app.toggle_run)
  hk.bind("F9", app.toggle_pause)
  hk.set_global(True/False)       # 切换全局/局部模式
"""

import threading

from PIL._tkinter_finder import tk


class HotkeyManager:
    def __init__(self, tk_app):
        self.app = tk_app
        self._bindings = {}       # key_str -> (callback, tk_tag)
        self._global = False
        self._kb_hooks = {}       # key_str -> keyboard hook handle
        self._kb_available = self._check_keyboard()

    @staticmethod
    def _check_keyboard():
        """检测 keyboard 库是否可用"""
        try:
            import keyboard  # noqa
            return True
        except ImportError:
            return False

    @property
    def global_supported(self):
        """是否支持全局热键"""
        return self._kb_available

    def _tk_key(self, key_str):
        """将配置中的按键名转为 Tkinter 事件序列格式。

        转换规则（Tkinter keysym 大小写敏感，F 键必须大写）：
          - "F8" / "f8"          → "<F8>"        （功能键统一大写 F）
          - "a" / "A"            → "<Key-a>"     （单字符走 Key 事件）
          - "ctrl+f8" / "Ctrl+F8" → "<Control-F8>" （组合键）
          - 已带 <> 的输入直接规范化后返回

        返回 None 表示无法识别（调用方应跳过绑定，避免 TclError）。
        """
        s = (key_str or "").strip()
        if not s:
            return None

        # 已经是 Tkinter 序列（带 <>）：规范化功能键大小写后直接返回
        if s.startswith("<") and s.endswith(">"):
            inner = s[1:-1]
            return "<" + self._normalize_inner(inner) + ">"

        # 组合键：ctrl+x / alt+x / shift+x
        if "+" in s:
            parts = [p.strip() for p in s.split("+") if p.strip()]
            if not parts:
                return None
            mods, key_part = [], ""
            for p in parts:
                pl = p.lower()
                if pl in ("ctrl", "control"):
                    mods.append("Control")
                elif pl == "alt":
                    mods.append("Alt")
                elif pl == "shift":
                    mods.append("Shift")
                else:
                    key_part = p
            if not key_part:
                return None
            # 功能键在组合键中也要大写 F
            key_part = self._normalize_keysym(key_part)
            mod_str = "-".join(mods)
            return f"<{mod_str}-{key_part}>" if mods else f"<{key_part}>"

        # 单按键
        sym = self._normalize_keysym(s)
        if sym is None:
            return None
        # 单字符（字母/数字/标点）走 <Key-x>；功能键/特殊键直接 <sym>
        if len(sym) == 1:
            return f"<Key-{sym}>"
        return f"<{sym}>"

    @staticmethod
    def _normalize_keysym(sym):
        """规范化单个按键名：功能键 f1-f12 统一为大写 F，其他原样返回。"""
        sl = sym.lower()
        if len(sl) >= 2 and sl[0] == "f" and sl[1:].isdigit():
            return "F" + sl[1:]
        return sym

    @classmethod
    def _normalize_inner(cls, inner):
        """规范化 <> 内部的序列（处理 Control-F8 这种形式）。"""
        if "-" in inner:
            parts = inner.split("-")
            return "-".join(cls._normalize_keysym(p) for p in parts)
        return cls._normalize_keysym(inner)

    def _kb_key(self, key_str):
        """将配置中的按键名转为 keyboard 库格式"""
        return key_str.strip().lower().replace("ctrl", "ctrl")

    def bind(self, key_str, callback):
        """绑定热键到回调函数"""
        self.unbind(key_str)
        self._bindings[key_str] = callback

        if self._global and self._kb_available:
            self._bind_global(key_str, callback)
        else:
            self._bind_local(key_str, callback)

    def _bind_local(self, key_str, callback):
        """Tkinter 局部绑定（带异常保护，非法按键序列不会导致程序崩溃）"""
        tk_seq = self._tk_key(key_str)
        if not tk_seq:
            return
        try:
            self.app.bind(tk_seq, lambda e: callback())
        except tk.TclError as e:
            # 记录但不抛出：避免单个热键配置错误导致整个控制台无法启动
            print(f"[HotkeyManager] 局部热键绑定失败 [{key_str}]: {e}")

    def _bind_global(self, key_str, callback):
        """keyboard 库全局绑定"""
        if not self._kb_available:
            self._bind_local(key_str, callback)
            return
        import keyboard
        kb_key = self._kb_key(key_str)
        hook = keyboard.add_hotkey(kb_key, callback, suppress=False)
        self._kb_hooks[key_str] = hook

    def unbind(self, key_str):
        """解绑某个热键"""
        # 移除 Tkinter 绑定
        tk_seq = self._tk_key(key_str)
        if tk_seq:
            try:
                self.app.unbind(tk_seq)
            except (KeyError, tk.TclError):
                pass
        # 移除全局绑定
        if key_str in self._kb_hooks:
            try:
                import keyboard
                keyboard.remove_hotkey(self._kb_hooks[key_str])
            except Exception:
                pass
            del self._kb_hooks[key_str]
        self._bindings.pop(key_str, None)

    def set_global(self, enabled):
        """切换全局/局部模式"""
        if enabled and not self._kb_available:
            return False
        was_global = self._global
        self._global = enabled
        if was_global == enabled:
            return True
        # 重新绑定所有热键
        bindings = dict(self._bindings)
        for key_str in list(self._kb_hooks.keys()):
            self._unbind_global_raw(key_str)
        for key_str in list(self._bindings.keys()):
            try:
                seq = self._tk_key(key_str)
                if seq:
                    self.app.unbind(seq)
            except (KeyError, Exception):
                pass
        for key_str, callback in bindings.items():
            if enabled and self._kb_available:
                self._bind_global(key_str, callback)
            else:
                self._bind_local(key_str, callback)
        return True

    def _unbind_global_raw(self, key_str):
        if key_str in self._kb_hooks:
            try:
                import keyboard
                keyboard.remove_hotkey(self._kb_hooks[key_str])
            except Exception:
                pass
            del self._kb_hooks[key_str]

    def cleanup(self):
        """清理所有全局热键绑定"""
        for key_str in list(self._kb_hooks.keys()):
            self._unbind_global_raw(key_str)
        self._kb_hooks.clear()
        self._bindings.clear()
