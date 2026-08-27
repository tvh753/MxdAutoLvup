# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:59
# @Author  : yy
# @File    : config_manager.py
# @Software: MxdAutoLvup

import json, os, copy,shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, "templates")

# ================= 玩家模板全局路径体系 =================
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
    "window_title": "冒险岛怀旧服",
    "monster_templates": [],              # [{"name": "蓝蘑菇", "path": "templates/xx.png"}]
    "player_template": {"name": "player", "path": "templates/player"},              # {"name": "玩家", "path": "..."}
    "detect_region": None,                # [x, y, w, h] 缩小搜索范围提速
    "hp_bar": {"x": 0, "y": 0, "w": 0, "h": 0},  # 红 · 状态栏底部
    "mp_bar": {"x": 0, "y": 0, "w": 0, "h": 0},  # 蓝
    "exp_bar": {"x": 0, "y": 0, "w": 0, "h": 0},  # 黄 · 经验条（仅监控显示）
    "keys": {
        "attack": "q", "skill1": "q", "skill2": "q", "skill3": "q",
        "hp_potion": "1", "mp_potion": "2",
        "move_left": "left", "move_right": "right", "jump": "alt",
        "up": "up", "down": "down",          # ← 新增：抓绳/爬绳
        "teleport": "shift",              # ← 新增：法师瞬移
        "pickup": "z",  # ← 新增：拾取
    },
    "thresholds": {
        "match": 0.80, "hp_potion": 55, "mp_potion": 35, "hp_stop": 12,
        "attack_range": 160, "potion_cooldown": 1.2, "roam_interval": 2.5,
        "skill_range": 260,
	    "chase_range": 220,        # 追击距离：怪物屏幕像素距离上限（巡逻时）
        "off_route_tol": 30,       # 偏离容差：小地图px，超出即放弃追击回归路线
        "pickup_interval": 0.3,    # 拾取间隔：移动中边走边捡（原 0.9）
    },
    "options": {
        "use_skill_rotation": True, "jump_while_roam": True,
        "stop_on_low_hp": True, "pause_on_unfocus": True,
        "loot_enabled": True
    },
    "patrol": {
        "enabled": False,
        "minimap": {"x": 0, "y": 0, "w": 0, "h": 0},
        "player_dot_color": [60, 230, 255],
        "dot_tolerance": 80,
        "search_range": 10,  # 玩家周围搜索半径（小地图px）
        "grab_tol": 4,  # 抓绳水平容差
        "route_path": "",  # 颜色路线图路径（地图包内）
        "current_map": "",  # 当前激活地图包名
        "dot_max_area": 40,         # 玩家点面积上限（点检测过滤）
    },
    "schedule": {
        "enabled": False, "duration_min": 60,
        "rest_lo_min": 5, "rest_hi_min": 10, "safe_stop_wait": 120,
    },
}


def deep_merge(base, new):
    for k, v in new.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v


class ConfigManager:
    def __init__(self, path=None):
        self.path = path or os.path.join(ROOT, "config.json")
        os.makedirs(TEMPLATE_DIR, exist_ok=True)
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    deep_merge(self.cfg, json.load(f))
            except Exception as e:
                print("配置读取失败，使用默认配置:", e)

        if migrate_player_template(self.cfg):
            self.save()  # 清洗结果落盘，下次启动不再报错

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("配置保存失败:", e)