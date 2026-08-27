# -*- coding: utf-8 -*-
# @Time    : 26/8/27 3:25
# @Author  : yy
# @File    : map_manager.py
# @Software: MxdAutoLvup

"""地图包管理器 v2 —— 绑定式存储
  · 玩家模板：templates/player/player.png 全局单独存放（跨地图共用）
  · 怪物模板：归属地图包 maps/<名>/monsters/，加载即切换
  · 保存：底图缺失自动现场补拍；怪物以当前配置为准拷入并清理孤儿
  · 加载：旧版包内玩家模板自动迁移到全局；缺失文件返回清单
"""
import os
import json
import shutil
import time
from core.imio import imread_u, imwrite_u
from core.config_manager import deep_merge


class MapManager:
    def __init__(self, root):
        self.maps_dir = os.path.join(root, "maps")
        self.player_dir = os.path.join(root, "templates", "player")
        os.makedirs(self.maps_dir, exist_ok=True)
        os.makedirs(self.player_dir, exist_ok=True)
        self.player_path = os.path.join(self.player_dir, "player.png")

    # ---------------- 基础 ----------------
    def list_maps(self):
        if not os.path.isdir(self.maps_dir):
            return []
        return sorted(d for d in os.listdir(self.maps_dir)
                      if os.path.isdir(os.path.join(self.maps_dir, d)))

    def _path(self, name, *parts):
        return os.path.join(self.maps_dir, name, *parts)

    @staticmethod
    def _safe_name(name):
        keep = [c for c in str(name) if c not in r'\/:*?"<>|']
        return ("".join(keep).strip() or "怪物")[:24]

    @staticmethod
    def _in_dir(path, d):
        p = os.path.normcase(os.path.abspath(path))
        dd = os.path.normcase(os.path.abspath(d))
        return p.startswith(dd + os.sep)

    # ---------------- 玩家模板（全局单独存放） ----------------
    def save_player(self, img):
        if img is None:
            return None
        if not imwrite_u(self.player_path, img):
            raise IOError(f"玩家模板写入失败: {self.player_path}")
        return self.player_path

    def player_exists(self):
        return os.path.isfile(self.player_path)

    # ---------------- 怪物模板（地图绑定） ----------------
    def add_monster(self, map_name, monster_name, img):
        """怪物模板直接存入地图包，返回包内路径"""
        d = self._path(map_name, "monsters")
        os.makedirs(d, exist_ok=True)
        base = self._safe_name(monster_name)
        p = os.path.join(d, base + ".png")
        if os.path.exists(p):  # 同名 → 时间戳后缀防覆盖
            p = os.path.join(d, f"{base}_{int(time.time() * 1000) % 100000}.png")
        if not imwrite_u(p, img):
            raise IOError(f"怪物模板写入失败: {p}")
        return p

    def list_monsters(self, map_name):
        d = self._path(map_name, "monsters")
        if not os.path.isdir(d):
            return []
        return [f for f in sorted(os.listdir(d)) if f.lower().endswith(".png")]

    # ---------------- 保存地图包 ----------------
    def save(self, name, cfg, minimap_img, route_img, grab_fn=None):
        """grab_fn：现场截图回调（返回整帧 BGR 或 None），用于底图自动补拍"""
        d = self._path(name)
        pack_mon_dir = os.path.join(d, "monsters")
        os.makedirs(pack_mon_dir, exist_ok=True)
        # ① 底图：传入优先；缺失 → ROI 已校准则现场补拍
        if minimap_img is None and grab_fn:
            mm = cfg.get("patrol", {}).get("minimap", {})
            if mm.get("w", 0) > 4 and mm.get("h", 0) > 4:
                frame = grab_fn()
                if frame is not None:
                    minimap_img = frame[mm["y"]:mm["y"] + mm["h"],
                                  mm["x"]:mm["x"] + mm["w"]].copy()
        if minimap_img is not None and \
                not imwrite_u(self._path(name, "minimap.png"), minimap_img):
            raise IOError(f"小地图底图写入失败: {d}")
        # ② 路线图：有新图才写；None 时保留包内旧图（重存不洗掉路线）
        if route_img is not None and \
                not imwrite_u(self._path(name, "route.png"), route_img):
            raise IOError(f"路线图写入失败: {d}")
        # ③ 怪物绑定：当前配置模板 → 拷入包内，统一指向包内路径
        monsters = []
        for t in cfg.get("monster_templates", []):
            src = t.get("path")
            nm = t.get("name") or "怪物"
            if not src or not os.path.exists(src):
                continue
            if self._in_dir(src, pack_mon_dir):
                dst = src  # 已在包内
            else:
                base = self._safe_name(nm)
                dst = os.path.join(pack_mon_dir, base + ".png")
                if os.path.exists(dst):
                    dst = os.path.join(
                        pack_mon_dir, f"{base}_{int(time.time() * 1000) % 100000}.png")
                shutil.copy2(src, dst)
            monsters.append({"name": nm, "path": dst})
        # ④ 清理孤儿：包内未被当前配置引用的模板（删除=真删的落盘体现）
        keep = {os.path.normcase(os.path.abspath(m["path"])) for m in monsters}
        for f in os.listdir(pack_mon_dir):
            fp = os.path.join(pack_mon_dir, f)
            if os.path.isfile(fp) and \
                    os.path.normcase(os.path.abspath(fp)) not in keep:
                try:
                    os.remove(fp)
                except OSError:
                    pass
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
                "dot_max_area": cfg.get("patrol", {}).get("dot_max_area", 40),
                "search_range": cfg.get("patrol", {}).get("search_range", 10),
                "grab_tol": cfg.get("patrol", {}).get("grab_tol", 4),
                "enabled": True,
                "route_path": self._path(name, "route.png"),
                "current_map": name,
            },
            "monster_templates": monsters,  # ← 地图↔怪物绑定清单
            # 玩家模板不进包：全局 templates/player/player.png 单独存放
        }
        with open(self._path(name, "profile.json"), "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        cfg["monster_templates"] = monsters  # config 同步指向包内路径
        return profile

    # ---------------- 加载地图包 ----------------
    def load(self, name, cfg):
        pfile = self._path(name, "profile.json")
        if not os.path.exists(pfile):
            return False, []
        try:
            with open(pfile, "r", encoding="utf-8") as f:
                profile = json.load(f)
        except Exception:
            return False, []
        profile.setdefault("patrol", {})["route_path"] = \
            self._path(name, "route.png")
        for key in ("window_title", "keys", "detect_region", "hp_bar",
                    "mp_bar", "exp_bar", "monster_templates"):
            if key in profile:
                cfg[key] = profile[key]
        cur = dict(cfg.get("patrol", {}))
        deep_merge(cur, profile.get("patrol", {}))
        cfg["patrol"] = cur
        # 旧版包内玩家模板 → 迁移到全局单独存放（一次性，幂等）
        old = profile.get("player_template")
        if old and old.get("path") and os.path.exists(old["path"]) \
                and not self.player_exists():
            img = imread_u(old["path"])
            if img is not None:
                try:
                    self.save_player(img)
                except IOError:
                    pass
        # 玩家模板：全局存在 → 恢复引用
        if self.player_exists():
            cfg["player_template"] = {"name": "玩家", "path": self.player_path}
        missing = [t["name"] for t in cfg.get("monster_templates", [])
                   if not os.path.exists(t.get("path", ""))]
        rp = cfg["patrol"].get("route_path", "")
        if rp and not os.path.exists(rp):
            missing.append("route.png(路线图)")
        if not os.path.isfile(self._path(name, "minimap.png")):
            missing.append("minimap.png(小地图底图)")
        return True, missing

    # ---------------- 图片存取（imio 中文路径安全） ----------------
    def load_minimap(self, name):
        return imread_u(self._path(name, "minimap.png"))

    def load_route(self, name):
        return imread_u(self._path(name, "route.png"))

    def save_minimap(self, name, img):
        return img is not None and imwrite_u(self._path(name, "minimap.png"), img)

    def save_route(self, name, route_img):
        return route_img is not None and \
            imwrite_u(self._path(name, "route.png"), route_img)

    def delete(self, name):
        d = self._path(name)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)