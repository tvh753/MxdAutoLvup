# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:59
# @Author  : yy
# @File    : config_manager.py
# @Software: MxdAutoLvup

"""配置管理模块：config.json 的读取 / 写入 / 版本迁移

职责：
  1. 维护项目默认配置 DEFAULT_CONFIG（键值结构即“配置的数据库结构”）；
  2. ConfigManager 启动时加载磁盘上的 config.json，用 deep_merge
     与默认值合并（缺项补默认，多出/改动项保留用户值）；
  3. 玩家模板路径体系：全局唯一存放 templates/player/player.png，
     兼容历史版本的各种写法（旧包内路径 / 相对路径 / 目录形式），
     读取时自动迁移清洗并落盘。
"""
import json, os, copy, shutil
import sys

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：以 exe 所在目录为根，保证 config.json / maps / templates 与 exe 同级可写
    ROOT = os.path.dirname(sys.executable)
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, "templates")

# ================= 玩家模板全局路径体系 =================
# 玩家模板全局只有一份，跨地图共用，避免每个地图包重复存一份
PLAYER_TPL_PATH = os.path.join(TEMPLATE_DIR, "player", "player.png")


def resolve_player_path(path):
    """玩家模板路径归一化：
      · 相对路径 → 项目根下的绝对路径（"templates/player" → ROOT/templates/player）
      · 目录 → 自动补 player.png
    返回有效的绝对路径；无效返回 None。"""
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.normpath(os.path.join(ROOT, path))
    if os.path.isdir(p):  # 手写的目录形式
        p = os.path.join(p, "player.png")
    return p if os.path.isfile(p) else None


def migrate_player_template(cfg):
    """清洗 config 中的 player_template（历史形态收敛到全局路径）。
    处理：旧包内路径 / 相对路径 / 目录 / 已丢失路径 / 缺失但有全局文件。
    返回 True 表示 cfg 被修改（调用方应保存）。"""
    pt = cfg.get("player_template")
    if not pt:
        if os.path.isfile(PLAYER_TPL_PATH):  # 无 entry 但全局存在 → 恢复引用
            cfg["player_template"] = {"name": "玩家", "path": PLAYER_TPL_PATH}
            return True
        return False
    resolved = resolve_player_path(pt.get("path", ""))
    if resolved and os.path.normcase(resolved) == os.path.normcase(PLAYER_TPL_PATH):
        return False  # 已是全局路径，OK
    if resolved:  # 有效但非全局（如旧包内残留文件）→ 迁移
        try:
            os.makedirs(os.path.dirname(PLAYER_TPL_PATH), exist_ok=True)
            if not os.path.isfile(PLAYER_TPL_PATH):
                shutil.copy2(resolved, PLAYER_TPL_PATH)
            cfg["player_template"] = {"name": "玩家", "path": PLAYER_TPL_PATH}
            return True
        except OSError:
            pass
    # 当前路径无效 → 回退全局；全局也没有 → 置空停用
    if os.path.isfile(PLAYER_TPL_PATH):
        cfg["player_template"] = {"name": "玩家", "path": PLAYER_TPL_PATH}
        return True
    cfg["player_template"] = None
    return True

DEFAULT_CONFIG = {
    # ============ 游戏窗口 ============
    "window_title": "冒险岛怀旧服",          # 绑定的游戏窗口标题（模糊匹配）
    # ============ 目标识别 ============
    "monster_templates": [],              # [{"name": "蓝蘑菇", "path": "templates/xx.png"}]
    "player_template": {"name": "player", "path": "templates/player"},              # {"name": "玩家", "path": "..."}
    "detect_region": None,                # [x, y, w, h] 缩小搜索范围提速
    # ============ 状态条区域（GUI 框选校准后写入） ============
    "hp_bar": {"x": 0, "y": 0, "w": 0, "h": 0},  # 红 · 状态栏底部
    "mp_bar": {"x": 0, "y": 0, "w": 0, "h": 0},  # 蓝
    "exp_bar": {"x": 0, "y": 0, "w": 0, "h": 0},  # 黄 · 经验条（仅监控显示）
    # ============ 按键映射（pydirectinput 键名） ============
    "keys": {
        "attack": "q", "skill1": "q", "skill2": "q", "skill3": "q",
        "hp_potion": "1", "mp_potion": "2",
        "move_left": "left", "move_right": "right", "jump": "alt",
        "up": "up", "down": "down",          # ← 新增：抓绳/爬绳
        "teleport": "shift",              # ← 新增：法师瞬移
        "pickup": "z",  # ← 新增：拾取
    },
    # ============ 识别与策略阈值 ============
    "thresholds": {
        "match": 0.80, "hp_potion": 55, "mp_potion": 35, "hp_stop": 12,
        "attack_range": 160, "potion_cooldown": 1.2, "roam_interval": 2.5,
        "skill_range": 260,
        "chase_range": 220,        # 追击距离：怪物屏幕像素距离上限（巡逻时）
        "off_route_tol": 30,       # 偏离容差：小地图px，超出即放弃追击回归路线
        "pickup_interval": 0.3,    # 拾取间隔：移动中边走边捡（原 0.9）
    },
    # ============ 行为开关 ============
    "options": {
        "use_skill_rotation": True, "jump_while_roam": True,
        "stop_on_low_hp": True, "pause_on_unfocus": True,
        "loot_enabled": True
    },
    # ============ 巡逻 / 地图包 ============
    "patrol": {
        "enabled": False,
        "minimap": {"x": 0, "y": 0, "w": 0, "h": 0},  # 小地图在画面中的区域
        "player_dot_color": [0, 128, 255],  # BGR: 目标HSV H:25-35, S:40-255, V:120-255
        "dot_tolerance": 80,                # 玩家点颜色容差
        "search_range": 10,  # 玩家周围搜索半径（小地图px）
        "grab_tol": 4,  # 抓绳水平容差
        "route_path": "",  # 颜色路线图路径（地图包内）
        "current_map": "",  # 当前激活地图包名
        "dot_max_area": 40,         # 玩家点面积上限（点检测过滤）
        # 绿底图模板匹配（Lv0主通道，方案C）：用小地图ROI在录制底图上做模板匹配
        "green_match": {
            "enabled": True,         # 是否启用绿底图匹配（默认开，可关）
            "tpl_ratio": 0.55,       # 匹配模板占ROI中心区域比例（0.3~0.8）
            "min_conf": 0.58,        # 匹配置信度下限（0.55~0.65）
            "use_offset": False,     # 是否自定义玩家在小地图中的偏移
            "offset": [0, 0],        # 自定义偏移(ox,oy)，不启用则用ROI中心
        },
    },
    # ============ 定时休息调度 ============
    "schedule": {
        "enabled": False, "duration_min": 60,          # 挂机时长（分钟，±3分钟随机）
        "rest_lo_min": 5, "rest_hi_min": 10,           # 休息时长范围（分钟）
        "safe_stop_wait": 120,                          # 走到安全点超时（秒）
    },
}


def deep_merge(base, new):
    """递归合并：new 覆盖 base。

    - 两边都是 dict → 递归逐键合并（保留 base 中 new 没有的键）；
    - 否则直接用 new 覆盖 base 的值。
    用于「磁盘配置覆盖默认配置」：老配置缺的字段自动补默认值，
    用户改动过的字段以用户值为准。
    """
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v


class ConfigManager:
    """config.json 的读写封装（GUI / 引擎共享同一个实例引用）"""

    def __init__(self, path=None):
        self.path = path or os.path.join(ROOT, "config.json")
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)  # 先取默认副本
        self.load()  # 再用磁盘配置覆盖合并

    def load(self):
        """加载磁盘配置 → 深合并进默认值 → 清洗玩家模板路径与玩家点颜色

        由于 GUI 与引擎持有的是同一个 cfg 引用，加载后无需通知刷新。
        """
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    deep_merge(self.cfg, json.load(f))
            except Exception as e:
                print("配置读取失败，使用默认配置:", e)

        if migrate_player_template(self.cfg):
            self.save()  # 清洗结果落盘，下次启动不再报错

        # player_dot_color 合法性清洗：畸形值直接移除（校准后会写回正确值）
        p = self.cfg.get("patrol", {})
        pdc = p.get("player_dot_color")
        if pdc is not None and not (
                isinstance(pdc, list) and len(pdc) == 3
                and all(isinstance(v, int) and not isinstance(v, bool)
                        and 0 <= v <= 255 for v in pdc)):
            p.pop("player_dot_color", None)
            self.save()

    def save(self):
        """把内存中的 cfg 整体写回 config.json（UTF-8，含缩进）"""
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("配置保存失败:", e)