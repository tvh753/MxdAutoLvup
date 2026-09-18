# -*- coding: utf-8 -*-
"""
游戏全景地图拼接工具
流程: 窗口抓屏 -> 相邻帧模板匹配求位移 -> 累加坐标 -> 羽化融合输出大图
"""
import argparse
import time
import sys

import cv2
import numpy as np
import mss
import win32gui



# ============================================================
# 1. 窗口抓取
# ============================================================
class WindowGrabber:
    def __init__(self, title_substr: str):
        self.hwnd = self._find_window(title_substr)
        self.sct = mss.mss()
        print(f"[+] 绑定窗口: hwnd={self.hwnd}  title={win32gui.GetWindowText(self.hwnd)!r}")

    @staticmethod
    def _find_window(substr: str) -> int:
        hits = []

        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd)
                # 排除命令行/编辑器窗口，防止匹配到脚本自身运行的窗口
                if not t or "cmd.exe" in t or "python" in t or "powershell" in t or "管理员:" in t:
                    return True
                if substr in t:
                    hits.append((hwnd, t))
            return True

        win32gui.EnumWindows(cb, None)
        if not hits:
            raise RuntimeError(f'未找到标题包含 "{substr}" 的可见窗口')

        # 如果还有多个，可以打印出来让你手动确认
        for hwnd, t in hits:
            print(f"    候选: hwnd={hwnd}  title={t!r}")

        return hits[0][0]

    def client_rect(self) -> dict:
        """客户区（不含标题栏/边框）在屏幕上的坐标"""
        l, t, r, b = win32gui.GetClientRect(self.hwnd)
        sl, st = win32gui.ClientToScreen(self.hwnd, (0, 0))
        return {"left": sl, "top": st, "width": r - l, "height": b - t}

    def grab(self) -> np.ndarray:
        """返回 BGR 图像"""
        shot = np.asarray(self.sct.grab(self.client_rect()))  # BGRA
        return shot[..., :3].copy()


# ============================================================
# 2. 相邻帧位移估计（模板匹配）
# ============================================================
def estimate_shift(prev_gray, curr_gray,
                   template_size: int = 320,
                   max_shift: int = 400,
                   scale: float = 0.5):
    """
    估计从 prev 到 curr 相机的位移 (dx, dy)。
    返回 (dx, dy, score)，score<0.4 基本不可信。

    原理: 在 prev 中心取一块 template，在 curr 中心附近搜索它的新位置。
          dx = 旧位置 - 新位置（相机往右走，画面往左移）
    """
    h, w = prev_gray.shape

    # 降采样加速（1/2 分辨率下匹配，再把位移乘回去）
    if scale != 1.0:
        ph, pw = int(h * scale), int(w * scale)
        p = cv2.resize(prev_gray, (pw, ph), interpolation=cv2.INTER_AREA)
        c = cv2.resize(curr_gray, (pw, ph), interpolation=cv2.INTER_AREA)
    else:
        p, c = prev_gray, curr_gray
        ph, pw = h, w

    ts = min(template_size, ph // 2, pw // 2)
    cy, cx = ph // 2, pw // 2
    y0, x0 = cy - ts // 2, cx - ts // 2
    tpl = p[y0:y0 + ts, x0:x0 + ts]

    # 只在中心附近搜索（限制范围，避免匹配到重复纹理或 UI）
    ms = int(max_shift * scale)
    sy0, sy1 = max(0, y0 - ms), min(ph, y0 + ts + ms)
    sx0, sx1 = max(0, x0 - ms), min(pw, x0 + ts + ms)
    search = c[sy0:sy1, sx0:sx1]

    if search.shape[0] < ts or search.shape[1] < ts:
        return 0.0, 0.0, 0.0

    res = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, maxloc = cv2.minMaxLoc(res)

    mx = sx0 + maxloc[0]
    my = sy0 + maxloc[1]

    dx = (x0 - mx) / scale
    dy = (y0 - my) / scale
    return float(dx), float(dy), float(maxv)


# ============================================================
# 3. 画布融合
# ============================================================
class Stitcher:
    def __init__(self, canvas_w=4096, canvas_h=4096, feather=True):
        self.cw, self.ch = canvas_w, canvas_h
        self.cx, self.cy = canvas_w // 2, canvas_h // 2
        # float32: 累加时避免溢出
        self.canvas = np.zeros((canvas_h, canvas_w, 3), np.float32)
        self.weight = np.zeros((canvas_h, canvas_w), np.float32)
        self.feather = feather
        self.min_x, self.min_y = canvas_w, canvas_h
        self.max_x, self.max_y = 0, 0

    def _kernel(self, h, w):
        # 原有的汉宁窗
        wy = np.hanning(max(h, 2)).astype(np.float32)
        wx = np.hanning(max(w, 2)).astype(np.float32)
        k = np.outer(wy, wx)

        # 新增：把画面中心 30% 的区域权重设为极小，用来抹除角色残影
        cy, cx = h // 2, w // 2
        hole_h, hole_w = int(h * 0.3), int(w * 0.3)
        k[cy - hole_h // 2: cy + hole_h // 2, cx - hole_w // 2: cx + hole_w // 2] *= 0.01

        return np.clip(k, 1e-3, None)

    def add(self, img: np.ndarray, ox: int, oy: int):
        """img 左上角放在画布 (cx+ox, cy+oy)"""
        h, w = img.shape[:2]
        x, y = self.cx + ox, self.cy + oy

        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.cw, x + w), min(self.ch, y + h)
        if x0 >= x1 or y0 >= y1:
            return

        sx0, sy0 = x0 - x, y0 - y
        sx1, sy1 = sx0 + (x1 - x0), sy0 + (y1 - y0)

        patch = img[sy0:sy1, sx0:sx1].astype(np.float32)
        k = self._kernel(patch.shape[0], patch.shape[1])

        self.canvas[y0:y1, x0:x1] += patch * k[..., None]
        self.weight[y0:y1, x0:x1] += k

        self.min_x, self.min_y = min(self.min_x, x), min(self.min_y, y)
        self.max_x, self.max_y = max(self.max_x, x + w), max(self.max_y, y + h)

    def result(self) -> np.ndarray:
        out = self.canvas / np.maximum(self.weight, 1e-6)[..., None]
        out = np.clip(out, 0, 255).astype(np.uint8)
        x0, y0 = max(0, self.min_x), max(0, self.min_y)
        x1, y1 = min(self.cw, self.max_x), min(self.ch, self.max_y)
        return out[y0:y1, x0:x1]


# ============================================================
# 4. 主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True, help="游戏窗口标题关键字（部分匹配）")
    ap.add_argument("--interval", type=float, default=0.4, help="截屏间隔（秒）")
    ap.add_argument("--out", default="map.png", help="输出文件")
    ap.add_argument("--crop", type=int, nargs=4, default=[0, 0, 0, 0],
                    metavar=("TOP", "BOTTOM", "LEFT", "RIGHT"),
                    help="裁掉 HUD 的像素数，例如 --crop 60 80 0 0")
    ap.add_argument("--min-score", type=float, default=0.5, help="匹配分数阈值")
    ap.add_argument("--canvas", type=int, default=4096, help="画布边长（越大越吃内存）")
    ap.add_argument("--preview", action="store_true", help="只截一张看看效果，然后退出")
    args = ap.parse_args()

    g = WindowGrabber(args.title)

    ct, cb, cl, cr = args.crop
    def crop(img):
        h, w = img.shape[:2]
        return img[ct:max(ct + 1, h - cb), cl:max(cl + 1, w - cr)]

    if args.preview:
        img = crop(g.grab())
        cv2.imwrite("preview.png", img)
        print(f"[+] 已保存 preview.png 尺寸={img.shape[1]}x{img.shape[0]}，"
              f"请查看 HUD 位置后用 --crop TOP BOTTOM LEFT RIGHT 裁掉")
        return

    st = Stitcher(args.canvas, args.canvas)
    print("[*] 3 秒后开始采集，请切到游戏窗口并开始移动角色")
    time.sleep(3)

    prev_gray = None
    ox, oy = 0.0, 0.0
    n = 0

    try:
        while True:
            bgr = crop(g.grab())
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

            if prev_gray is None:
                st.add(bgr, 0, 0)
                print(f"[帧 {n}] 起始帧")
            else:
                dx, dy, score = estimate_shift(prev_gray, gray)
                if score < args.min_score:
                    print(f"[帧 {n}] 跳过 (score={score:.2f}) — 重新锚定")
                    prev_gray = gray
                    time.sleep(args.interval)
                    continue
                ox += dx
                oy += dy
                st.add(bgr, int(round(ox)), int(round(oy)))
                print(f"[帧 {n}] d=({dx:+6.1f},{dy:+6.1f})  score={score:.2f}  "
                      f"累计=({ox:+7.1f},{oy:+7.1f})")

            prev_gray = gray
            n += 1
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n[*] 采集结束（共 {n} 帧），正在合成...")

    out = st.result()
    cv2.imwrite(args.out, out)
    print(f"[+] 已保存 {args.out}  尺寸={out.shape[1]}x{out.shape[0]}")


if __name__ == "__main__":
    main()