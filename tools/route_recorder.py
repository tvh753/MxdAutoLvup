# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""路线录制工具 v4（照抄参考项目，唯一区别：用游戏画面匹配 map + 玩家模板定位）

与参考项目 routeRecorder.py 的差异：
  · 参考项目：小地图 → 在 map 上匹配 → 位置
  · 本项目：  游戏画面（去 UI）→ 在 map 上匹配 → 位置
  · 参考项目：玩家位置用小地图黄点
  · 本项目：  玩家位置用玩家模板匹配

其他完全一致：固定 map 上画 route、累积坐标、F3/F4 保存。

用法：
    python tools/route_recorder.py --map 蘑菇山
    python tools/route_recorder.py --map 蘑菇山 --resume

按键：
    方向键          画线（左走/右走/爬绳）
    跳跃键 + 方向   画跳标记
    瞬移键 + 方向   画瞬移标记
    F3              保存当前路线 → routeN.png
    F4              保存 map.png
    ESC             退出
"""
import argparse
import glob
import os
import sys
import time

import cv2
import numpy as np

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from core.config_manager import ConfigManager, ROOT
from core.imio import imread_u, imwrite_u


# ---- 与 core/color_route.py 的 RAW_CODES 保持一致（BGR 顺序）----
ACTION_COLORS = {
    "left none none":      (0, 0, 255),     # 左走  红
    "right none none":     (255, 0, 0),     # 右走  蓝
    "left none jump":      (0, 127, 255),   # 左跳  橙
    "right none jump":     (255, 255, 0),   # 右跳  青
    "none down jump":      (0, 255, 127),   # 下跳  淡绿
    "none none jump":      (255, 0, 255),   # 原地跳 品红
    "none none goal":      (0, 255, 255),   # 终点  黄
    "none up teleport":    (127, 0, 255),   # 上瞬移 粉
    "none down teleport":  (255, 0, 127),   # 下瞬移 紫
    "left none teleport":  (0, 127, 0),     # 左瞬移 深绿
    "right none teleport": (19, 69, 139),   # 右瞬移 棕
    "none up none":        (127, 127, 127), # 上爬绳 灰
    "none down none":      (127, 255, 255), # 下爬绳 淡黄
}


class RouteRecorder:
    """照抄参考项目结构：固定 map 上画线"""

    BLOB_COOLDOWN = 0.7
    # ★ 匹配参数：用游戏画面中心一小块去匹配 map，避免模板太大导致太慢
    MATCH_REGION_W = 700   # 用于匹配的中央区域宽（像素）
    MATCH_REGION_H = 350   # 用于匹配的中央区域高（像素）
    LOCAL_SEARCH_RADIUS = 200  # 局部搜索半径（px）

    def __init__(self, map_name, mode="window", fps=10):
        self.map_name = map_name
        self.mode = mode
        self.fps = fps
        self._stop = False

        self._pending_save_route = False
        self._pending_save_map = False
        self._pending_exit = False
        self._last_blob_t = 0.0

        # ---- 配置 ----
        cfg_mgr = ConfigManager()
        self.cfg = cfg_mgr.cfg
        self.keys = self.cfg.get("keys", {})

        # 底部 UI 高度（用于裁掉）
        ui = self.cfg.get("patrol", {}).get("ui_y_start", None)
        if ui is None:
            # 从配置里找，没找到用经验值
            ui = 640
        self.map_area_h = int(ui)  # 游戏画面里地图部分的高度

        # ---- 玩家模板 ----
        pt = self.cfg.get("player_template")
        self.player_tpl = None
        if pt and pt.get("path") and os.path.isfile(pt["path"]):
            img = imread_u(pt["path"], cv2.IMREAD_COLOR)
            if img is not None:
                self.player_tpl = img
                print(f"[Recorder] 玩家模板已加载: {pt['path']} "
                      f"({img.shape[1]}x{img.shape[0]})")
        if self.player_tpl is None:
            print("[Recorder] ⚠ 未加载玩家模板，无法定位玩家！"
                  "请先在控制台「框选玩家模板」")

        # ---- 截图器 ----
        if mode == "vnc":
            from core.window_capture import VncCapture
            v = self.cfg.get("vnc", {})
            self.capture = VncCapture(
                host=v.get("host", "127.0.0.1"),
                port=v.get("port", 5900),
                password=v.get("password") or None)
            kw = f"{v.get('host', '127.0.0.1')}:{v.get('port', 5900)}"
            if not self.capture.bind(kw):
                raise RuntimeError(f"VNC 连接失败: {kw}")
            print(f"[Recorder] VNC 已连接: {kw}")
        else:
            from core.window_capture import WindowCapture
            self.capture = WindowCapture()
            title = self.cfg.get("window_title", "")
            if not title or not self.capture.bind(title):
                raise RuntimeError(f"窗口绑定失败: {title}")
            print(f"[Recorder] 窗口已绑定: {self.capture.window_title}  "
                  f"分辨率={self.capture.size}")

        # ---- 地图目录 ----
        self.map_dir = os.path.join(ROOT, "maps", map_name)
        os.makedirs(self.map_dir, exist_ok=True)
        self.map_path = os.path.join(self.map_dir, "map.png")

        # ---- 加载用户提供的 map.png ----
        img = imread_u(self.map_path)
        if img is None:
            raise RuntimeError(
                f"未找到 map.png: {self.map_path}\n"
                f"请先手动放入一张完整的 map.png（网图/攻略图均可）")
        self.img_map = img
        # ★ 路线图 = map 的副本，线条直接画在上面。
        #   参考项目就是这么做的：保存的 route.png 本身包含地图底图，
        #   打开就能直观看到路线的位置和走向。
        self.img_route = self.img_map.copy()
        self.img_map_gray = cv2.cvtColor(self.img_map, cv2.COLOR_BGR2GRAY)
        print(f"[Recorder] map.png 已加载: {img.shape[1]}x{img.shape[0]}")

        # ---- 状态 ----
        self._last_match_loc = None  # 上次匹配位置（用于局部搜索加速）
        self.last_player_global = None

        # ---- 键盘 ----
        self.key_press = set()
        self._key_map = self._build_key_map()
        try:
            from pynput import keyboard
        except ImportError:
            raise RuntimeError("需要 pynput: pip install pynput")
        self._kb = keyboard
        self.listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release)
        self.listener.daemon = True
        self.listener.start()
        # 打印按键映射 —— 用户可直观看到每个动作绑定了什么键
        print("[Recorder] 按键映射（游戏里按这些键就能被录制）:")
        for name, keyset in self._key_map.items():
            if not keyset:
                print(f"    ⚠ {name:10s} → 未识别（config.json 里键名错误或为空）")
            else:
                # 用逗号列出所有变体
                keys_str = ", ".join(str(k) for k in keyset)
                print(f"    ✓ {name:10s} → {keys_str}")

    # ================= 键盘 =================
    def _build_key_map(self):
        """把 config 里的按键名映射为「一组 pynput Key 对象」
        ★ 关键：pynput 里 alt / ctrl / shift 都有左/右变体，用户按下时
        监听器收到的是 `Key.alt_l` 或 `Key.alt_r`，而不是 `Key.alt`。
        `Key.alt` 只是个别名，`key == Key.alt` 永远为 False，导致
        按住 Alt 时 key_press 里收不到 "jump"。
        解决方案：一个逻辑键对应一个"集合"，按下时判断 `key in 集合`。
        """
        from pynput.keyboard import Key, KeyCode

        def to_keys(name):
            """返回一个 set（可能含多个 pynput Key 变体）；无效返回空集"""
            name = (name or "").lower().strip()
            if not name:
                return set()
            SPECIAL = {
                "space":   {Key.space},
                "alt":     {Key.alt_l, Key.alt_r, Key.alt},  # ★ 三种都收
                "alt_l":   {Key.alt_l, Key.alt},
                "alt_r":   {Key.alt_r, Key.alt},
                "ctrl":    {Key.ctrl_l, Key.ctrl_r, Key.ctrl},
                "control": {Key.ctrl_l, Key.ctrl_r, Key.ctrl},
                "shift":   {Key.shift_l, Key.shift_r, Key.shift},
                "enter":   {Key.enter},
                "return":  {Key.enter},
                "esc":     {Key.esc},
                "escape":  {Key.esc},
                "tab":     {Key.tab},
                "up":      {Key.up},
                "down":    {Key.down},
                "left":    {Key.left},
                "right":   {Key.right},
                "f1":      {Key.f1}, "f2": {Key.f2}, "f3": {Key.f3},
                "f4":      {Key.f4}, "f5": {Key.f5}, "f6": {Key.f6},
                "f7":      {Key.f7}, "f8": {Key.f8}, "f9": {Key.f9},
                "f10":     {Key.f10}, "f11": {Key.f11}, "f12": {Key.f12},
            }
            if name in SPECIAL:
                return SPECIAL[name]
            if len(name) == 1:
                return {KeyCode.from_char(name)}
            return set()

        k = self.keys
        # ★ jump 键同时监听 config 配置的键 + Space + Alt
        #   因为 Alt 是系统修饰键，pynput 在部分环境下会丢失事件；
        #   Space 作为普通键非常可靠，双重保险。
        jump_keys = (to_keys(k.get("jump", "space"))
                     | to_keys("space")
                     | to_keys("alt"))
        return {
            "left":     to_keys(k.get("move_left", "left")),
            "right":    to_keys(k.get("move_right", "right")),
            "up":       to_keys(k.get("up", "up")),
            "down":     to_keys(k.get("down", "down")),
            "jump":     jump_keys,
            "teleport": to_keys(k.get("teleport", "e")),
        }

    def _on_press(self, key):
        # ★ 用集合判断（一个逻辑键可能对应多个 pynput Key 变体）
        for name, keyset in self._key_map.items():
            if keyset and key in keyset:
                self.key_press.add(name)
                return
        try:
            Key = self._kb.Key
            if key == Key.f3:
                self._pending_save_route = True
            elif key == Key.f4:
                self._pending_save_map = True
            elif key == Key.esc:
                self._pending_exit = True
        except Exception:
            pass

    def _on_release(self, key):
        for name, keyset in self._key_map.items():
            if keyset and key in keyset:
                self.key_press.discard(name)
                return

    # ================= 玩家定位 =================
    def _detect_player_in_frame(self, frame):
        """用玩家模板在游戏画面里匹配 → 返回玩家中心坐标 (px, py) 或 None"""
        if self.player_tpl is None:
            return None
        th, tw = self.player_tpl.shape[:2]
        fh, fw = frame.shape[:2]
        if th > fh or tw > fw:
            return None
        try:
            res = cv2.matchTemplate(frame, self.player_tpl,
                                    cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val < 0.7:
                return None
            return (max_loc[0] + tw // 2, max_loc[1] + th // 2)
        except cv2.error:
            return None

    # ================= 画面在 map 上匹配 =================
    def _find_view_in_map(self, frame_map):
        """用游戏画面在 map 上多尺度边缘匹配

        返回: (loc, score, (view_x0, view_y0), scale)
            loc:        匹配位置（map 尺度上的左上角）
            score:      相似度（0~1，越高越好）
            (view_x0, view_y0): 匹配区域在游戏画面里的左上角
            scale:      游戏画面 → map 的缩放比
        """
        fh, fw = frame_map.shape[:2]
        rw = min(self.MATCH_REGION_W, fw)
        rh = min(self.MATCH_REGION_H, fh)
        x0 = (fw - rw) // 2
        y0 = (fh - rh) // 2
        view = frame_map[y0:y0 + rh, x0:x0 + rw]
        view_gray = cv2.cvtColor(view, cv2.COLOR_BGR2GRAY)
        view_edge = cv2.Canny(view_gray, 40, 120)

        map_edge = cv2.Canny(self.img_map_gray, 40, 120)
        map_h, map_w = map_edge.shape[:2]

        # 尝试多个缩放比（游戏画面 → map）
        best = None
        for scale in (0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0):
            vh = int(rh * scale)
            vw = int(rw * scale)
            if vh < 60 or vw < 60 or vh > map_h or vw > map_w:
                continue
            v_resized = cv2.resize(view_edge, (vw, vh),
                                   interpolation=cv2.INTER_AREA)
            try:
                res = cv2.matchTemplate(map_edge, v_resized,
                                        cv2.TM_CCOEFF_NORMED)
            except cv2.error:
                continue
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if best is None or max_val > best[1]:
                best = (max_loc, max_val, scale)

        if best is None or best[1] < 0.15:
            return None, 0.0, (x0, y0), 1.0
        loc, max_val, scale = best
        return loc, max_val, (x0, y0), scale

    # ================= 主循环 =================
    def _tick(self):
        if self._pending_exit:
            self._pending_exit = False
            self._stop = True
            return
        if self._pending_save_map:
            self._pending_save_map = False
            self._save_map()
        if self._pending_save_route:
            self._pending_save_route = False
            self._save_route()

        # 1. 截图
        frame = self.capture.screenshot()
        if frame is None:
            return

        # 2. 裁掉底部 UI（血条/技能栏）
        H, W = frame.shape[:2]
        ch = min(self.map_area_h, H)
        frame_map = frame[:ch, :]

        # 3. 玩家在画面内的位置
        p_in_frame = self._detect_player_in_frame(frame_map)
        if p_in_frame is None:
            self._show_debug(frame, None, None, "玩家模板未命中")
            return
        px_frame, py_frame = p_in_frame

        # 4. 画面在 map 上的位置
        loc, score, (vx0, vy0), scale = self._find_view_in_map(frame_map)
        if loc is None or score < 0.15:
            self._show_debug(frame, p_in_frame, None,
                             f"map匹配失败 score={score:.3f}")
            return

        # 5. 玩家全局坐标
        # 玩家在匹配区域内的相对位置 → 缩放到 map 尺度 → 加到匹配位置上
        px_in_view = (px_frame - vx0) * scale
        py_in_view = (py_frame - vy0) * scale
        px_global = loc[0] + px_in_view
        py_global = loc[1] + py_in_view
        player_global = (px_global, py_global)

        # 6. 画线
        self._draw_action(player_global)

        # 7. 调试
        self._show_debug(frame, p_in_frame, player_global,
                         f"map_loc={loc} score={score:.3f} scale={scale:.2f}")

    def _current_action(self):
        kp = self.key_press
        has_jump = "jump" in kp
        has_tp   = "teleport" in kp
        has_l    = "left" in kp
        has_r    = "right" in kp
        has_u    = "up" in kp
        has_d    = "down" in kp

        if has_jump:
            if has_l: return "left none jump", True
            if has_r: return "right none jump", True
            if has_d: return "none down jump", True
            return "none none jump", True
        if has_tp:
            if has_l: return "left none teleport", True
            if has_r: return "right none teleport", True
            if has_u: return "none up teleport", True
            if has_d: return "none down teleport", True
            return None, False
        if has_u: return "none up none", False
        if has_d: return "none down none", False
        if has_l: return "left none none", False
        if has_r: return "right none none", False
        return None, False

    def _draw_action(self, player_global):
        """完全照抄参考项目 routeRecorder 的画线逻辑

        · 跳跃/瞬移（is_blob）：每 BLOB_COOLDOWN 秒打一个点
        · 走路/爬绳：从前一帧位置到当前位置画线
        · 无动作：仅更新位置

        不加任何方向约束 / 中值滤波 —— 斜线是允许的
        （因为地图路面本身可能是倾斜的）。
        """
        if self.img_route is None:
            self.img_route = self.img_map.copy()

        action, is_blob = self._current_action()
        if action is None:
            self.last_player_global = player_global
            return

        color = ACTION_COLORS.get(action)
        if color is None:
            return

        px, py = int(player_global[0]), int(player_global[1])
        h, w = self.img_route.shape[:2]
        if px < 0 or py < 0 or px >= w or py >= h:
            return

        # ---- 跳跃/瞬移：每 0.7s 打一个点 ----
        if is_blob:
            now = time.time()
            if now - self._last_blob_t > self.BLOB_COOLDOWN:
                self._last_blob_t = now
                cv2.circle(self.img_route, (px, py), 3, color, -1)
            self.last_player_global = None
            return

        # ---- 走路/爬绳：从前一帧位置到当前位置画线 ----
        if self.last_player_global is None:
            self.last_player_global = player_global
            return
        lx, ly = int(self.last_player_global[0]), int(self.last_player_global[1])
        # 线宽 2（你之前要求的）
        cv2.line(self.img_route, (lx, ly), (px, py), color, 2)
        self.last_player_global = player_global

    # ================= 保存 =================
    def _save_map(self):
        if imwrite_u(self.map_path, self.img_map):
            print(f"[Recorder] ✓ 保存 map.png "
                  f"{self.img_map.shape[1]}x{self.img_map.shape[0]}")
        else:
            print(f"[Recorder] ✗ 保存 map.png 失败")

    def _save_route(self):
        if self.img_route is None or self.img_route.max() == 0:
            print("[Recorder] 当前路线为空，跳过")
            return
        idx = 1
        while True:
            path = os.path.join(self.map_dir, f"route{idx}.png")
            if not os.path.isfile(path):
                break
            idx += 1
        if imwrite_u(path, self.img_route):
            print(f"[Recorder] ✓ 保存 route{idx}.png "
                  f"{self.img_route.shape[1]}x{self.img_route.shape[0]}")
            # 重置为 map 副本（新一条路线从干净底图开始画）
            self.img_route = self.img_map.copy()
            self.last_player_global = None
        else:
            print(f"[Recorder] ✗ 保存 route{idx}.png 失败")

    # ================= 调试 =================
    def _count_routes(self):
        return len(glob.glob(os.path.join(self.map_dir, "route[0-9]*.png")))

    def _show_debug(self, frame, p_in_frame, player_global, status=""):
        dbg = frame.copy()
        # 用红线框出用于匹配的中央区域
        H, W = frame.shape[:2]
        ch = min(self.map_area_h, H)
        rw = min(self.MATCH_REGION_W, W)
        rh = min(self.MATCH_REGION_H, ch)
        vx0 = (W - rw) // 2
        vy0 = (ch - rh) // 2
        cv2.rectangle(dbg, (vx0, vy0), (vx0 + rw, vy0 + rh), (0, 200, 255), 2)
        cv2.putText(dbg, "MATCH REGION", (vx0 + 4, vy0 + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
        # 玩家位置
        if p_in_frame is not None:
            cv2.circle(dbg, p_in_frame, 5, (0, 0, 255), -1)
        # 底部状态栏
        cv2.putText(dbg, f"Keys: {' '.join(sorted(self.key_press)) or '-'}",
                    (10, ch - 60), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 255, 255), 2)
        cv2.putText(dbg, f"Saved routes: {self._count_routes()}",
                    (10, ch - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 255, 255), 2)
        if status:
            cv2.putText(dbg, status, (10, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
        cv2.imshow("Route Recorder", dbg)

        # map 视图 —— img_route 本身就含底图 + 线条，直接显示
        show = self.img_route.copy() if self.img_route is not None \
            else self.img_map.copy()
        # 玩家点在 map 上的位置
        if player_global is not None:
            px, py = int(player_global[0]), int(player_global[1])
            h, w = show.shape[:2]
            if 0 <= px < w and 0 <= py < h:
                cv2.circle(show, (px, py), 4, (0, 0, 255), -1)
        mh, mw = show.shape[:2]
        scale = min(1.0, 900.0 / max(mw, mh))
        if scale < 1.0:
            show = cv2.resize(show, (int(mw * scale), int(mh * scale)),
                              interpolation=cv2.INTER_NEAREST)
        cv2.imshow("Map", show)
        cv2.waitKey(1)

    # ================= 生命周期 =================
    def run(self):
        target_dt = 1.0 / max(1, self.fps)
        print(f"[Recorder] 开始录制 → {self.map_dir}")
        print("[Recorder] F3=保存路线  F4=保存地图  ESC=退出")
        try:
            while not self._stop:
                t0 = time.time()
                self._tick()
                dt = time.time() - t0
                if dt < target_dt:
                    time.sleep(target_dt - dt)
        except KeyboardInterrupt:
            print("\n[Recorder] Ctrl+C 退出")
        finally:
            self._cleanup()

    def _cleanup(self):
        if self.img_route is not None and self.img_route.max() > 0:
            print("[Recorder] 自动保存未完成的路线…")
            self._save_route()
        try:
            self.capture.close()
        except Exception:
            pass
        try:
            self.listener.stop()
        except Exception:
            pass
        cv2.destroyAllWindows()
        print("[Recorder] 退出")


def main():
    parser = argparse.ArgumentParser(description="枫叶挂机 · 路线录制工具")
    parser.add_argument("--map", required=True, help="地图名")
    parser.add_argument("--mode", choices=["window", "vnc"], default="window")
    parser.add_argument("--fps", type=int, default=10)
    args = parser.parse_args()

    print("=" * 60)
    print("  枫叶挂机 · 路线录制工具 v4")
    print("=" * 60)

    try:
        rec = RouteRecorder(args.map, args.mode, args.fps)
    except Exception as e:
        print(f"[Recorder] 初始化失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    rec.run()


if __name__ == "__main__":
    main()