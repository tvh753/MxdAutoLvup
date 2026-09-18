# -*- coding: utf-8 -*-
"""读取 config/config_map.yaml，把「配置名」映射到 maps/<package>/ 目录。

与 main_window.py 的联动：
  · refresh_maps()   → 下拉列表里显示 entry.name
  · load_map_pack()  → entry = find_by_name(sel)，用 entry.package 作为目录
  · cfg["patrol"]["current_map"] 始终存 package（目录名），
    这样 bot_engine.reload_runtime() 里
        pack_dir = os.path.join(ROOT, "maps", current_map, "monsters")
    不用改。
"""
import os
from dataclasses import dataclass
from typing import List, Optional

try:
    import yaml
except ImportError:
    yaml = None

# 避免和 config_manager 循环导入：运行时再取 ROOT
_DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class MapEntry:
    name: str       # 下拉列表显示名（配置名）
    package: str    # maps/ 下的地图包目录名


class MapConfigError(Exception):
    """配置文件本身有问题（不存在 / 格式错 / 重名 / 路径非法）。"""


def _paths():
    try:
        from core.config_manager import ROOT
    except Exception:
        ROOT = _DEFAULT_ROOT
    return (os.path.join(ROOT, "config"),
            os.path.join(ROOT, "config", "config_map.yaml"),
            os.path.join(ROOT, "maps"))


def load_map_entries(config_file: Optional[str] = None) -> List[MapEntry]:
    """读取 config_map.yaml → List[MapEntry]。"""
    if yaml is None:
        raise MapConfigError("未安装 pyyaml，请 pip install pyyaml")
    _, default_file, _ = _paths()
    path = config_file or default_file
    if not os.path.isfile(path):
        raise MapConfigError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    items = raw.get("maps")
    if not isinstance(items, list) or not items:
        raise MapConfigError("config_map.yaml 中的 maps 必须是非空列表")

    entries: List[MapEntry] = []
    seen = set()
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise MapConfigError(f"maps 第 {i} 项格式错误，应为 name/package 字典")
        name = str(item.get("name", "")).strip()
        package = str(item.get("package", "")).strip()
        if not name or not package:
            raise MapConfigError(f"maps 第 {i} 项缺少 name 或 package")
        if name in seen:
            raise MapConfigError(f"配置名重复: {name}")
        # 防路径穿越 / 绝对路径
        if os.path.isabs(package) or ".." in package.replace("\\", "/").split("/"):
            raise MapConfigError(f"maps 第 {i} 项 package 非法: {package}")
        seen.add(name)
        entries.append(MapEntry(name=name, package=package))
    return entries


def find_by_name(name: str, entries: Optional[List[MapEntry]] = None) -> Optional[MapEntry]:
    """按配置名查找。"""
    if entries is None:
        entries = load_map_entries()
    return next((e for e in entries if e.name == name), None)


def find_by_package(package: str, entries: Optional[List[MapEntry]] = None) -> Optional[MapEntry]:
    """按地图包目录名反查（下拉列表回显用）。"""
    if entries is None:
        entries = load_map_entries()
    return next((e for e in entries if e.package == package), None)