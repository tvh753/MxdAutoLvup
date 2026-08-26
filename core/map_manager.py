# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:25
# @Author  : yy
# @File    : map_manager.py
# @Software: MxdAutoLvup

# -*- coding: utf-8 -*-
"""地图包：小地图底图 + 颜色路线 + 怪物模板 + 专属配置 打包复用
maps/<名称>/
  ├── minimap.png   小地图底图
  ├── route.png     颜色路线层
  ├── profile.json  专属配置
  └── monsters/     该地图怪物模板
所有图片 IO 走 imio（中文路径安全）；保存后 config.json 与 profile
统一指向包内文件，杜绝“两套路径”。
"""
import os
import json
import shutil
from core.imio import imread_u, imwrite_u
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

    @staticmethod
    def _copy_into_pack(src, pack_dir):
        """模板拷入地图包，返回包内路径；源丢失但包内有旧副本时沿用副本
        （支持对同一地图包反复保存）；src==dst 时跳过拷贝防 SameFileError。"""
        if not src:
            return None
        dst = os.path.join(pack_dir, "monsters", os.path.basename(src))
        if os.path.exists(src):
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
            return dst
        if os.path.exists(dst):
            return dst
        return None

    # ---------- 保存 ----------
    def save(self, name, cfg, minimap_img, route_img):
        d = self._path(name)
        os.makedirs(os.path.join(d, "monsters"), exist_ok=True)
        if minimap_img is not None and not imwrite_u(self._path(name, "minimap.png"),
                                                     minimap_img):
            raise IOError(f"小地图底图写入失败: {d}")
        if route_img is not None and not imwrite_u(self._path(name, "route.png"),
                                                   route_img):
            raise IOError(f"路线图写入失败: {d}")
        monsters = []
        for t in cfg.get("monster_templates", []):
            dst = self._copy_into_pack(t.get("path"), d)
            if dst:
                monsters.append({"name": t.get("name", "怪物"), "path": dst})
        player = None
        pt = cfg.get("player_template")
        if pt:
            dst = self._copy_into_pack(pt.get("path"), d)
            if dst:
                player = {"name": pt.get("name", "玩家"), "path": dst}
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
            "monster_templates": monsters,
            "player_template": player,
        }
        with open(self._path(name, "profile.json"), "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        # ★ 关键：config.json 同步指向包内文件，保存/加载/重存一套路径
        cfg["monster_templates"] = monsters
        cfg["player_template"] = player
        return profile

    # ---------- 加载 ----------
    def load(self, name, cfg):
        """成功返回，missing 为缺失文件名列表（供日志提示）"""
        pfile = self._path(name, "profile.json")
        if not os.path.exists(pfile):
            return False, []
        try:
            with open(pfile, "r", encoding="utf-8") as f:
                profile = json.load(f)
        except Exception:
            return False, []
        profile.setdefault("patrol", {})["route_path"] = self._path(name, "route.png")
        for key in ("window_title", "keys", "detect_region", "hp_bar", "mp_bar",
                    "exp_bar", "monster_templates", "player_template"):
            if key in profile:
                cfg[key] = profile[key]
        cur = dict(cfg.get("patrol", {}))
        deep_merge(cur, profile.get("patrol", {}))
        cfg["patrol"] = cur
        missing = [t["name"] for t in cfg.get("monster_templates", [])
                   if not os.path.exists(t.get("path", ""))]
        pt = cfg.get("player_template")
        if pt and not os.path.exists(pt.get("path", "")):
            missing.append(pt.get("name", "玩家"))
        rp = cfg["patrol"].get("route_path", "")
        if rp and not os.path.exists(rp):
            missing.append("route.png(路线图)")
        mm = self._path(name, "minimap.png")
        if not os.path.exists(mm):
            missing.append("minimap.png(小地图底图)")
        return True, missing

    def load_minimap(self, name):
        return imread_u(self._path(name, "minimap.png"))

    def load_route(self, name):
        return imread_u(self._path(name, "route.png"))

    def save_route(self, name, route_img):
        if route_img is not None:
            return imwrite_u(self._path(name, "route.png"), route_img)
        return False

    def delete(self, name):
        d = self._path(name)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)