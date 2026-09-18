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
import queue
import random
import time
import pydirectinput
import threading
from core.window_capture import press_key_down, press_key_up  # 见第 4 步

pydirectinput.PAUSE = 0.02      # 两次按键之间最小间隔（秒），防止过快丢键
pydirectinput.FAILSAFE = False  # 关闭鼠标移到屏幕角自动终止的安全机制（后台运行用）


class ActionController:
    """单击 / 组合键控制器（v3：完全非阻塞）

    ============================================================
    【v2 → v3 的改动】
    ============================================================
    v2 的问题：
        tap() 里用 time.sleep(hold) 阻塞键盘线程
        → hold=80ms 期间键盘线程完全睡着
        → 主循环想转向/重按方向键都要等下一个 tick
        → 主观感受："按键不跟手"

    v3 的方案：把"按下"和"释放"拆成两次 tick
        · tick N   : press_key_down(key)，记录 release_at = now + hold
        · tick N+1 : 检查 now >= release_at → press_key_up(key)
        键盘线程不睡眠，每 33ms（30 FPS）都能响应新指令。

    ============================================================
    【状态机字段】
    ============================================================
    _active_key : 当前正被按下的键名（None 表示没按）
    _release_at : 该键应该被释放的时间戳
    _combo_seq  : combo 的步骤序列 [(动作, 键名, 延迟到下一步), ...]
    _combo_idx  : 当前执行到第几步
    _combo_t    : 下一步执行的时间戳
    """

    def __init__(self):
        self._cd = {}                        # 冷却池 {名字: 上次触发时间}
        self._queue = queue.Queue(maxsize=64)  # 待发动作队列
        # ---- 非阻塞 tap 状态 ----
        self._active_key = None              # 当前按下的键（None = 没按）
        self._release_at = 0.0               # 释放时间戳
        # ---- combo 状态机 ----
        self._combo_seq = None               # 步骤列表
        self._combo_idx = 0                  # 当前步骤索引
        self._combo_t = 0.0                  # 下一步的时间戳

    def cooldown_ok(self, name, seconds):
        """冷却检查（主循环侧调用，保持原语义）

        参数：
            name    : 冷却池的名字（"atk" / "hp" / "buff1" 等）
            seconds : 冷却时长（秒）
        返回：
            True  → 冷却已过，同时刷新冷却时间戳
            False → 冷却未到
        """
        now = time.time()
        if now - self._cd.get(name, 0) >= seconds:
            self._cd[name] = now
            return True
        return False

    def tap(self, key, hold=None):
        """入队一次"按下 + 稍后释放"的按键

        参数：
            key  : 键名（"a" / "space" / "1" 等）
            hold : 按住时长（秒）；None 则随机 0.03~0.08
        """
        if not key:
            return
        if hold is None:
            hold = random.uniform(0.03, 0.08)
        try:
            self._queue.put_nowait(("tap", key, hold))
        except queue.Full:
            print(f"[tap] 队列满，丢弃 {key!r}")

    def combo(self, k1, k2, hold=None):
        """入队一次组合键（k1 先下、k2 后下、k2 先抬、k1 后抬）

        参数：
            k1, k2 : 键名（任一为空则跳过对应步骤）
            hold   : k2 按住时长；None 则随机 0.20~0.30
        """
        if not k1 and not k2:
            return
        try:
            self._queue.put_nowait(("combo", k1, k2, hold))
        except queue.Full:
            pass

    # ---------------- 键盘线程调用 ----------------
    def tick(self):
        """键盘线程每 1/KB_FPS 秒调用一次

        ============================================================
        【执行顺序（重要）】
        ============================================================
        ① 释放到期的 tap 按键（不睡眠，只查时间戳）
        ② 推进 combo 状态机（若正在进行）
        ③ 若 tap 键还在按住 → 本 tick 不处理新动作（防按键叠加）
        ④ 从队列取一个动作执行

        ============================================================
        【为什么 ③ 要 return】
        ============================================================
        假设当前按住 "a"（release_at = now + 60ms）。
        如果此时直接消费队列里的"按下 left"：
          · 会同时按住 "a" 和 "left"
          · 但游戏可能不接受这种并行按键
          · 或者导致方向键没生效
        所以"正在按键时"不处理新动作，等它释放完再说。

        ============================================================
        """
        now = time.time()

        # ============================================
        # ① 释放到期的 tap 按键
        # ============================================
        if self._active_key is not None and now >= self._release_at:
            try:
                press_key_up(self._active_key)
            except Exception:
                pass
            self._active_key = None

        # ============================================
        # ② 推进 combo 状态机
        # ============================================
        if self._combo_seq is not None:
            # 到时间点就执行当前步骤
            if now >= self._combo_t:
                kind, k, delay_next = self._combo_seq[self._combo_idx]
                try:
                    if kind == "down":
                        press_key_down(k)
                    else:
                        press_key_up(k)
                except Exception:
                    pass

                # 推进到下一步
                self._combo_idx += 1
                if self._combo_idx >= len(self._combo_seq):
                    # combo 完成
                    self._combo_seq = None
                    self._combo_idx = 0
                else:
                    # 安排下一步的时间
                    self._combo_t = now + delay_next
            # combo 期间不处理新动作
            return

        # ============================================
        # ③ tap 键还在按住 → 本 tick 不处理新动作
        # ============================================
        if self._active_key is not None:
            return

        # ============================================
        # ④ 消费队列里的一个动作
        # ============================================
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return

        action = item[0]
        try:
            if action == "tap":
                _, key, hold = item
                if not key:
                    return
                if hold is None:
                    hold = random.uniform(0.03, 0.08)
                press_key_down(key)
                # 记录"何时该释放"，下一 tick 释放（不睡眠）
                self._active_key = key
                self._release_at = now + hold

            elif action == "combo":
                _, k1, k2, hold = item
                if hold is None:
                    hold = random.uniform(0.20, 0.30)
                pre = random.uniform(0.015, 0.035)  # k1 下 → k2 下的间隔
                gap = random.uniform(0.015, 0.035)  # k2 抬 → k1 抬的间隔

                # 构造步骤序列：
                #   (动作, 键名, 执行后延迟到下一步)
                steps = []
                if k1:
                    # k1 按下 → 等 pre（有 k2）或 hold（无 k2）
                    steps.append(("down", k1, pre if k2 else hold))
                if k2:
                    # k2 按下 → 等 hold
                    steps.append(("down", k2, hold))
                    # k2 抬起 → 等 gap
                    steps.append(("up", k2, gap))
                if k1:
                    # k1 抬起（最后一步，delay 无意义）
                    steps.append(("up", k1, 0.0))

                self._combo_seq = steps
                self._combo_idx = 0
                self._combo_t = now  # 立即执行第一步

        except Exception as e:
            import traceback
            print(f"[tick] 异常: {e}\n{traceback.format_exc()}")

    def release_all(self):
        """立即释放所有按键（停机/失焦时调用）

        ★ 注意：要清掉非阻塞状态，否则下次 tick 会重复释放一个不存在的键
        """
        # 清空队列
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        # 释放正在按下的键
        if self._active_key is not None:
            try:
                press_key_up(self._active_key)
            except Exception:
                pass
            self._active_key = None
        self._release_at = 0.0
        # 清空 combo 状态
        self._combo_seq = None
        self._combo_idx = 0
        self._combo_t = 0.0


class MovementController:
    """方向 / 爬绳控制器（v2：异步下发）

    与 v1 的区别：
      · set_dir / set_climb 只更新"目标状态"，不立即发键（非阻塞）
      · 独立线程按固定 FPS 调 tick()，只在目标变化时才真正发键
      · 与参考项目 MapStoryAutoLevelUp 的 KeyBoardController 思路一致
    """

    def __init__(self):
        self.cfg = {}
        # 目标状态（由主循环 set_dir/set_climb 更新）
        self._target_dir = 0        # -1/0/+1
        self._target_climb = None   # "up"/"down"/None
        # 实际按下的键（由 tick 维护）
        self._cur_dir_key = None
        self._cur_climb_key = None
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
        """立即释放所有按键（停机/失焦时调用，不能被 tick 拖延）"""
        with self._lock:
            self._target_dir = 0
            self._target_climb = None
        self._do_tick(force_release=True)

    # ---- 键盘线程调用：按 FPS 检查变化并下发 ----
    def tick(self):
        """由键盘线程每 1/FPS 秒调用一次"""
        self._do_tick()

    def _do_tick(self, force_release=False):
        with self._lock:
            want_dir = 0 if force_release else self._target_dir
            want_climb = None if force_release else self._target_climb

        # 解析目标键名
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

        # 方向键下发
        if want_dir_key != self._cur_dir_key:
            try:
                if self._cur_dir_key:
                    press_key_up(self._cur_dir_key)
                if want_dir_key:
                    press_key_down(want_dir_key)
            except Exception:
                pass
            self._cur_dir_key = want_dir_key

        # 爬绳键下发
        if want_climb_key != self._cur_climb_key:
            try:
                if self._cur_climb_key:
                    press_key_up(self._cur_climb_key)
                if want_climb_key:
                    press_key_down(want_climb_key)
            except Exception:
                pass
            self._cur_climb_key = want_climb_key

    def current_dir_key(self):
        """返回当前实际按下的方向键名（供决策层查询）

        【为什么需要它】
        主线程调 set_dir(lr) 只是更新"目标方向"，实际按键由键盘线程
        在下一次 tick 才下发（30 FPS → 33ms 一次）。所以在攻击前，
        主线程需要知道"方向键到底按下了没有"，避免出现
        "方向还没切换就先攻击" → 打空的情况。

        【返回值】
        当前实际按下的键名（"left" / "right" / None），
        可直接与 _k("move_left") / _k("move_right") 对比。
        """
        return self._cur_dir_key

    def repress_dir(self, direction):
        """强制重按方向键：先释放当前方向键，再设置新目标方向

        【为什么需要这个方法】
        冒险岛角色的朝向由"最后按下的方向键"决定，而不是"当前按住的方向键"。
        这意味着：
          · 如果脚本一直按住左键，角色朝向应该是左
          · 但如果角色因受伤/攻击动画/被击退而改变了朝向，
            继续"按住左键"不会让角色重新朝左 → 必须"释放再按"一次

        【实现方式】
        ① 清空 _cur_dir_key，让下一次 tick 认为"当前没有方向键"
        ② 在锁外立即 release 旧键（避免 press_key_up 卡锁）
        ③ 设置新目标方向
        ④ 键盘线程下一次 tick 时，会执行：
              want_dir_key != _cur_dir_key(None) → 按下新方向键

        【线程安全】
        · _cur_dir_key / _target_dir 的修改在 _lock 内
        · press_key_up 在锁外执行（可能阻塞几毫秒，避免占锁）
        """
        with self._lock:
            cur = self._cur_dir_key       # 记录旧键
            self._cur_dir_key = None      # 清空状态，让 tick 强制重按
            self._target_dir = direction or 0

        # 锁外释放旧键（press_key_up 可能阻塞，不占锁）
        if cur:
            try:
                press_key_up(cur)
            except Exception:
                pass