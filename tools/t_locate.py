# -*- coding: utf-8 -*-
"""绕过小地图：玩家模板 → 裁小块 → 在 map 上匹配"""

import cv2
import numpy as np
import os

# ============ 参数 ============
GAME_SHOT  = r"D:\test_demo\MxdAutoLvup\test_game.png"
MAP_IMG    = r"D:\test_demo\MxdAutoLvup\maps\火焰之地Ⅴ\map.png"
PLAYER_TPL = r"D:\test_demo\MxdAutoLvup\templates\player\player.png"   # ★ 玩家模板

# 玩家模板匹配的最小分数
PLAYER_TPL_THRESH = 0.40

# 从玩家周围裁多大一块（地形为主，避开天空和 UI）
CROP_W, CROP_H = 300, 300
CROP_UP   = 100   # 玩家上方 100px
CROP_DOWN = 100   # 玩家下方 200px
# ==============================


def imread_u(path):
    if not path or not os.path.isfile(path):
        return None
    return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)


def imwrite_u(path, img):
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if ok:
        buf.tofile(path)


def find_player_in_frame(frame, tpl):
    """名字条模板定位玩家（名字条在角色脚下）

    ★ 关键：
      1. 搜索区域限制到上半 2/3，避免匹配到底部 UI 上相似的文字
      2. 名字条在角色脚下，玩家中心 = 名字条中心 - 30px（向上）
      3. 阈值放宽到 0.4，名字条有描边/阴影，分数上不了 0.6
    """
    if tpl is None:
        return None, 0.0
    th, tw = tpl.shape[:2]
    H, W = frame.shape[:2]
    if th > H or tw > W:
        print(f"[错误] 名字条模板({tw}x{th}) 比画面({W}x{H})还大")
        return None, 0.0

    # 搜索区域：上半 2/3（去掉底部 UI），左上角小地图涂黑
    SEARCH_BOTTOM = int(H * 0.66)
    search = frame[0:SEARCH_BOTTOM, :].copy()
    search[0:150, 0:200] = 0

    f_gray = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
    t_gray = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)

    res = cv2.matchTemplate(f_gray, t_gray, cv2.TM_CCOEFF_NORMED)
    _, mv, _, ml = cv2.minMaxLoc(res)

    name_x, name_y = ml
    cx = name_x + tw // 2
    cy = name_y + th // 2

    # ★ 名字条在角色脚下 → 玩家中心 = 名字条中心 - 30px（向上）
    OFFSET_Y = -30
    player_x = cx
    player_y = cy + OFFSET_Y

    # 诊断可视化
    dbg = frame.copy()
    cv2.rectangle(dbg, (name_x, name_y),
                  (name_x + tw, name_y + th), (255, 0, 0), 2)
    cv2.putText(dbg, f"nametag score={mv:.3f}",
                (name_x, max(20, name_y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)
    cv2.drawMarker(dbg, (player_x, player_y), (0, 0, 255),
                   cv2.MARKER_CROSS, 30, 3)
    cv2.putText(dbg, f"Player ({player_x},{player_y})",
                (player_x + 14, player_y - 14),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    cv2.line(dbg, (0, SEARCH_BOTTOM), (W, SEARCH_BOTTOM),
             (0, 255, 255), 2)
    cv2.putText(dbg, f"search limit y={SEARCH_BOTTOM}",
                (10, SEARCH_BOTTOM - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    try:
        dbg[10:10 + th, W - tw - 10:W - 10] = tpl
        cv2.rectangle(dbg, (W - tw - 10, 10),
                      (W - 10, 10 + th), (0, 255, 0), 2)
    except Exception:
        pass
    imwrite_u("dbg_player_match.png", dbg)

    return (player_x, player_y), mv


def main():
    print("=" * 60)
    print("  玩家模板定位 + 小块地形匹配 map")
    print("=" * 60)

    game = imread_u(GAME_SHOT)
    mp = imread_u(MAP_IMG)
    tpl = imread_u(PLAYER_TPL)

    if game is None:
        print(f"[错误] 游戏截图不存在: {GAME_SHOT}")
        return
    if mp is None:
        print(f"[错误] map 不存在: {MAP_IMG}")
        return
    if tpl is None:
        print(f"[错误] 玩家模板不存在: {PLAYER_TPL}")
        return

    H, W = game.shape[:2]
    print(f"[1] 游戏截图: {W}x{H}")
    print(f"[2] map: {mp.shape[1]}x{mp.shape[0]}")
    print(f"[3] 玩家模板: {tpl.shape[1]}x{tpl.shape[0]}")

    # ---- ① 玩家模板定位 ----
    # ---- ① 玩家模板定位 ----
    p_in_frame, score = find_player_in_frame(game, tpl)
    if p_in_frame is None or score < PLAYER_TPL_THRESH:
        print(f"\n[失败] 名字条模板未命中 (score={score:.3f} < {PLAYER_TPL_THRESH})")
        imwrite_u("dbg_frame.png", game)
        imwrite_u("dbg_tpl.png", tpl)
        return
    PLAYER_X, PLAYER_Y = p_in_frame
    print(f"[4] 玩家在画面内: ({PLAYER_X},{PLAYER_Y})  "
          f"名字条得分={score:.3f}")

    # ---- ② 裁玩家附近小块 ----
    x0 = max(0, PLAYER_X - CROP_W // 2)
    y0 = max(0, PLAYER_Y - CROP_UP)
    x1 = min(W, x0 + CROP_W)
    y1 = min(H, y0 + CROP_H)
    patch = game[y0:y1, x0:x1].copy()
    ph, pw = patch.shape[:2]
    print(f"[5] 裁剪块: {pw}x{ph}  位置=({x0},{y0})-({x1},{y1})")
    imwrite_u("dbg_patch.png", patch)

    px_in_patch = PLAYER_X - x0
    py_in_patch = PLAYER_Y - y0
    print(f"    玩家在块内: ({px_in_patch},{py_in_patch})")

    # ---- ③ 边缘化 ----
    p_edge = cv2.Canny(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), 50, 150)
    m_edge = cv2.Canny(cv2.cvtColor(mp, cv2.COLOR_BGR2GRAY), 50, 150)

    # ---- ④ 多尺度匹配 ----
    mh, mw = m_edge.shape
    best = None
    print("\n[匹配明细]")
    for s in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]:
        w = int(pw * s)
        h = int(ph * s)
        if w < 40 or h < 40 or w > mw or h > mh:
            continue
        p_resized = cv2.resize(p_edge, (w, h), interpolation=cv2.INTER_AREA)
        if int((p_resized > 0).sum()) < 200:
            continue
        res = cv2.matchTemplate(m_edge, p_resized, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(res)
        print(f"  scale={s:.2f}  size={w}x{h}  score={mv:.3f}  loc={ml}")
        if best is None or mv > best[1]:
            best = (ml, mv, s, w, h)

    if best is None:
        print("所有 scale 失败")
        return

    (mx, my), pscore, s, w, h = best
    print(f"\n[6] 最佳匹配: score={pscore:.3f} scale={s:.2f} loc=({mx},{my})")

    # ---- ⑤ 玩家 map 坐标 ----
    px_map = int(mx + px_in_patch * s)
    py_map = int(my + py_in_patch * s)
    print(f"[7] 玩家 map 坐标: ({px_map},{py_map})")

    # ---- ⑥ 在 map 上画出所有标记 ----
    out = mp.copy()

    # 匹配框（绿）—— 表示"游戏画面小块在 map 上覆盖的区域"
    cv2.rectangle(out, (mx, my), (mx + w, my + h), (0, 255, 0), 2)
    cv2.putText(out, f"patch score={pscore:.2f} scale={s:.2f}",
                (mx + 5, my - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    # 玩家点（黄）
    cv2.circle(out, (px_map, py_map), 12, (0, 0, 0), -1)
    cv2.circle(out, (px_map, py_map), 10, (0, 255, 255), -1)
    cv2.drawMarker(out, (px_map, py_map), (0, 0, 0),
                   cv2.MARKER_CROSS, 20, 2)
    cv2.putText(out, f"Player ({px_map},{py_map})",
                (px_map + 14, py_map - 14),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    # NAV 面板窗口（红）—— 项目里 NAV 面板就用这个范围
    nax0, nay0 = px_map - 50, py_map - 80
    nax1, nay1 = px_map + 50, py_map + 20
    cv2.rectangle(out, (nax0, nay0), (nax1, nay1), (0, 0, 255), 3)

    imwrite_u("locate_result3.png", out)
    print("\n已保存:")
    print("  dbg_patch.png      ← 从游戏里裁的 500x300 小块")
    print("  locate_result3.png ← map 上定位结果")

    # ---- 同时输出 NAV 面板视图（看效果）----
    if 0 <= nay0 and 0 <= nax0 and nay1 <= out.shape[0] and nax1 <= out.shape[1]:
        nav = out[nay0:nay1, nax0:nax1].copy()
        # 放大 4 倍便于观察
        nav_big = cv2.resize(nav, (nav.shape[1] * 4, nav.shape[0] * 4),
                             interpolation=cv2.INTER_NEAREST)
        imwrite_u("locate_nav_panel.png", nav_big)
        print("  locate_nav_panel.png ← NAV 面板显示内容（放大 4 倍）")


if __name__ == "__main__":
    main()