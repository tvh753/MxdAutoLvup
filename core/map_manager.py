# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:25
# @Author  : yy
# @File    : map_manager.py
# @Software: MxdAutoLvup

"""地图包：小地图底图 + 颜色路线 + 怪物模板 + 专属配置 打包复用

maps/<名称>/
  ├── minimap.png   小地图底图（编辑/显示）
  ├── route.png     颜色路线层（黑底+标记色）
  ├── profile.json  专属配置（按键/血蓝条/检测区域/巡逻参数…）
  └── monsters/     该地图怪物模板
"""
import os
import json
import shutil
import cv2

from core.config_manager import deep_merge


class MapManager:
    def __init__(self, root):
        self.maps_dir = os.path.join(root, "maps")
        os.makedirs(self.maps_dir, exist_ok=True)

    def list_maps(self):
        if not os.path.isdir(self.maps_dir):
            return []
        return sorted(d for d in os.listdir(self.maps_dir)
                      if os.path.isdir(os.path.join(self.maps_dir, d)))

    def _path(self, name, *parts):
        return os.path.join(self.maps_dir, name, *parts)

    # ---------- 保存 ----------
    def save(self, name, cfg, minimap_img, route_img):
        d = self._path(name)
        os.makedirs(os.path.join(d, "monsters"), exist_ok=True)
        if minimap_img is not None:
            cv2.imwrite(self._path(name, "minimap.png"), minimap_img)
        if route_img is not None:
            cv2.imwrite(self._path(name, "route.png"), route_img)

        profile = {
            "window_title": cfg.get("window_title", ""),
            "keys": cfg.get("keys", {}),
            "detect_region": cfg.get("detect_region"),
            "hp_bar": cfg.get("hp_bar", {}),
            "mp_bar": cfg.get("mp_bar", {}),
            "exp_bar": cfg.get("exp_bar", {}),
            "patrol": {
                "minimap": cfg.get("patrol", {}).get("minimap", {}),
                "player_dot_color": cfg.get("patrol", {}).get("player_dot_color"),
                "dot_tolerance": cfg.get("patrol", {}).get("dot_tolerance", 80),
                "search_range": cfg.get("patrol", {}).get("search_range", 10),
                "grab_tol": cfg.get("patrol", {}).get("grab_tol", 4),
                "enabled": True,
                "route_path": self._path(name, "route.png"),
                "current_map": name,
            },
            "monster_templates": [],
            "player_template": None,
        }
        for t in cfg.get("monster_templates", []):        # 怪物模板拷入包内
            src = t.get("path")
            if src and os.path.exists(src):
                dst = os.path.join(d, "monsters", os.path.basename(src))
                shutil.copy2(src, dst)
                profile["monster_templates"].append({"name": t["name"], "path": dst})
        pt = cfg.get("player_template")
        if pt and pt.get("path") and os.path.exists(pt["path"]):
            dst = os.path.join(d, "monsters", os.path.basename(pt["path"]))
            shutil.copy2(pt["path"], dst)
            profile["player_template"] = {"name": pt["name"], "path": dst}

        with open(self._path(name, "profile.json"), "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        return profile

    # ---------- 加载 ----------
    def load(self, name, cfg):
        pfile = self._path(name, "profile.json")
        if not os.path.exists(pfile):
            return False
        try:
            with open(pfile, "r", encoding="utf-8") as f:
                profile = json.load(f)
        except Exception:
            return False
        profile["patrol"]["route_path"] = self._path(name, "route.png")
        for key in ("window_title", "keys", "detect_region", "hp_bar", "mp_bar",
                    "exp_bar", "monster_templates", "player_template"):
            if key in profile:
                cfg[key] = profile[key]
        cur = dict(cfg.get("patrol", {}))
        deep_merge(cur, profile.get("patrol", {}))
        cfg["patrol"] = cur
        return True

    def load_minimap(self, name):
        return cv2.imread(self._path(name, "minimap.png"))

    def load_route(self, name):
        return cv2.imread(self._path(name, "route.png"))

    def save_route(self, name, route_img):
        if route_img is not None:
            cv2.imwrite(self._path(name, "route.png"), route_img)

    def delete(self, name):
        d = self._path(name)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)