# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:58
# @Author  : yy
# @File    : __init__.py.py
# @Software: MxdAutoLvup

"""
gui 包：枫叶挂机控制台的图形界面层（基于 tkinter，深色游戏风）。

模块分工：
  main_window    —— 主控制台窗口：窗口绑定 / 模板框选 / 状态条校准 /
                     按键绑定 / 参数调节 / 实时预览 / 地图包管理
  widgets        —— 自绘 UI 组件（NeoButton 按钮、Bar 进度条、
                     KeyEntry 按键捕获、ScrollFrame 滚动容器）
  theme          —— 深色主题配色常量（见 theme.py）
  region_selector—— 跨窗口区域框选工具（怪物模板 / 状态条 / 小地图框选）
  route_painter  —— 颜色路线绘制器（在地图底图上画动作颜色线）
  route_editor   —— 路点式路线编辑器（v2 参考实现，已由绘制器取代）

与 core 层的关系：GUI 只通过 core.bot_engine 的公开接口驱动引擎，
不直接触碰底层截图/识别逻辑；引擎状态通过 status 字典 + 预览帧队列
单向回传。
"""


def main():
    """占位入口（真正的入口是项目根目录的 main.py）"""
    pass


if __name__ == "__main__":
    main()
