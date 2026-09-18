# 🍁 枫叶挂机控制台 · MxdAutoLvup

基于 OpenCV 图色识别的冒险岛怀旧服自动化挂机工具。通过**后台窗口截图 + 模板匹配 + 颜色路线导航**，完成找怪、攻击、喝药、拾取、巡逻、爬绳、地图切换、定时休息等一系列操作，并提供深色游戏风格的图形控制台（GUI）。

> ⚠️ 本项目仅用于学习交流（图像处理、模板匹配、自动化脚本、GUI 编程）。请勿在官方服务器或违反游戏规则的场景使用。

---

## ✨ 功能特性

| 模块 | 说明 |
|---|---|
| 🪟 **双抓屏模式** | 本机窗口（PrintWindow / windows_capture）或虚拟机 VNC，二选一 |
| ⚡ **异步截图** | 优先使用 `windows_capture`（FPS 10~20），不可用时回退到 `PrintWindow` |
| 👾 **模板识别** | 多模板匹配 + 绿底掩膜 + NMS 去重，标注实时识别结果 |
| ❤ **资源监控** | 框选 HP/MP/EXP 状态条，颜色自适应识别百分比，自动喝药 |
| 🎮 **自动战斗** | 攻击/技能轮换、左右追击、脱离、站桩攻击、AOE 模式 |
| 🗺 **颜色路线** | 小地图/大 map 上绘制路线，录制一次、多图复用 |
| 🪢 **爬绳导航** | 对准绳 X 轴 → 停步 → 跳+↑ 抓绳 → 持续攀爬，失败自动脱困 |
| 📦 **地图包** | 每张地图独立的「底图 + 路线 + 怪物模板 + 按键」，热切换 |
| ⏱ **定时休息** | 挂机时长随机 ±3 分钟，到点休息后自动继续（拍卖场 / 绳） |
| 🔔 **定时下线** | 配置时间自动执行 Esc→↑→Enter 下线，次日按 Enter 上线 |
| 🔒 **防误操作** | 失焦自动暂停按键、按键下发全程后台 DirectInput |

---

## 🧰 环境要求

- **Windows 10 / 11**（64 位）
- **Python 3.12**
- 依赖见 `requirements.txt`

### 安装依赖

```bash
pip install -r requirements.txt
```

### 关键依赖说明

| 包名 | 用途 | 是否必装 |
|---|---|---|
| `opencv-python` | 截图、模板匹配、绘图 | **必装** |
| `numpy` | 图像数组、坐标运算 | **必装** |
| `pydirectinput` | DirectInput 扫描码发送按键 | **必装** |
| `pywin32` | win32gui/win32ui/win32api（GDI 截图、窗口操作） | **必装** |
| `Pillow` | numpy 数组 → tkinter PhotoImage | **必装**（GUI） |
| `windows-capture` | Windows Graphics Capture API 异步截图 | 推荐 |
| `vncdotool` | VNC 虚拟机抓屏 + 键鼠下发 | 可选（VNC 模式） |
| `keyboard` | 全局热键（任意窗口焦点生效） | 可选 |

> 注：`pydirectinput` 依赖 `pywin32`；如游戏以**管理员权限**运行，脚本也需以管理员权限启动。

---

## 🚀 快速上手

1. **启动**
   ```bash
   python main.py
   ```

2. **绑定游戏窗口**：在「🎯 目标」页 → 选择窗口 → 点【绑定】。
   或切到「☁ 虚拟机游戏(VNC)」→ 填 IP/端口/密码 → 点【连接】。

3. **框选怪物模板**：点【📷 框选怪物模板】→ 实时画面中框选怪物本体 → 命名保存。

4. **校准状态条**（可选）：满血/满蓝时框选完整 HP/MP/EXP 条，颜色自动识别。

5. **配置按键**：切到「⌨ 按键」页，点击输入框 → 按键盘按键绑定。

6. **加载地图包**（强烈推荐）：
   - 「🎯 目标」页 → 「🗺 地图包」区域 → 选择配置名 → 点【加载】。
   - 没有现成地图包时：点【新增】建目录 → 放底图 → 【🧭 校准小地图】 → 【🎨 绘制路线】 → 【💾 保存】。

7. **启动挂机**：点【▶ 启动挂机】（或按 `F8`），`F9` 暂停/恢复。

---

## 📸 界面预览

### 目标页（抓屏模式 / 窗口绑定 / 模板 / 地图包）

![目标页](https://github.com/tvh753/MxdAutoLvup/blob/master/img/main_target.png?raw=true)

### 按键页（按键映射 / 攻击方式 / 热键 / 定时按键）

![按键页](https://github.com/tvh753/MxdAutoLvup/blob/master/img/main_key.png?raw=true)

### 参数页（识别阈值 / 行为开关 / 巡逻微调 / 挂机时长）

![参数页](https://github.com/tvh753/MxdAutoLvup/blob/master/img/main_param.png?raw=true)

> 截图占位：请把三张控制台截图放到 `docs/screenshots/` 目录下，命名分别为 `main_target.png`、`main_key.png`、`main_param.png`。

---

## 🏗️ 线程模型（关键！）

整个引擎运行在 **4 个线程**里，通过共享状态 + 覆盖式 slot 通信，互不阻塞：

```
┌──────────────────────────────────────────────────────────────────────┐
│                          主线程（tkinter）                            │
│   事件回调 / 状态轮询（80ms）/ 日志轮询（250ms）/ 预览队列取出          │
└──────────────────────────────────────────────────────────────────────┘
        │ 配置注入 / 模式切换               ▲ 状态 / 预览 / 日志
        ▼                                  │
┌──────────────────────────┐    queue    ┌──────────────────────────┐
│   BotEngine 主循环线程    │────────────►│  GUI 预览 & 日志队列      │
│   _tick ~30 FPS          │              └──────────────────────────┘
│  ┌────────────────────┐  │
│  │ ① 截图              │  │
│  │ ② 血蓝条（1/3 帧）  │  │
│  │ ③ 投喂识别帧         │  │
│  │ ④ 读导航结果         │  │
│  │ ⑤ 决策 + 预览        │  │
│  └────────────────────┘  │
└──────────────────────────┘
     │  投入 frame slot            ▲ 读取 monsters / player
     ▼                             │
┌──────────────────────────┐   覆盖式 slot   ┌──────────────────────────┐
│   识别线程 DetThread      │───────────────►│  主循环读最新结果          │
│   _det_loop              │                └──────────────────────────┘
│  · 玩家模板局部搜索 ±200  │
│  · 怪物模板攻击框内匹配    │
└──────────────────────────┘
     │  投入 frame slot            ▲ 读取 player_map
     ▼                             │
┌──────────────────────────┐   覆盖式 slot   ┌──────────────────────────┐
│   导航线程 NavThread      │───────────────►│  主循环读最新结果          │
│   _nav_loop              │                └──────────────────────────┘
│  · 名字条定位玩家画面坐标  │
│  · 小地图 → 大 map 坐标   │
└──────────────────────────┘

┌──────────────────────────┐
│  键盘线程 KbThread        │
│   _keyboard_loop 30 FPS  │
│  · 消费按键队列           │
│  · 持续按方向键（非阻塞）  │
│  · 独立于主循环，避免卡顿  │
└──────────────────────────┘
```

**设计要点**：

- **主循环只做决策**，识别/导航/按键都异步，主循环几乎不被阻塞。
- **覆盖式 slot**：识别/导航只需"最新"结果，中间帧丢弃无所谓 → 不积压。
- **键盘线程 30 FPS**：即使主循环卡 200ms，按键下发节奏仍稳定。
- **按键非阻塞**：`tap` 拆成"按下 → 记录释放时间 → 下一 tick 释放"，键盘线程不 sleep。

---

## 📁 目录结构

```
MxdAutoLvup/
├── main.py                    # 程序入口（DPI 感知 + 启动 GUI）
├── build.bat                  # PyInstaller 打包脚本
├── config.json                # 运行时配置（自动生成/更新）
├── requirements.txt           # Python 依赖清单
├── icon.ico                   # 程序图标
│
├── config/
│   └── config_map.yaml        # 配置名 ↔ 地图包目录名 映射表
│
├── core/                      # 核心逻辑层（无 GUI 依赖）
│   ├── bot_engine.py          # 引擎主线程（决策 / 巡逻 / 战斗 / 调度）
│   ├── action_controller.py   # 键盘控制器（非阻塞 + 独立线程）
│   ├── window_capture.py      # 三种抓屏：WindowCapture / FastWindowCapture / VncCapture
│   ├── vnc_controller.py      # VNC 键鼠控制器
│   ├── detector.py            # 模板匹配（多模板 + 绿底掩膜 + NMS）
│   ├── resource_monitor.py    # HP/MP/EXP 状态条检测
│   ├── color_route.py         # 颜色路线导航（定位 / 寻路 / 爬绳）
│   ├── patrol.py              # 路点式巡逻（备用实现）
│   ├── map_manager.py         # 地图包管理（底图 / 路线 / 怪物模板）
│   ├── map_recorder.py        # 小地图滚动录制器
│   ├── map_config.py          # config_map.yaml 的解析
│   ├── config_manager.py      # config.json 读写 + 版本迁移
│   ├── hotkey_manager.py      # 局部/全局热键封装
│   ├── rope_detector.py       # 主画面绳子识别（爬绳对位）
│   └── imio.py                # 中文路径安全的图片读写
│
├── gui/                       # 图形界面层（tkinter）
│   ├── main_window.py         # 主控制台窗口
│   ├── widgets.py             # 自绘组件（按钮 / 进度条 / 按键捕获 / 滚动）
│   ├── theme.py               # 深色主题配色
│   ├── region_selector.py     # 区域框选工具
│   ├── route_painter.py       # 颜色路线绘制器
│   └── route_editor.py        # 路点式路线编辑器
│
├── maps/                      # 地图包目录（每个子目录 = 一张地图）
│   └── <地图名>/
│       ├── map.png            # 小地图底图（快照，或从 wz 缩放的）
│       ├── minimap_wz.png     # wz 原图（可选，自动缩放为 map.png）
│       ├── minimap.png        # 小地图快照（小地图模式用）
│       ├── route.png          # 颜色路线图（单条）
│       ├── route1.png ...     # 多路线（交替循环）
│       ├── profile.json       # 地图包配置（怪物模板 / 按键 / 参数）
│       └── monsters/          # 该地图的怪物模板
│           ├── 怪物1.png
│           └── ...
│
├── templates/                 # 全局模板（跨地图共用）
│   ├── player/
│   │   ├── player.png         # 玩家名字条模板
│   │   └── huangdian.bmp      # 小地图黄点模板
│   └── dot/
│       └── dot.png            # 小地图定位用黄点模板
│
└── tools/                     # 辅助工具（开发调试用）
    ├── t_locate.py            # 玩家定位调试
    ├── detect_yellow_dot.py   # 黄点颜色采样
    ├── record_map.py          # 地图录制
    ├── route_recorder.py      # 路线录制
    └── 打包指南.md             # 打包 exe 说明
```

---

## ⚙️ 配置说明（config.json）

`config.json` 与 `config/config_map.yaml` 两个文件是整个项目的"配置数据库"。

### 顶层字段

```jsonc
{
  "window_title": "冒险岛怀旧服",       // 绑定窗口标题关键字
  "capture_mode": "window",             // "window" | "vnc"
  "vnc": { ... },                       // VNC 连接参数
  "player_template": {                  // 全局玩家名字条模板
    "name": "玩家",
    "path": ".../templates/player/player.png"
  },
  "monster_templates": [],              // 运行时由地图包填充，不落盘
  "detect_region": null                 // 检测区域 [x,y,w,h]，null=全屏
}
```

### keys —— 按键映射

```jsonc
"keys": {
  "attack": "a",        // 普通攻击
  "skill1": "q",        // 技能 1~3
  "skill2": "s",
  "skill3": "q",
  "hp_potion": "1",     // 红药
  "mp_potion": "2",     // 蓝药
  "move_left": "left",  // 左移
  "move_right": "right",// 右移
  "jump": "space",      // 跳
  "up": "up",           // 上爬绳
  "down": "down",       // 下爬绳
  "teleport": "shift_l",// 法师瞬移
  "pickup": "z",        // 拾取
  "escape": "escape",   // 下线
  "enter": "enter",     // 上线 / 确认
  "pet_potion": "",     // 宠物药（空=禁用）
  "buff1": "", ...      // 5 个 BUFF 键（空=禁用）
}
```

### thresholds —— 识别与策略参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `match` | 0.75 | 模板匹配置信度阈值（越高越严格） |
| `hp_potion` | 30 | 红药触发百分比 |
| `mp_potion` | 10 | 蓝药触发百分比 |
| `hp_stop` | 12 | 血量低于此值触发停机保护 |
| `attack_range` | 221 | 攻击框水平范围（像素） |
| `skill_range` | 86 | 攻击框垂直高度 |
| `attack_bottom_offset` | 0 | 攻击框底边相对玩家 Y 的偏移（正=下移） |
| `chase_range` | 209 | 追击范围（应 ≥ attack_range） |
| `off_route_tol` | 50 | 偏离路线容差（像素），超出禁战 |
| `pickup_interval` | 0.1 | 拾取间隔（秒） |
| `potion_cooldown` | 1.2 | 喝药最小冷却 |
| `roam_interval` | 5.2 | 无路线时左右转向间隔 |
| `mob_overlap_area` | 60 | 怪物框与攻击框最小重叠面积 |
| `atk_cd_normal` | [0.2, 0.25] | 普攻冷却随机区间 |
| `atk_cd_skill` | [0.15, 0.2] | 技能冷却随机区间 |

### options —— 行为开关

```jsonc
"options": {
  "use_skill_rotation": true,   // 技能轮换
  "jump_while_roam": true,      // 巡逻时随机跳
  "stop_on_low_hp": false,      // 血量过低自动停机
  "pause_on_unfocus": true,     // 失焦暂停按键（推荐）
  "loot_enabled": true,         // 边走边拾取
  "preview_enabled": true,      // 控制台右侧预览（关=省 CPU）
  "nav_panel_enabled": true     // 预览右上角 NAV 面板
}
```

### patrol —— 巡逻 / 地图包

```jsonc
"patrol": {
  "enabled": true,              // 路线巡逻开关
  "minimap": { "x": 32, "y": 104, "w": 95, "h": 109 },
  "player_dot_color": [3, 255, 255],  // 小地图黄点 BGR
  "search_range": 40.0,         // 路线搜索半径
  "route_path": "D:\\...\\maps\\南部森林训练场Ⅲ\\route.png",
  "current_map": "南部森林训练场Ⅲ",
  "map_offset": [0.0, 0.0],     // map 坐标微调
  "max_chase_time": 6.6         // 单次追击最长时长
}
```

### schedule —— 定时调度（休息 / 下线）

```jsonc
"schedule": {
  "enabled": true,
  "rest_mode": "auction",       // "auction"（拍卖场）| "rope"（绳子）
  "duration_min": 15,           // 一轮挂机时长（实际 ±3 分钟）
  "rest_lo_min": 5,             // 休息最短（分钟）
  "rest_hi_min": 10,            // 休息最长（分钟）
  "auction": {
    "enter_x": 1140, "enter_y": 750,  // 进拍卖按钮坐标
    "exit_x":  1158, "exit_y":  25,   // 退拍卖按钮坐标
    "use_esc_to_exit": false,
    "click_jitter": 8,
    "enter_wait": 3.0,
    "exit_wait": 3.0
  },
  "logout_enabled": false,      // 定时下线
  "logout_time": "00:18",
  "login_enabled": false,       // 定时上线
  "login_time": "08:00"
}
```

### hotkeys —— 控制台热键

```jsonc
"hotkeys": {
  "start_stop": "f8",           // 启动/停止
  "pause_resume": "f9",         // 暂停/继续
  "global_enabled": true        // 全局热键（需 keyboard 库）
}
```

---

## 📦 地图包（Map Pack）

一个地图包 = 一张地图的完整"作战资料"，存于 `maps/<地图名>/`：

```
maps/南部森林训练场Ⅲ/
├── map.png             # 小地图底图（从 minimap_wz.png 等比缩放）
├── minimap_wz.png      # wz 原图（可选）
├── minimap.png         # 小地图快照
├── route.png           # 单条路线
├── route1.png          # 或多条路线（交替循环）
├── route2.png
├── profile.json        # 地图包级配置
└── monsters/           # 该地图的怪物模板
    ├── 怪物1.png
    ├── 怪物2.png
    └── ...
```

### 地图包的生命周期

```
创建 → 放底图 → 校准小地图 → 绘制路线 → 绑定怪物 → 保存
  │        │          │            │          │        │
  └────────┴──────────┴────────────┴──────────┴────────┘
                         ▼
                    下次直接「加载」
```

### config_map.yaml

每新增一个地图包，需要在 `config/config_map.yaml` 里登记一条记录（配置名 ↔ 目录名），GUI 下拉列表会从它读：

```yaml
maps:
- name: 蘑菇山
  package: 蘑菇山
- name: 南部森林训练场Ⅲ
  package: 南部森林训练场Ⅲ
- name: 火焰之地Ⅴ
  package: 火焰之地Ⅴ
```

---

## 🎨 绘制颜色路线（v5）

打开「🎨 绘制路线」，左侧色板选择**动作颜色**，拖动绘制：

| 颜色 | 动作 | 颜色 | 动作 |
|---|---|---|---|
| 🔴 红 | 左走 | 🔵 蓝 | 右走 |
| 🟠 橙 | 左跳 | 🟡 青 | 右跳 |
| 🟢 黄绿 | 下跳 | 🟣 品红 | 原地跳 |
| 🟩 浅绿 | 停止（休息点） | 🟨 黄 | 终点 |
| 🌸 粉 | 上瞬移 | 🟪 紫 | 下瞬移 |
| 🟢 深绿 | 左瞬移 | 🟤 棕 | 右瞬移 |
| ⬜ 灰 | 上爬绳 | 🟨 淡黄 | 下爬绳 |

**技巧**：
- 按住 **Shift** 拖 = 画直线，可吸附水平/垂直（画平台横线、绳子竖线）；
- 右键 = 撤销上一笔；「橡皮擦」擦掉画错的线；
- 平台画横线、绳子画竖线，路线容差高（自动按最短路经寻点）；
- 终点画黄：可多路线循环（`route.png` → `route1.png` → `route2.png` → ...）。

---

## 🛠 技术原理

### 1. 后台截图 `window_capture.py`

三种实现，接口一致：

| 类 | 原理 | 性能 |
|---|---|---|
| `WindowCapture` | `PrintWindow` + `PW_RENDERFULLCONTENT` | 单帧 ~100ms，FPS 3~5 |
| `FastWindowCapture` | `windows_capture`（Windows Graphics Capture） | 单帧 ~5ms，FPS 10~20 |
| `VncCapture` | `vncdotool` 远程 VNC | 单帧 ~50~150ms（网络往返） |

启动时**优先用 FastWindowCapture**，失败回退到 WindowCapture。

### 2. 目标识别 `detector.py`

- 多模板匹配 + 置信度阈值 + **NMS 去重**；
- **绿底掩膜**：模板若为绿底素材（背景 `(0,255,0)`），自动生成掩膜忽略背景；
- **玩家局部搜索**：用上一帧位置 ±200px，速度提升 5~10 倍；
- **怪物攻击框内搜索**：只在攻击框 ± margin 内匹配，框外的怪不识别 → 省 CPU。

### 3. 状态条监测 `resource_monitor.py`

HP/MP/EXP 条裁剪 → HSV → 按颜色/亮度统计填充比例。容错方向错位、满血溢出。

### 4. 颜色路线导航 `color_route.py`

- **路线编码**：像素颜色 → 最近邻 `COLOR_CODE` 索引 → `label` 图；
- **玩家定位**：小地图在 map 上模板匹配 + 黄点相对位置；
- **寻路**：取玩家周围 `search_range` 内的标记，按「距离 + 背后惩罚」评分选动作；
- **爬绳状态机**：`align → grab → climb / backoff`；
- **停滞看门狗**：长时间不动 → 随机脱困。

### 5. 动作执行 `action_controller.py`

- **非阻塞**：`tap` 拆成"按下 → 下一 tick 释放"，键盘线程不 sleep；
- **移动状态机**：`set_dir / set_climb` 只改目标状态，键盘线程 30 FPS 检查变化后发键；
- **`repress_dir`**：强制重按方向键（攻击前保证朝向同步）。

### 6. 决策引擎 `bot_engine.py`

- **覆盖式架构**（参考项目 HuntingState）：
  ```
  cmd_x, cmd_y, cmd_action = 巡逻指令
  if 有怪能打: cmd_action = "attack"; cmd_x = 方向
  apply(cmd_x, cmd_y, cmd_action)
  ```
- 爬绳 / 战斗 / 巡逻共存，战斗优先级高。

### 7. 配置管理 `config_manager.py`

`config.json` 集中管理全部参数，`ConfigManager` 负责：
- 启动时读磁盘 + 深合并默认值；
- 玩家模板路径迁移清洗（收敛到全局路径）；
- `monster_templates` 不落盘（地图包所有）。

---

## ❓ 常见问题（FAQ）

### Q1：识别不到怪物 / 识别率低？
- 重新框选怪物模板（**避开血条数字、选特征明显的部位**）；
- 调低「参数」页「匹配阈值」（0.6~0.7 之间）；
- 确认模板**没有绿底**（若为绿底会自动启用掩膜，可能过度过滤）。

### Q2：怪在攻击框里但不打？
按顺序检查：
1. `keys.attack` 是否为空 → 看日志 `[攻击] mode=normal attack='...'`；
2. `chase_range` 是否 ≥ `attack_range`；
3. `skill_range` 是否足够大（默认 86，垂直只覆盖 86px）；
4. `mob_overlap_area` 是否过大（默认 60，试试 15）；
5. 预览里的攻击框是否真的框住了怪。

### Q3：角色不转身，攻击打空？
- 检查 `keys.move_left` / `keys.move_right` 是否绑定；
- 看日志 `[TURN] want=... got=...` → 若 `got` 一直为 None，键盘线程可能卡住；
- `pydirectinput` 需要游戏以相同权限运行。

### Q4：找不到小地图玩家黄点？
- 重新【🧭 校准小地图】（自动重新采样黄点颜色）；
- 调大参数页「玩家点面积」；
- 确认框选范围**只含地图区域**、不含标题文字。

### Q5：游戏画面黑屏 / 截不到图？
- 游戏窗口**不能最小化**；
- 部分全屏模式请改用**窗口化**运行；
- `PrintWindow` 对某些渲染引擎失效 → 切到无边框窗口。

### Q6：按键没反应？
- 游戏以管理员运行时，脚本也需以管理员运行；
- 检查「行为开关」里「游戏失焦时暂停按键」是否误触发；
- 检查 `keys.attack` 等按键是否为空。

### Q7：VNC 连接失败？
- 检查 VNC 服务是否开启（RealVNC / TightVNC / UltraVNC）；
- 检查端口（默认 5900）、防火墙；
- 密码错误或未设密码时，`password` 留空。

### Q8：FPS 只有 30？
- 主循环硬编码 `1/30s` 上限 → 想上 60 改 `_tick` 里 `target_duration`；
- 装 `windows-capture` 库可显著提升截图速度。

### Q9：定时下线/上线没执行？
- 检查 `schedule.logout_enabled` / `login_enabled` 是否为 true；
- 时间为**当天**还是次日：`logout_time` 比 `login_time` 晚就正常；
- 看日志有没有 `⏰ 定时下线将于 ... 执行`。

### Q10：控制台有日志，终端也在刷屏？
- 找到 `gui/main_window.py` 的 `_log_fn`：
  ```python
  def _log_fn(m, lv="info"):
      print(f"[{time.strftime('%H:%M:%S')}][{lv}] {m}")  # ← 删这行
      self.log_queue.put((time.strftime("%H:%M:%S"), m, lv))
  ```

---

## 📦 打包为 exe

项目已提供 `build.bat`（PyInstaller 打包脚本）。完整流程见 [tools/打包指南.md](tools/打包指南.md)。

简版：

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --icon=icon.ico ^
  --add-data "config;config" ^
  --add-data "templates;templates" ^
  --add-data "maps;maps" ^
  main.py
```

打包后 `dist/MxdAutoLvup.exe` 与 `config/`、`templates/`、`maps/` 同级即可运行。

---

## 📝 更新日志

### v5（当前版本）
- ✅ 引擎拆成 4 线程（主 / 键盘 / 识别 / 导航）
- ✅ 键盘非阻塞：`tap` 拆成"按下 + 下一 tick 释放"
- ✅ 攻击框底边可控（`attack_bottom_offset`）
- ✅ 玩家局部搜索（±200px），速度提升 5~10 倍
- ✅ 怪物只在攻击框内识别，省 CPU
- ✅ 定时调度支持拍卖场 / 绳子两种休息模式
- ✅ 定时下线 / 上线
- ✅ VNC 虚拟机模式
- ✅ 多路线循环
- ✅ 地图坐标偏移微调

---

## ⚠️ 免责声明

本项目仅用于**学习交流**（OpenCV 图像处理、窗口 API、自动化编程、GUI 开发）。

**严禁**用于：
- 破坏游戏平衡、扰乱游戏环境
- 商业盈利
- 违反游戏用户协议的操作

使用本项目造成的任何后果（包括账号封禁、财产损失）由使用者自行承担。作者不对任何直接或间接损失负责。

---

## 📜 License

MIT License — 详见 [LICENSE](LICENSE)。

---

## 🙏 致谢

- 参考项目：[MapleStoryAutoLevelUp](https://github.com/KenYu910645/MapleStoryAutoLevelUp)（状态机架构、颜色路线导航思路）
- 依赖库：OpenCV、NumPy、windows_capture、pydirectinput、vncdotool

---

## 📮 反馈

- Issue：[GitHub Issues](../../issues)
- 讨论：[GitHub Discussions](../../discussions)