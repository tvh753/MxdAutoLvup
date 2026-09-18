# -*- coding: utf-8 -*-
"""全局热键管理器 v2（改用 pynput）

支持两种模式：
  1. 局部热键（默认）：Tkinter bind，仅在控制台聚焦时生效
  2. 全局热键：使用 pynput 库注册系统级热键，任何窗口聚焦时都生效

★ 相比 keyboard 库，pynput 在游戏窗口和部分特殊场景下的兼容性更好：
    · keyboard 用 SetWindowsHookEx 拦 WH_KEYBOARD_LL，某些 DirectInput
      独占模式下会失效
    · pynput 也是底层钩子，但对焦点窗口更宽容，实测游戏内更稳

使用方式：
  hk = HotkeyManager(app)
  hk.bind("F8", app.toggle_run)
  hk.bind("F9", app.toggle_pause)
  hk.set_global(True/False)

★ Windows 下若仍不生效，请用管理员身份运行本程序（或命令行）。
"""

import tkinter as tk


class HotkeyManager:
    def __init__(self, tk_app):
        self.app = tk_app
        self._bindings = {}          # key_str -> callback（原始配置名，如 "F8"）
        self._global = False
        self._listener = None        # pynput.GlobalHotKeys 实例
        self._kb_available = self._check_pynput()
        if not self._kb_available:
            print("[HotkeyManager] ⚠ 未安装 pynput 库，全局热键不可用。"
                  "请运行: pip install pynput")

    @staticmethod
    def _check_pynput():
        """检测 pynput 库是否可用"""
        try:
            import pynput  # noqa
            return True
        except ImportError:
            return False

    @property
    def global_supported(self):
        return self._kb_available

    # ============ 键名转换：Tkinter（局部） ============
    def _tk_key(self, key_str):
        """将配置中的按键名转为 Tkinter 事件序列格式。

        转换规则（Tkinter keysym 大小写敏感，F 键必须大写）：
          - "F8" / "f8"          → "<F8>"
          - "a" / "A"            → "<Key-a>"
          - "ctrl+f8"            → "<Control-F8>"
        返回 None 表示无法识别。
        """
        s = (key_str or "").strip()
        if not s:
            return None
        if s.startswith("<") and s.endswith(">"):
            return "<" + self._normalize_inner(s[1:-1]) + ">"
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
            key_part = self._normalize_keysym(key_part)
            mod_str = "-".join(mods)
            return f"<{mod_str}-{key_part}>" if mods else f"<{key_part}>"
        sym = self._normalize_keysym(s)
        if sym is None:
            return None
        if len(sym) == 1:
            return f"<Key-{sym}>"
        return f"<{sym}>"

    @staticmethod
    def _normalize_keysym(sym):
        """功能键 f1-f12 统一为大写 F，其他原样返回"""
        sl = sym.lower()
        if len(sl) >= 2 and sl[0] == "f" and sl[1:].isdigit():
            return "F" + sl[1:]
        return sym

    @classmethod
    def _normalize_inner(cls, inner):
        """规范化 <> 内部的序列（如 Control-F8）"""
        if "-" in inner:
            return "-".join(cls._normalize_keysym(p) for p in inner.split("-"))
        return cls._normalize_keysym(inner)

    # ============ 键名转换：pynput（全局） ============
    def _py_key(self, key_str):
        """转换为 pynput GlobalHotKeys 格式的字符串

        pynput 格式：
          'a'、'1'                 单字符
          '<f8>'、'<enter>'        特殊键
          '<ctrl>+<f8>'            组合键
        返回 None 表示无法识别。
        """
        s = (key_str or "").strip().lower()
        if not s:
            return None

        # 特殊键名映射（配置名 → pynput 内部名）
        special = {
            "esc": "esc", "escape": "esc",
            "enter": "enter", "return": "enter",
            "space": "space", "tab": "tab",
            "backspace": "backspace", "bsp": "backspace",
            "delete": "delete", "del": "delete",
            "up": "up", "down": "down", "left": "left", "right": "right",
            "home": "home", "end": "end",
            "pageup": "page_up", "pagedown": "page_down",
            "capslock": "caps_lock", "caps": "caps_lock",
        }

        def _one(part):
            p = part.strip().lower()
            # 修饰键
            if p in ("ctrl", "control"):
                return "<ctrl>"
            if p == "alt":
                return "<alt>"
            if p == "shift":
                return "<shift>"
            if p in ("win", "cmd", "super"):
                return "<cmd>"
            # 功能键 f1-f12
            if len(p) >= 2 and p[0] == "f" and p[1:].isdigit():
                return f"<{p}>"
            # 特殊键
            if p in special:
                return f"<{special[p]}>"
            # 单字符
            if len(p) == 1:
                return p
            # 其他无法识别 → 原样包进 <>
            return f"<{p}>"

        if "+" in s:
            parts = [p.strip() for p in s.split("+") if p.strip()]
            if not parts:
                return None
            return "+".join(_one(p) for p in parts)
        return _one(s)

    # ============ 绑定 ============
    def bind(self, key_str, callback):
        """绑定热键到回调函数"""
        # 先从旧绑定里移除（同键 rebind 场景）
        self._bindings.pop(key_str, None)
        self._bindings[key_str] = callback

        if self._global and self._kb_available:
            self._rebuild_global_listener()
        else:
            self._bind_local(key_str, callback)

    def _bind_local(self, key_str, callback):
        """Tkinter 局部绑定（带异常保护）"""
        tk_seq = self._tk_key(key_str)
        if not tk_seq:
            print(f"[HotkeyManager] ⚠ 无法识别的按键 [{key_str}]，跳过局部绑定")
            return
        try:
            self.app.bind(tk_seq, lambda e: callback())
        except tk.TclError as e:
            print(f"[HotkeyManager] 局部热键绑定失败 [{key_str}]: {e}")

    def _make_global_callback(self, key_str, callback):
        """创建全局热键回调

        ★ pynput 的回调在它自己的监听线程里执行，而 toggle_run/start_bot
        内部会操作 tkinter（messagebox、控件更新）。tkinter 不是线程安全
        的，跨线程调用会崩溃或静默失败 —— 所以必须用 app.after(0, ...)
        把回调调度到 tkinter 主线程。
        """
        def _dispatch():
            print(f"[HotkeyManager] 🎯 全局热键触发: {key_str}")
            try:
                self.app.after(0, callback)
            except Exception as e:
                print(f"[HotkeyManager] after 调度失败: {e}，直接调用回调")
                try:
                    callback()
                except Exception as e2:
                    print(f"[HotkeyManager] 全局热键回调异常 [{key_str}]: {e2}")
                    import traceback
                    traceback.print_exc()
        return _dispatch

    def _rebuild_global_listener(self):
        """重建 pynput 全局监听器（每次绑定变化时调用）

        pynput 的 GlobalHotKeys 不支持运行时动态增删热键 —— 必须停止
        旧监听器、创建新的。每次 bind/unbind 都重建一遍。
        """
        if not self._kb_available:
            return
        # 先停旧监听器
        self._stop_global_listener()

        # 组装热键字典 {pynput_key: callback}
        py_hotkeys = {}
        for key_str, cb in self._bindings.items():
            py_key = self._py_key(key_str)
            if not py_key:
                print(f"[HotkeyManager] ⚠ 无法转换为 pynput 键: [{key_str}]")
                continue
            py_hotkeys[py_key] = self._make_global_callback(key_str, cb)

        if not py_hotkeys:
            return  # 暂无热键，静默返回

        # 启动新监听器
        try:
            from pynput import keyboard as pk
            self._listener = pk.GlobalHotKeys(py_hotkeys)
            self._listener.daemon = True
            self._listener.start()
        except Exception as e:
            print(f"[HotkeyManager] ❌ 启动全局监听失败: {e}")
            import traceback
            traceback.print_exc()
            self._listener = None

    def _stop_global_listener(self):
        """停止全局监听器"""
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def unbind(self, key_str):
        """解绑某个热键"""
        # 局部：解 Tk 绑定
        tk_seq = self._tk_key(key_str)
        if tk_seq:
            try:
                self.app.unbind(tk_seq)
            except (KeyError, tk.TclError):
                pass
        # 全局：从 _bindings 移除，重建监听
        had = self._bindings.pop(key_str, None)
        if had is not None and self._global and self._kb_available:
            self._rebuild_global_listener()

    def set_global(self, enabled):
        """切换全局/局部模式"""
        if enabled and not self._kb_available:
            print("[HotkeyManager] ⚠ pynput 库不可用，无法启用全局热键")
            return False
        was_global = self._global
        self._global = enabled
        if was_global == enabled:
            return True

        if enabled:
            # 局部 → 全局
            # 先解除所有 Tk 局部绑定
            for key_str in list(self._bindings.keys()):
                tk_seq = self._tk_key(key_str)
                if tk_seq:
                    try:
                        self.app.unbind(tk_seq)
                    except (KeyError, tk.TclError):
                        pass
            # 重建全局监听
            self._rebuild_global_listener()
            print("[HotkeyManager] 热键模式: 全局（pynput）")
        else:
            # 全局 → 局部
            self._stop_global_listener()
            for key_str, cb in self._bindings.items():
                self._bind_local(key_str, cb)
            print("[HotkeyManager] 热键模式: 局部（tkinter）")
        return True

    def cleanup(self):
        """清理所有热键（退出程序时调用）"""
        self._stop_global_listener()
        self._bindings.clear()