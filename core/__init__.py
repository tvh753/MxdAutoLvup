# -*- coding: utf-8 -*-
# @Time    : 26/8/26 19:05
# @Author  : yy
# @File    : __init__.py.py
# @Software: MxdAutoLvup

"""
core 包：枫叶挂机（冒险岛怀旧服）自动升级脚本的【核心逻辑层】。

模块分工（按数据流顺序）：
  window_capture   —— PrintWindow 后台截图，获取游戏画面帧
  detector         —— 模板匹配识别怪物 / 玩家（NMS 去重）
  resource_monitor —— HP / MP / EXP 状态条颜色识别
  color_route      —— 小地图颜色路线导航 v5（玩家定位 / 寻路 / 爬绳，主巡逻引擎）
  patrol           —— 小地图路点式巡逻导航 v2（旧版参考实现）
  rope_detector    —— 主画面绳子 / 梯子识别（爬绳前精调对位兜底）
  action_controller—— DirectInput 后台按键 + 连续移动状态机
  bot_engine       —— 独立决策线程：截图 → 识别 → 决策 → 执行 的主循环
  config_manager   —— config.json 读写 / 版本迁移 / 玩家模板路径体系
  map_manager      —— 地图包管理（底图 / 颜色路线 / 怪物模板的打包与加载）
  imio             —— Windows 中文路径安全的图片读写（绕开 ANSI API）

依赖关系：bot_engine 位于最顶层，向下组合调用其余各模块；
gui 层通过 bot_engine 的公开接口驱动，不直接触碰底层识别逻辑。
"""


def main():
    """占位入口（本包不需要直接执行，真正的入口是项目根目录的 main.py）"""
    pass


if __name__ == "__main__":
    main()
