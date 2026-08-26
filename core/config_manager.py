# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:59
# @Author  : yy
# @File    : config_manager.py
# @Software: MxdAutoLvup

import json, os, copy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, "templates")

DEFAULT_CONFIG = {
    "window_title": "冒险岛怀旧服",
    "monster_templates": [],              # [{"name": "蓝蘑菇", "path": "templates/xx.png"}]
    "player_template": None,              # {"name": "玩家", "path": "..."}
    "detect_region": None,                # [x, y, w, h] 缩小搜索范围提速
    "hp_bar": {"x": 0, "y": 0, "w": 0, "h": 0},  # 红 · 状态栏底部
    "mp_bar": {"x": 0, "y": 0, "w": 0, "h": 0},  # 蓝
    "exp_bar": {"x": 0, "y": 0, "w": 0, "h": 0},  # 黄 · 经验条（仅监控显示）
    "keys": {
        "attack": "q", "skill1": "q", "skill2": "q", "skill3": "q",
        "hp_potion": "1", "mp_potion": "2",
        "move_left": "left", "move_right": "right", "jump": "alt",
    },
    "thresholds": {
        "match": 0.80, "hp_potion": 55, "mp_potion": 35, "hp_stop": 12,
        "attack_range": 160, "potion_cooldown": 1.2, "roam_interval": 2.5,
    },
    "options": {
        "use_skill_rotation": True, "jump_while_roam": True,
        "stop_on_low_hp": True, "pause_on_unfocus": True,
    },
    "patrol": {
        "enabled": False,
        "minimap": {"x": 0, "y": 0, "w": 0, "h": 0},
        "player_dot_color": [60, 230, 255],
        "dot_tolerance": 80,
        "waypoints": [],  # [[mx, my], ...] 小地图坐标
        "mode": "pingpong",  # pingpong 往返 / loop 循环
        "arrive_tol": 6,
        "max_chase_time": 8.0,  # 追击超时，超时回归路线
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

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("配置保存失败:", e)