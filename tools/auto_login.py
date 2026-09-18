# -*- coding: utf-8 -*-
import re
import time
import random
import cv2
import numpy as np
import mss
import win32gui
import pydirectinput
import easyocr
import pyperclip

# ================= 用户配置区 =================
# 请按照你的需求修改以下配置
CONFIG = {
    "game_title": "冒险岛怀旧服",  # 游戏窗口标题（部分匹配）
    "account": "17620101069",  # 指定的账号
    "server_name": "小白兔",  # 指定的服务器名称
    "channel_mode": "specify",  # 指定频道填 "specify"，随机频道填 "random"
    "specify_channel": 2,  # 指定的频道号（如 5 代表频道 5）
    "character_mode": "index",  # 指定角色填 "index"（按顺序，从1开始），指定角色名填 "name"
    "character_index": 1,  # 角色索引，1 代表第一个角色
    "character_name": "兔兔宝贝打蘑菇",  # 如果 character_mode 为 "name"，则使用该名称
}

# 红框区域的相对坐标（根据你的屏幕分辨率，这里是1366x768的估算值）
# 格式 [left, top, width, height]
CHANNEL_BOX = [520, 340, 300, 230]
# ==============================================

# 初始化 OCR 引擎（识别文字用，首次运行会下载模型，请耐心等待）
print("[*] 正在加载 OCR 模型...")
reader = easyocr.Reader(['ch_sim', 'en'], gpu=False)


class GameBot:
    def __init__(self, title):
        self.sct = mss.mss()
        self.hwnd = self._find_window(title)
        self.rect = self._get_client_rect()
        print(f"[+] 成功绑定窗口: {title}，客户区坐标: {self.rect}")
        pydirectinput.PAUSE = 0.1

    def _find_window(self, substr):
        hits = []

        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd)
                if t and substr in t and "cmd.exe" not in t and "python" not in t:
                    hits.append((hwnd, t))
            return True

        win32gui.EnumWindows(cb, None)
        if not hits:
            raise RuntimeError(f"未找到标题包含 '{substr}' 的窗口")
        return hits[0][0]

    def _get_client_rect(self):
        l, t, r, b = win32gui.GetClientRect(self.hwnd)
        sl, st = win32gui.ClientToScreen(self.hwnd, (0, 0))
        return {"left": sl, "top": st, "width": r - l, "height": b - t}

    def capture(self):
        """截取游戏客户区画面"""
        shot = np.asarray(self.sct.grab(self.rect))
        return cv2.cvtColor(shot[..., :3], cv2.COLOR_BGR2RGB)  # 转为RGB给easyocr用

    def capture_bgr(self):
        """截取游戏客户区画面（BGR，给OpenCV用）"""
        shot = np.asarray(self.sct.grab(self.rect))
        return shot[..., :3].copy()

    def click(self, x, y, double=False):
        """在窗口客户区相对坐标 (x, y) 处点击"""
        screen_x = self.rect["left"] + x
        screen_y = self.rect["top"] + y
        pydirectinput.moveTo(screen_x, screen_y)
        time.sleep(0.1)
        pydirectinput.click()
        if double:
            time.sleep(0.05)
            pydirectinput.click()
        print(f"    -> 点击坐标: ({x}, {y}) {'双击' if double else ''}")

    def press(self, key):
        pydirectinput.press(key)
        print(f"    -> 按键: {key}")

    def wait_image(self, template_path, timeout=10, threshold=0.8):
        """等待图像出现，返回中心坐标，超时返回None"""
        start = time.time()
        while time.time() - start < timeout:
            screen = self.capture_bgr()
            template = cv2.imread(template_path)
            if template is None:
                print(f"[!] 警告: 找不到模板图片 {template_path}")
                return None
            res = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= threshold:
                h, w = template.shape[:2]
                return (max_loc[0] + w // 2, max_loc[1] + h // 2)
            time.sleep(0.5)
        return None

    # ================= 业务逻辑 =================

    def step1_login(self):
        """步骤1：识别账号并点击连接"""
        print("[*] 步骤1：账号登录")
        time.sleep(2)
        # 截取画面进行 OCR 识别
        screen = self.capture()
        results = reader.readtext(screen)

        target_account = CONFIG["account"]
        found = False
        for (bbox, text, prob) in results:
            clean_text = text.replace(" ", "")
            if target_account in clean_text:
                print(f"    -> 识别到目标账号: {text}")
                found = True
                break

        if not found:
            print(f"    [!] 未识别到指定账号 {target_account}，尝试清除并重新输入...")
            # 点击 X 按钮清除（这里需要你根据实际画面坐标调整，或者通过模板匹配找 X 按钮）
            # 假设 X 按钮在 (990, 250) 附近
            self.click(990, 250)
            time.sleep(0.5)
            # 粘贴账号
            pyperclip.copy(target_account)
            pydirectinput.hotkey('ctrl', 'v')
            time.sleep(0.5)

        # 找到“连接”按钮并点击（这里用相对坐标，你需要根据截图估算）
        # 根据你的图1，“连接”按钮大概在 (860, 400)
        self.click(860, 400)
        print("[+] 已点击连接，等待进入服务器选择...")
        time.sleep(3)

    def step2_select_server(self):
        """步骤2：选择服务器"""
        print("[*] 步骤2：选择服务器")
        target_server = CONFIG["server_name"]
        time.sleep(2)

        screen = self.capture()
        results = reader.readtext(screen)

        for (bbox, text, prob) in results:
            if target_server in text:
                # 计算文字中心点
                top_left = bbox[0]
                bottom_right = bbox[2]
                cx = int((top_left[0] + bottom_right[0]) / 2)
                cy = int((top_left[1] + bottom_right[1]) / 2)
                print(f"    -> 找到服务器 '{target_server}'，坐标 ({cx}, {cy})")
                self.click(cx, cy)
                break
        else:
            print(f"    [!] 未找到服务器 '{target_server}'，尝试使用默认坐标点击")
            # 默认点小白兔的坐标（根据图2估算）
            self.click(832, 167)

        print("[+] 已选择服务器，等待进入频道选择...")
        time.sleep(3)

    def _get_cell_center(self, row_idx, col_idx):
        """根据行列索引（从0开始）计算频道单元格在屏幕上的中心坐标"""
        box = CHANNEL_BOX
        # 假设表格是 4列5行
        cols, rows = 4, 5
        cell_w = box[2] // cols
        cell_h = box[3] // rows

        # 计算相对于客户区的坐标
        cx = box[0] + col_idx * cell_w + cell_w // 2
        cy = box[1] + row_idx * cell_h + cell_h // 2
        return cx, cy

    def step3_select_channel(self):
        """步骤3：选择频道（基于固定表格数学计算，不使用 OCR）"""
        print("[*] 步骤3：选择频道")
        time.sleep(2)

        box = CHANNEL_BOX
        cols, rows = 4, 5  # 4列5行
        mode = CONFIG["channel_mode"]

        if mode == "specify":
            target_ch = CONFIG["specify_channel"]
            print(f"    -> 目标频道: 频道{target_ch}")

            if not (1 <= target_ch <= 100):
                print(f"[!] 频道号 {target_ch} 超出常规范围 (1-100)")
                return

            # 1. 计算需要滚动的次数
            # 初始首行是 1，每滚动一次首行 +4
            scroll_times = (target_ch - 1) // 4

            # 2. 执行滚动
            if scroll_times > 0:
                print(f"    -> 需要向下滚动 {scroll_times} 次")
                # 鼠标必须先移动到表格内部才能触发滚动
                box_cx = box[0] + box[2] // 2
                box_cy = box[1] + box[3] // 2
                pydirectinput.moveTo(self.rect["left"] + box_cx, self.rect["top"] + box_cy)
                time.sleep(0.5)

                for i in range(scroll_times):
                    self.scroll(-3)  # 向下滚动
                    print(f"       -> 第 {i + 1} 次滚动完成")
                    time.sleep(0.6)  # 等待滚动动画结束
            else:
                print("    -> 目标频道在初始视图中，无需滚动")

            # 3. 计算目标频道在滚动后的表格中的位置
            # 滚动后的首行频道号
            first_ch_in_view = scroll_times * 4 + 1
            # 相对偏移量
            offset = target_ch - first_ch_in_view

            row_idx = offset // 4
            col_idx = offset % 4

            # 安全校验，防止计算出界（比如滚动后目标频道在屏幕外）
            if not (0 <= row_idx < rows and 0 <= col_idx < cols):
                print(f"[!] 计算出错: 目标频道 {target_ch} 不在预期的表格位置内")
                return

            # 4. 计算绝对坐标并双击
            cx, cy = self._get_cell_center(row_idx, col_idx)
            print(f"    -> 目标频道位于第 {row_idx + 1} 行，第 {col_idx + 1} 列，坐标 ({cx}, {cy})")

            self.click(cx, cy, double=True)

        elif mode == "random":
            print("    -> 随机频道模式")
            # 随机选择 1 到 20 之间的频道（初始视图内的）
            random_ch = random.randint(1, 20)
            print(f"    -> 随机选中频道{random_ch}")

            # 计算初始视图中的行列
            offset = random_ch - 1
            row_idx = offset // 4
            col_idx = offset % 4

            cx, cy = self._get_cell_center(row_idx, col_idx)
            self.click(cx, cy, double=True)

        print("[+] 频道选择完成，等待进入角色选择...")
        time.sleep(3)

    def step4_select_character(self):
        """步骤4：选择角色"""
        print("[*] 步骤4：选择角色")
        time.sleep(2)

        mode = CONFIG["character_mode"]

        if mode == "index":
            idx = CONFIG["character_index"]
            # 角色1, 2, 3 的固定坐标（根据图4估算）
            char_coords = {
                1: (598, 490),
                2: (736, 490),
                3: (873, 490)
            }
            if idx in char_coords:
                cx, cy = char_coords[idx]
                print(f"    -> 选择第 {idx} 个角色，坐标 ({cx}, {cy})")
                self.click(cx, cy)
            else:
                print(f"    [!] 无效的角色索引 {idx}，默认选择第1个")
                self.click(598, 490)

        elif mode == "name":
            target_name = CONFIG["character_name"]
            print(f"    -> 目标角色名: {target_name}")
            screen = self.capture()
            results = reader.readtext(screen)

            for (bbox, text, prob) in results:
                if target_name in text.replace(" ", ""):
                    top_left = bbox[0]
                    bottom_right = bbox[2]
                    cx = int((top_left[0] + bottom_right[0]) / 2)
                    cy = int((top_left[1] + bottom_right[1]) / 2)
                    print(f"    -> 找到角色 '{target_name}'，坐标 ({cx}, {cy})")
                    self.click(cx, cy)
                    break
            else:
                print(f"    [!] 未识别到角色名 '{target_name}'，默认点击第1个角色")
                self.click(598, 490)

        time.sleep(0.5)
        print("[+] 已选择角色，按 Enter 进入游戏")
        self.press('enter')


# ================= 主程序 =================

def main():
    bot = GameBot(CONFIG["game_title"])

    try:
        bot.step1_login()
        bot.step2_select_server()
        bot.step3_select_channel()
        bot.step4_select_character()
        print("[🎉] 自动登录流程全部完成！")
    except Exception as e:
        print(f"[❌] 发生错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()