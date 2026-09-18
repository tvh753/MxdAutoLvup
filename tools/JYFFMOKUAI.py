# 更新日期2025.11.22
import random
import time
import math
from vncdotool import api
import cv2
import numpy as np
import ctypes
import os
from ctypes import wintypes
from ctypes import *
from typing import List, Any
# from win32 import win32gui, win32api
import win32gui
import pyautogui
from wechat_ocr.ocr_manager import OcrManager, OCR_MAX_TASK_ID


def find_top_windows_exactly(win_title=None, win_class=None):
    """
    通过完整的窗口标题或类名，准确查找顶级窗口句柄，以列表形式返回，列表中字典的键win_title表示顶级窗口标题和键win_hwnd表示顶级窗口句柄
    :param win_title: 完整的窗口标题
    :param win_class: 完整的窗口类名
    :return: 以列表形式返回，列表中字典的键win_title表示顶级窗口标题和键win_hwnd表示顶级窗口句柄
    """
    win_hwnd: List[Any] = []

    def callback(hwnd, extra):
        # 顶级窗口的句柄和标题
        get_class_name = win32gui.GetClassName(hwnd)
        get_win_title = win32gui.GetWindowText(hwnd)
        if win_title is not None and win_class is not None:
            if win_class == get_class_name and win_title == get_win_title:
                title_hwnd = {'win_title': get_win_title, 'win_hwnd': hwnd}
                win_hwnd.append(title_hwnd)
        elif win_title is not None:
            if win_title == get_win_title:
                title_hwnd = {'win_title': get_win_title, 'win_hwnd': hwnd}
                win_hwnd.append(title_hwnd)
        elif win_class is not None:
            if win_class == get_class_name:
                title_hwnd = {'win_title': get_win_title, 'win_hwnd': hwnd}
                win_hwnd.append(title_hwnd)

    if win_title is None and win_class is None:
        win_hwnd.append(0)
        return win_hwnd
    else:
        win32gui.EnumWindows(callback, None)
    return win_hwnd


# fuzzy
def find_top_windows_fuzzy(win_title=None, win_class=None):
    """
    通过模糊的窗口标题或类名，模糊匹配所有符合条件窗口的句柄，以列表形式返回，列表中字典的键win_title表示顶级窗口标题和键win_hwnd表示顶级窗口句柄
    :param win_title: 模糊的窗口标题
    :param win_class: 模糊的窗口类名
    :return: 以列表形式返回，列表中字典的键win_title表示顶级窗口标题和键win_hwnd表示顶级窗口句柄
    """
    win_hwnd: List[Any] = []

    def callback(hwnd, extra):
        # 顶级窗口的句柄和标题
        get_class_name = win32gui.GetClassName(hwnd)
        get_win_title = win32gui.GetWindowText(hwnd)
        if win_title is not None and win_class is not None:
            if win_title in get_win_title and win_class in get_class_name:
                title_hwnd = {'win_title': get_win_title, 'win_hwnd': hwnd}
                win_hwnd.append(title_hwnd)
        elif win_title is not None:
            if win_title in get_win_title:
                title_hwnd = {'win_title': get_win_title, 'win_hwnd': hwnd}
                win_hwnd.append(title_hwnd)
        elif win_class is not None:
            if win_class in get_class_name:
                title_hwnd = {'win_title': get_win_title, 'win_hwnd': hwnd}
                win_hwnd.append(title_hwnd)

    if win_title is None and win_class is None:
        win_hwnd.append(0)
        return win_hwnd
    else:
        win32gui.EnumWindows(callback, None)
    return win_hwnd


class jy_vnc:
    def __init__(self):
        self.ocr_manager = None
        self.ocr_result = None
        self.path_name = None
        self.client = None
        self.x = 0
        self.y = 0
        # v25: screenshot_np 用的固定临时文件（延迟创建，避免每次 mkstemp）
        self._tmp_np_path = None

    def vnc_connect(self, vnc_host, vnc_port, vnc_password=None):
        """
        vnc方式链接，成功返回1,并将鼠标初始位置设为(5,5)，失败返回0，请确保链接后，不要人为移动鼠标。如果人为移动，请断开链接后，重新链接

        :param vnc_host: 字符串类型，链接地址本机链接虚拟机一般为'127.0.0.1'
        :param vnc_port: 整数型，端口号，比如5900
        :param vnc_password: 字符串类型，vnc链接密码
        :return: 成功返回1，失败返回0
        """
        if vnc_password:
            self.client = api.connect(f'{vnc_host}::{vnc_port}', password=vnc_password)
        else:
            self.client = api.connect(f'{vnc_host}::{vnc_port}')
        if self.client is None:
            return 0
        else:
            self.x = 5
            self.y = 5
            self.client.mouseMove(self.x, self.y)
            return 1

    def vnc_set_ini(self, path_name):
        """
        初始化基本参数
        :param path_name: 字符串类型， 本地保存的全路径，注意包含图片的保存名称和后缀。比如c:/a.png
        :return: 无
        """
        self.path_name = path_name

    def vnc_capture_screen(self):
        """
        通过vnc方式全屏截图，即根据虚拟机屏幕大小，截取整个虚拟机的屏幕，并保存到本地。无返回值
        :return:无返回值，判断是否成功，请到参数设置的路径下查看有没有图片
        """
        self.client.captureScreen(self.path_name)

    def screenshot_np(self):
        """v25: 直接返回 BGR ndarray（OpenCV 直接用），不落盘到用户目录

        相比 vnc_capture_screen（只存文件）和 cv2.imread 读回，
        本方法利用 vncdotool 的内部副作用：
          captureScreen(filename) 会先把 framebuffer 刷进
          self.client.screen（PIL Image），再 save 到文件。
        我们借这个副作用：触发一次保存（保证 framebuffer 已更新），
        然后直接从 client.screen 读内存数据 —— 比 cv2.imread 少一次
        真正的磁盘读取，且不污染用户可感知的文件。

        返回：BGR ndarray；失败返回 None
        """
        if self.client is None:
            return None
        if self._tmp_np_path is None:
            import tempfile
            fd, self._tmp_np_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
        try:
            # 触发一次 framebuffer 刷新（同步阻塞，返回时 screen 已更新）
            self.client.captureScreen(self._tmp_np_path)
            scr = getattr(self.client, "screen", None)
            if scr is None:
                # 兜底：从临时文件读
                return cv2.imread(self._tmp_np_path)
            arr = np.array(scr)
            if arr.ndim == 2:  # 灰度兜底
                return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
            # PIL Image 是 RGB，转成 OpenCV 用的 BGR
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        except Exception as e:
            print(f"[jy_vnc.screenshot_np] 截图异常: {e}")
            return None

    def vnc_capture_region(self, x1, y1, x2, y2):
        """
        通过vnc方式区域截图，无返回值
        :param x1: 整数型，起始坐标x1
        :param y1: 整数型，起始坐标y1
        :param x2: 整数型，终止坐标x2
        :param y2: 整数型，终止坐标y2
        :return:无返回值，判断是否成功，请到参数（path_name）设置的路径下查看有没有图片
        """
        # x_p = x2 - x1
        # y_p = y2 - y1
        # self.client.captureRegion(path_name, x1, y1, x_p, y_p)  # 区域截图
        self.client.captureRegion(self.path_name, x1, y1, x2 - x1, y2 - y1)

    def vnc_findpic(self, x1, y1, x2, y2, img_template, similarity, method=0):
        """
        模板匹配找图，返回符合相似度要求,且相似度最高的那个中心点位置坐标, 如果没有合适位置，则返回0，注意范围要比参数图片的尺寸大
        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :param img_template: 要找的图片的全路径
        :param similarity: 相似度
        :param method: 找图方法，其中0 和 1表示越是接近1越相似，2表示越是接近0越相似
                                0 代表：cv2.TM_CCORR_NORMED：标准相似匹配，匹配值范围在0到1之间，匹配值越大表示两个图像越相似，其中1表示完美匹配，0表示没有匹配。
                                1 代表：cv2.TM_CCOEFF_NORMED：标准相关系数匹配，匹配值的范围是-1到1之间。匹配值越接近1，表示两个图像越相似
                                2 代表：cv2.TM_SQDIFF_NORMED：标准平方差匹配。匹配值的范围是0到1。匹配值越小表示两个图像越相似,完全匹配结果为0
        :return: 返回符合相似度要求,且相似度最高的那个中心点位置坐标, 如果没有合适位置，则返回0
        """

        def get_max(_template_res):
            g_minvalue, g_maxvalue, g_minloc, g_maxloc = cv2.minMaxLoc(template_res)
            if g_maxvalue >= similarity:
                g_maxloc_center_point = (g_maxloc[0] + x1 + width // 2, g_maxloc[1] + y1 + height // 2)
                return g_maxloc_center_point
            else:
                return 0

        self.vnc_capture_region(x1, y1, x2, y2)
        find_img = cv2.imread(self.path_name)
        img_template = cv2.imread(img_template)
        height, width, s = img_template.shape
        if method == 0:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCORR_NORMED)
            maxloc_center_point = get_max(template_res)
            return maxloc_center_point

        elif method == 1:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCOEFF_NORMED)
            maxloc_center_point = get_max(template_res)
            return maxloc_center_point
        elif method == 2:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_SQDIFF_NORMED)
            minvalue, maxvalue, minloc, maxloc = cv2.minMaxLoc(template_res)
            if minvalue <= similarity:
                min_loc_center_point = (minloc[0] + x1 + width // 2, minloc[1] + y1 + height // 2)
                return min_loc_center_point
            else:
                return 0

    def vnc_findpicEx(self, x1, y1, x2, y2, img_template, similarity, method=0):
        """
        模板匹配找图，以列表的形式，返回所有符合相似度要求的中心点位置坐标, 如果没有合适位置，则返回空列表，注意范围要比参数图片的尺寸大
        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :param img_template: 要找的图片的全路径
        :param similarity: 相似度
        :param method: 找图方法，其中0 和 1表示越是接近1越相似，2表示越是接近0越相似
                                0 代表：cv2.TM_CCORR_NORMED：标准相似匹配，匹配值范围在0到1之间，匹配值越大表示两个图像越相似，其中1表示完美匹配，0表示没有匹配。
                                1 代表：cv2.TM_CCOEFF_NORMED：标准相关系数匹配，匹配值的范围是-1到1之间。匹配值越接近1，表示两个图像越相似
                                2 代表：cv2.TM_SQDIFF_NORMED：标准平方差匹配。匹配值的范围是0到1。匹配值越小表示两个图像越相似,完全匹配结果为0
        :return:以列表的形式，返回所有符合相似度要求的中心点位置坐标
        """
        self.vnc_capture_region(x1, y1, x2, y2)
        find_img = cv2.imread(self.path_name)
        img_template = cv2.imread(img_template)
        height, width, s = img_template.shape
        hypotenuse_sqr = width ** 2 + height ** 2
        res = None
        if method == 0:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCORR_NORMED)
            res = np.where(template_res > similarity)
        elif method == 1:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCOEFF_NORMED)
            res = np.where(template_res > similarity)
        elif method == 2:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_SQDIFF_NORMED)
            res = np.where(template_res < similarity)
        res_zip = zip(res[0], res[1])
        center_point_list = []
        for a in res_zip:
            b = a[::-1]
            center_point = (x1 + b[0] + width // 2, y1 + b[1] + height // 2)
            if len(center_point_list) == 0:
                center_point_list.append(center_point)
            else:
                not_append = False
                for i in center_point_list:
                    hypotenuse_sqr_point = (center_point[0] - i[0]) ** 2 + (center_point[1] - i[1]) ** 2
                    if hypotenuse_sqr_point <= hypotenuse_sqr:
                        not_append = True
                if not not_append:
                    center_point_list.append(center_point)

        return center_point_list

    def vnc_start_ocr(self, wechat_ocr_file, wechat_dll_file):
        """
        启动ocr
        :param wechat_ocr_file: 设置WeChatOCR.EXE 全路径，如'ocr/WeChatOCR.EXE'
        :param wechat_dll_file: 设置 mmmojo.dll mmmojo_64.dll路径
        :return: 无
        """

        def ocr_call_back(img_file, results):
            self.ocr_result = results

        # 实例化OcrManager类
        ocr_manager = OcrManager(wechat_dll_file)
        # 设置WeChatOCR.EXE的路径
        ocr_manager.SetExePath(wechat_ocr_file)
        # 设置dll文件路径
        ocr_manager.SetUsrLibDir(wechat_dll_file)
        # 设置回调函数
        ocr_manager.SetOcrResultCallback(ocr_call_back)
        # 启动ocr
        ocr_manager.StartWeChatOCR()
        self.ocr_manager = ocr_manager
        return self.ocr_manager

    def vnc_findtext(self, x1, y1, x2, y2):
        """
        前台模式，本地ocr图片识字，使用前，必须调用start_ocr命令启动ocr服务。以列表形式，返回识别到的字的[内容，x坐标，y坐标]
        使用此中ocr识字前，必须链接虚拟机
        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :return: 以列表形式，返回识别到的字的[内容，x坐标，y坐标]
        """

        def make_results():
            results = self.ocr_result
            if len(results['ocrResult']) > 0:
                ocr_text = results['ocrResult'][0]['text']
                text_left_x = results['ocrResult'][0]['location']['left']
                text_left_y = results['ocrResult'][0]['location']['top']
                text_right_x = results['ocrResult'][0]['location']['right']
                text_right_y = results['ocrResult'][0]['location']['bottom']

                if text_left_x is None:
                    text_left_x = 0
                if text_left_y is None:
                    text_left_y = 0
                if text_right_x is None:
                    text_right_x = 0
                if text_right_y is None:
                    text_right_y = 0
                text_center_x = (text_right_x - text_left_x) // 2
                text_center_y = (text_right_y - text_left_y) // 2
                text_center_x = x1 + text_center_x + text_left_x
                text_center_y = y1 + text_center_y + text_left_y
                ocr_res = [ocr_text, text_center_x, text_center_y]
                return ocr_res

        self.vnc_capture_region(x1, y1, x2, y2)
        # 开始识别图片中的文字
        self.ocr_manager.DoOCRTask(self.path_name)
        while self.ocr_manager.m_task_id.qsize() != OCR_MAX_TASK_ID:
            pass
        orc_ress = make_results()
        os.remove(self.path_name)
        return orc_ress

    def vnc_findtext2(self, x1, y1, x2, y2, img_path):
        """
        前台模式，本地ocr图片识字，使用前，必须调用start_ocr命令启动ocr服务。以列表形式，返回识别到的字的[内容，x坐标，y坐标]
        使用此中ocr识字前，不用链接虚拟机，只需要提供你要识别的图片路径即可
        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :param img_path: 图片路径
        :return: 以列表形式，返回识别到的字的[内容，x坐标，y坐标]
        """

        def make_results():
            results = self.ocr_result
            if len(results['ocrResult']) > 0:
                ocr_text = results['ocrResult'][0]['text']
                text_left_x = results['ocrResult'][0]['location']['left']
                text_left_y = results['ocrResult'][0]['location']['top']
                text_right_x = results['ocrResult'][0]['location']['right']
                text_right_y = results['ocrResult'][0]['location']['bottom']

                if text_left_x is None:
                    text_left_x = 0
                if text_left_y is None:
                    text_left_y = 0
                if text_right_x is None:
                    text_right_x = 0
                if text_right_y is None:
                    text_right_y = 0
                text_center_x = (text_right_x - text_left_x) // 2
                text_center_y = (text_right_y - text_left_y) // 2
                text_center_x = x1 + text_center_x + text_left_x
                text_center_y = y1 + text_center_y + text_left_y
                ocr_res = [ocr_text, text_center_x, text_center_y]
                return ocr_res

        # self.vnc_capture_region(x1, y1, x2, y2)
        # 开始识别图片中的文字
        self.ocr_manager.DoOCRTask(img_path)
        while self.ocr_manager.m_task_id.qsize() != OCR_MAX_TASK_ID:
            pass
        orc_ress = make_results()
        os.remove(img_path)
        return orc_ress

    def vnc_close_ocr(self):
        """
        关闭ocr服务
        :return: 无
        """
        # 结束识别，关闭服务
        self.ocr_manager.KillWeChatOCR()


    def generate_points(self, x1, y1, x2, y2, num_points):
        points = []

        for i in range(num_points):
            # 计算当前点的位置
            t = i / (num_points - 1)
            # 使用平方根函数来调整步长，使得初始步长小，接近终点步长大
            adjusted_t = t ** 0.5  # 这里使用平方根函数来调整步长
            x = x1 + adjusted_t * (x2 - x1)
            y = y1 + adjusted_t * (y2 - y1)
            points.append((x + random.randint(-5, 12), y + random.randint(-3, 15)))

        # points[len(points) - 1] = (x2, y2)
        points.append((x2, y2))
        return points

    def vnc_mouse_real_move(self, x2, y2, keep_time=0.03, max_step=3):
        """
        模拟真实鼠标轨迹,移动鼠标到某个位置

        :param x2: 终点坐标x
        :param y2: 终点坐标y
        :param keep_time: 移动轨迹中的时间间隔，默认为0.03
        :param max_step: 最大步长，默认为3
        :return: 无
        """
        # 定义贝塞尔曲线的4个关键点
        chazhi_x = abs(self.x - x2)
        chazhi_y = abs(self.y - y2)

        juli = math.sqrt(chazhi_x * chazhi_x + chazhi_y * chazhi_y)

        dianshuliang = int(juli / max_step)
        time_intervals = [keep_time / 100]

        if dianshuliang > 1:
            for i in range(dianshuliang, 1, -2):
                # 点之间的时间间隔（单位：秒），这里先快速移动，再逐渐减速
                time_intervals.append(keep_time / (i + 1))

        time_intervals.append(keep_time)
        # print("self.x,self.y",self.x,self.y,dianshuliang)
        control_points = self.generate_points(self.x, self.y, x2, y2, dianshuliang)

        # print(control_points)
        # 贝塞尔函数，参数是控制点、起点、终点、时间参数（0 <= t <= 1）
        def bezier_curve(x_1, y_1, x_2, y_2, cx1, cy1, cx2, cy2, t_):
            return (
                (1 - t_) ** 3 * x_1 + 3 * (1 - t_) ** 2 * t_ * cx1 + 3 * (1 - t_) * t_ ** 2 * cx2 + t_ ** 3 * x_2,
                (1 - t_) ** 3 * y_1 + 3 * (1 - t_) ** 2 * t_ * cy1 + 3 * (1 - t_) * t_ ** 2 * cy2 + t_ ** 3 * y_2
            )

        # total_time = sum(time_intervals)
        for i in range(len(control_points)):
            t = i / (len(control_points) - 1)
            if i < len(control_points) - 1:
                x, y = bezier_curve(control_points[i][0], control_points[i][1], control_points[i + 1][0],
                                    control_points[i + 1][1],
                                    control_points[i][0], control_points[i][1], control_points[i + 1][0],
                                    control_points[i + 1][1], t)

            else:
                x, y = control_points[-1]  # 使用列表的最后一个元素作为终点坐标

            self.vnc_mouse_move(int(x), int(y))
            # time.sleep(delay)
            time.sleep(keep_time)

    def vnc_mouse_move_press(self, x, y, sleep_time=0.03, i=1, is_real=0, max_step=30):
        """
        通过vnc方式实现 鼠标移至点击，移至某个坐标后，延迟一定时间进行鼠标点击，无返回值


        :param x: 整数类型，x坐标
        :param y: 整数类型，y坐标
        :param sleep_time: 数字类型，移动到某个位置后，等待时间（单位秒） 默认为0.03秒，
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :param is_real: 整数类型，是否开启鼠标轨迹，模拟真实鼠标移动过程，0：代表不开启，1：代表开启，默认为0，不开启。
        :param max_step: 整数类型，最大步长，如果没有开启鼠标轨迹，将不会起作用，默认为3
        :return: 无返回值
        """

        if is_real == 0:
            self.client.mouseMove(x, y)
            time.sleep(sleep_time)
            self.client.mousePress(i)
        else:
            self.vnc_mouse_real_move(x, y, sleep_time, max_step)
            time.sleep(0.03)
            self.client.mousePress(i)
        self.x = x
        self.y = y

    def vnc_mouse_move_double_press(self, x, y, sleep_time=0.03, i=1, is_real=0, max_step=30):
        """
        通过vnc方式实现 鼠标移至点击，移至某个坐标后，延迟一定时间进行鼠标点击，无返回值


        :param x: 整数类型，x坐标
        :param y: 整数类型，y坐标
        :param sleep_time: 数字类型，移动到某个位置后，等待时间（单位秒） 默认为0.03秒，
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :param is_real: 整数类型，是否开启鼠标轨迹，模拟真实鼠标移动过程，0：代表不开启，1：代表开启，默认为0，不开启。
        :param max_step: 整数类型，最大步长，如果没有开启鼠标轨迹，将不会起作用，默认为3
        :return: 无返回值
        """

        if is_real == 0:
            self.client.mouseMove(x, y)
            time.sleep(sleep_time)
            self.client.mousePress(i)
            self.client.mousePress(i)
        else:
            self.vnc_mouse_real_move(x, y, sleep_time, max_step)
            time.sleep(0.03)
            self.client.mousePress(i)
            self.client.mousePress(i)
        self.x = x
        self.y = y

    def vnc_mouse_down(self, i=1):
        """
        通过vnc方式实现 按下鼠标，无返回值
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :return: 无返回值
        """
        self.client.mouseDown(i)

    def vnc_mouse_up(self, i=1):
        """
        通过vnc方式实现 弹起鼠标，无返回值
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :return: 无返回值
        """
        self.client.mouseUp(i)

    def vnc_mouse_move(self, x, y):
        """
        通过vnc方式实现 移动鼠标到某点坐标
        :param x: x坐标
        :param y: y坐标
        :return: 无
        """
        self.x = x
        self.y = y
        self.client.mouseMove(self.x, self.y)

    def vnc_key_press(self, key_name):
        """
        通过vnc方式实现单击键盘
        :param key_name: 字符串类型，要按的键盘的字母名称，比如字母'a' 'f1'
        :return: 无返回值
        """
        self.client.keyPress(key_name)

    def vnc_key_down(self, key_name):
        """
        通过vnc方式实现 按下 键盘上的某个键
        :param key_name: 字符串类型，要按的键盘的字母名称，比如字母'a'  'f1'
        :return: 无返回值
        """
        self.client.keyDown(key_name)

    def vnc_key_up(self, key_name):
        """
        通过vnc方式实现 弹起 键盘上的某个键
        :param key_name: 字符串类型，要按的键盘的字母名称，比如字母'a'  'f1'
        :return: 无返回值
        """
        self.client.keyUp(key_name)


    def vnc_disconnect(self):
        """断开 vnc 链接，并清理 screenshot_np 的临时文件"""
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None
        if self._tmp_np_path and os.path.isfile(self._tmp_np_path):
            try:
                os.remove(self._tmp_np_path)
            except Exception:
                pass
        self._tmp_np_path = None


# 驱动键鼠d
class jy_driver_d:
    def __init__(self):
        self.y_offset = None
        self.x_offset = None
        self.hwnd = None
        self.y = None
        self.x = None
        self.driver = None
        self.left = 0
        self.top = 0
        self.ocr_manager = None
        self.ocr_result = None
        self.vk = {'esc': 100, 'f1': 101, 'f2': 102, 'f3': 103, 'f4': 104, 'f5': 105, 'f6': 106, 'f7': 107, 'f8': 108,
                   'f9': 109, 'f10': 100, 'f11': 101, 'f12': 103,
                   '`': 200, '1': 201, '2': 202, '3': 203, '4': 204, '5': 205, '6': 206, '7': 207, '8': 208, '9': 209,
                   '0': 210,
                   '-': 211, '=': 212, '\\': 213, 'bsp': 214,
                   'tab': 300, 'q': 301, 'w': 302, 'e': 303, 'r': 304, 't': 305, 'y': 306, 'u': 307, 'i': 308, 'o': 309,
                   'p': 310, '[': 311, ']': 312, 'enter': 313,
                   'caps': 400, 'a': 401, 's': 402, 'd': 403, 'f': 404, 'g': 405, 'h': 406, 'j': 407, 'k': 408,
                   'l': 409, ';': 410, "'": 411,
                   'shift': 500, 'z': 501, 'x': 502, 'c': 503, 'v': 504, 'b': 505, 'n': 506, 'm': 507, ',': 508,
                   '.': 509, '/': 510,
                   'ctrl': 600, 'win': 601, 'alt': 602, 'space': 603,
                   'del': 706, 'up': 709, 'down': 711, 'left': 710, 'right': 712,
                   '*': 812, '+': 814
                   }

    def get_window_rect(self, _hwnd, _is_first):
        """
        获取窗口的位置和大小
        :return: 返回一个包含窗口左上角和右下角坐标的元组 (left, top, right, bottom)
        """
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(_hwnd, ctypes.byref(rect)):
            raise ctypes.WinError()
        if _is_first == 0:
            ctypes.windll.user32.SetForegroundWindow(_hwnd)
        return rect.left, rect.top, rect.right, rect.bottom

    def d_getwindowposition(self, hwnd, x_offset, y_offset, is_first=0):
        """
        获取窗口的左上角坐标
        :param x_offset: x方向的偏移
        :param y_offset: y方向的偏移
        :param hwnd: 窗口句柄
        :param is_first: 本次程序运行，是否第一次调用本命令，默认为0，即第一次，非0为不是第一次
        :return: 返回一个包含窗口左上角坐标的元组 (left, top)
        """

        self.left, self.top, right, bottom = self.get_window_rect(hwnd, is_first)
        self.hwnd = hwnd
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.left = self.left + x_offset
        self.top = self.top + y_offset
        return self.left, self.top

    def d_set_ini(self, path_name):
        """
        驱动初始化，成功返回1，失败返回0
        :param path_name: 驱动dll全路径，即使与脚本文件同一个安装路径，也需要写完整路径，比如：X:/XX/XX.dll
        :return:成功返回1，失败返回0
        """
        self.driver = windll.LoadLibrary(path_name)
        st = self.driver.DD_btn(0)
        if st == 1:
            print("驱动初始化成功！")
            self.x = self.left + random.randint(10, 20)
            self.y = self.top + random.randint(10, 20)
            self.driver.DD_mov(self.x, self.y)
            return 1
        else:
            print("驱动初始化失败，请尝试管理员身份运行！")
            return 0

    def generate_points(self, x1, y1, x2, y2, num_points):
        points = []

        for i in range(num_points):
            # 计算当前点的位置
            t = i / (num_points - 1)
            # 使用平方根函数来调整步长，使得初始步长小，接近终点步长大
            adjusted_t = t ** 0.5  # 这里使用平方根函数来调整步长
            x = x1 + adjusted_t * (x2 - x1)
            y = y1 + adjusted_t * (y2 - y1)
            points.append((x + random.randint(-5, 12), y + random.randint(-3, 15)))

        points.append((x2, y2))
        return points

    def d_mouse_real_move(self, x2, y2, keep_time=0.03, max_step=3, is_re_get_win=0):
        """
        模拟真实鼠标轨迹,移动鼠标到某个位置

        :param x2: 整数类型，终点坐标x **注意是相对与窗口的坐标x
        :param y2: 整数类型，终点坐标y **注意是相对与窗口的坐标x
        :param keep_time: 数字类型，移动轨迹中的时间间隔，默认为0.03
        :param max_step: 整数类型，最大步长，默认为3
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 无
        """
        if is_re_get_win == 1:
            self.d_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)
        x2 = x2 + self.left
        y2 = y2 + self.top
        # 定义贝塞尔曲线的4个关键点
        chazhi_x = abs(self.x - x2)
        chazhi_y = abs(self.y - y2)

        juli = math.sqrt(chazhi_x * chazhi_x + chazhi_y * chazhi_y)

        dianshuliang = int(juli / max_step)
        time_intervals = [keep_time / 100]

        if dianshuliang > 1:
            for i in range(dianshuliang, 1, -2):
                # 点之间的时间间隔（单位：秒），这里先快速移动，再逐渐减速
                time_intervals.append(keep_time / (i + 1))

        time_intervals.append(keep_time)
        # print("self.x,self.y",self.x,self.y,dianshuliang)
        control_points = self.generate_points(self.x, self.y, x2, y2, dianshuliang)

        # print(control_points)
        # 贝塞尔函数，参数是控制点、起点、终点、时间参数（0 <= t <= 1）
        def bezier_curve(x_1, y_1, x_2, y_2, cx1, cy1, cx2, cy2, t_):
            return (
                (1 - t_) ** 3 * x_1 + 3 * (1 - t_) ** 2 * t_ * cx1 + 3 * (1 - t_) * t_ ** 2 * cx2 + t_ ** 3 * x_2,
                (1 - t_) ** 3 * y_1 + 3 * (1 - t_) ** 2 * t_ * cy1 + 3 * (1 - t_) * t_ ** 2 * cy2 + t_ ** 3 * y_2
            )

        # total_time = sum(time_intervals)
        for i in range(len(control_points)):
            t = i / (len(control_points) - 1)
            if i < len(control_points) - 1:
                x, y = bezier_curve(control_points[i][0], control_points[i][1], control_points[i + 1][0],
                                    control_points[i + 1][1],
                                    control_points[i][0], control_points[i][1], control_points[i + 1][0],
                                    control_points[i + 1][1], t)

            else:
                x, y = control_points[-1]  # 使用列表的最后一个元素作为终点坐标

            self.driver.DD_mov(int(x), int(y))
            # time.sleep(delay)
            time.sleep(keep_time)

    # def mouse_real_move(self, x2, y2, keep_time=0.03, max_step=3):
    #     """
    #     模拟真实鼠标轨迹,移动鼠标到某个位置
    #
    #     :param x2: 整数类型，终点坐标x **注意是相对与窗口的坐标x
    #     :param y2: 整数类型，终点坐标y **注意是相对与窗口的坐标x
    #     :param keep_time: 数字类型，移动轨迹中的时间间隔，默认为0.03
    #     :param max_step: 整数类型，最大步长，默认为3
    #     :return: 无
    #     """
    #     # 定义贝塞尔曲线的4个关键点
    #     chazhi_x = abs(self.x - x2)
    #     chazhi_y = abs(self.y - y2)
    #
    #     juli = math.sqrt(chazhi_x * chazhi_x + chazhi_y * chazhi_y)
    #
    #     dianshuliang = int(juli / max_step)
    #     print("dianshuliang", dianshuliang)
    #     time_intervals = [keep_time / 100]
    #
    #     if dianshuliang > 1:
    #         for i in range(dianshuliang, 1, -2):
    #             # 点之间的时间间隔（单位：秒），这里先快速移动，再逐渐减速
    #             time_intervals.append(keep_time / (i + 1))
    #
    #     time_intervals.append(keep_time)
    #     # print("self.x,self.y",self.x,self.y,dianshuliang)
    #     control_points = self.generate_points(self.x, self.y, x2, y2, dianshuliang)
    #
    #     # print(control_points)
    #     # 贝塞尔函数，参数是控制点、起点、终点、时间参数（0 <= t <= 1）
    #     def bezier_curve(x_1, y_1, x_2, y_2, cx1, cy1, cx2, cy2, t_):
    #         return (
    #             (1 - t_) ** 3 * x_1 + 3 * (1 - t_) ** 2 * t_ * cx1 + 3 * (1 - t_) * t_ ** 2 * cx2 + t_ ** 3 * x_2,
    #             (1 - t_) ** 3 * y_1 + 3 * (1 - t_) ** 2 * t_ * cy1 + 3 * (1 - t_) * t_ ** 2 * cy2 + t_ ** 3 * y_2
    #         )
    #
    #     # total_time = sum(time_intervals)
    #     for i in range(len(control_points)):
    #         t = i / (len(control_points) - 1)
    #         if i < len(control_points) - 1:
    #             x, y = bezier_curve(control_points[i][0], control_points[i][1], control_points[i + 1][0],
    #                                 control_points[i + 1][1],
    #                                 control_points[i][0], control_points[i][1], control_points[i + 1][0],
    #                                 control_points[i + 1][1], t)
    #
    #         else:
    #             x, y = control_points[-1]  # 使用列表的最后一个元素作为终点坐标
    #
    #         self.driver.DD_mov(int(x), int(y))
    #         # time.sleep(delay)
    #         time.sleep(keep_time)

    def d_mouse_move_press(self, x, y, sleep_time=0.03, i=1, is_real=0, max_step=30, is_re_get_win=0):
        """
        驱动鼠标移动点击

        :param x: 整数类型，x坐标 **注意是相对与窗口的坐标x
        :param y: 整数类型，y坐标 **注意是相对与窗口的坐标y
        :param sleep_time: 数字类型，移动到某个位置后，等待时间（单位秒） 默认为0.03秒，
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :param is_real: 整数类型，是否开启鼠标轨迹，模拟真实鼠标移动过程，0：代表不开启，1：代表开启，默认为0，不开启。
        :param max_step: 整数类型，最大步长，如果没有开启鼠标轨迹，将不会起作用，默认为3
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 无返回值
        """
        if is_re_get_win == 1:
            self.d_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)

        if is_real == 0:
            x = x + self.left
            y = y + self.top
            self.driver.DD_mov(x, y)
            time.sleep(sleep_time)
            if i == 1:
                self.driver.DD_btn(1)
                time.sleep(0.03)
                self.driver.DD_btn(2)
            elif i == 2:
                self.driver.DD_btn(16)
                time.sleep(0.03)
                self.driver.DD_btn(32)
            elif i == 3:
                self.driver.DD_btn(4)
                time.sleep(0.03)
                self.driver.DD_btn(8)
        else:
            if x == self.x and y == self.y:
                self.x = self.x - random.randint(5, 10)
                self.y = self.y - random.randint(5, 10)
            self.d_mouse_real_move(x, y, sleep_time, max_step)
            time.sleep(sleep_time)
            if i == 1:
                self.driver.DD_btn(1)
                time.sleep(0.03)
                self.driver.DD_btn(2)
            elif i == 2:
                self.driver.DD_btn(16)
                time.sleep(0.03)
                self.driver.DD_btn(32)
            elif i == 3:
                self.driver.DD_btn(4)
                time.sleep(0.03)
                self.driver.DD_btn(8)
        self.x = x
        self.y = y
        time.sleep(0.03)

    def d_mouse_move_double_press(self, x, y, sleep_time=0.03, i=1, is_real=0, max_step=30, is_re_get_win=0):
        """
        驱动鼠标移动双击击

        :param x: 整数类型，x坐标 **注意是相对与窗口的坐标x
        :param y: 整数类型，y坐标 **注意是相对与窗口的坐标y
        :param sleep_time: 数字类型，移动到某个位置后，等待时间（单位秒） 默认为0.03秒，
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :param is_real: 整数类型，是否开启鼠标轨迹，模拟真实鼠标移动过程，0：代表不开启，1：代表开启，默认为0，不开启。
        :param max_step: 整数类型，最大步长，如果没有开启鼠标轨迹，将不会起作用，默认为3
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 无返回值
        """
        if is_re_get_win == 1:
            self.d_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)

        if is_real == 0:
            x = x + self.left
            y = y + self.top
            self.driver.DD_mov(x, y)
            time.sleep(sleep_time)
            if i == 1:
                self.driver.DD_btn(1)
                time.sleep(0.03)
                self.driver.DD_btn(2)
                time.sleep(0.04)
                self.driver.DD_btn(1)
                time.sleep(0.05)
                self.driver.DD_btn(2)
            elif i == 2:
                self.driver.DD_btn(16)
                time.sleep(0.03)
                self.driver.DD_btn(32)
                time.sleep(0.04)
                self.driver.DD_btn(16)
                time.sleep(0.05)
                self.driver.DD_btn(32)
            elif i == 3:
                self.driver.DD_btn(4)
                time.sleep(0.03)
                self.driver.DD_btn(8)
                time.sleep(0.04)
                self.driver.DD_btn(4)
                time.sleep(0.05)
                self.driver.DD_btn(8)
        else:
            if x == self.x and y == self.y:
                self.x = self.x - random.randint(5, 10)
                self.y = self.y - random.randint(5, 10)
            self.d_mouse_real_move(x, y, sleep_time, max_step)
            time.sleep(sleep_time)
            if i == 1:
                self.driver.DD_btn(1)
                time.sleep(0.03)
                self.driver.DD_btn(2)
                time.sleep(0.04)
                self.driver.DD_btn(1)
                time.sleep(0.05)
                self.driver.DD_btn(2)
            elif i == 2:
                self.driver.DD_btn(16)
                time.sleep(0.03)
                self.driver.DD_btn(32)
                time.sleep(0.04)
                self.driver.DD_btn(16)
                time.sleep(0.05)
                self.driver.DD_btn(32)
            elif i == 3:
                self.driver.DD_btn(4)
                time.sleep(0.03)
                self.driver.DD_btn(8)
                time.sleep(0.04)
                self.driver.DD_btn(4)
                time.sleep(0.05)
                self.driver.DD_btn(8)
        self.x = x
        self.y = y
        time.sleep(0.03)

    def d_mouse_move(self, x, y, is_re_get_win=0):
        """
        驱动鼠标移动到某个位置

        :param x: 整数类型，x坐标
        :param y: 整数类型，y坐标
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 无
        """
        if is_re_get_win == 1:
            self.d_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)
        x = x + self.left
        y = y + self.top
        self.x = x
        self.y = y
        self.driver.DD_mov(self.x, self.y)

    def d_mouse_down(self, i=1):
        """
        驱动鼠标按下
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :return: 无
        """
        if i == 1:
            self.driver.DD_btn(1)
        elif i == 2:
            self.driver.DD_btn(16)
        elif i == 3:
            self.driver.DD_btn(4)

    def d_mouse_up(self, i=1):
        """
        驱动鼠标弹起
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :return: 无
        """
        if i == 1:
            self.driver.DD_btn(2)
        elif i == 2:
            self.driver.DD_btn(32)
        elif i == 3:
            self.driver.DD_btn(8)
        time.sleep(0.03)

    # 键盘操作
    def d_key_up(self, key_name):
        """
        驱动键盘 按下 某键
        :param key_name: 键盘名称，对应键帽上的字符
        :return: 无
        """
        self.driver.DD_key(self.vk[key_name], 2)
        time.sleep(0.03)

    def d_key_down(self, key_name):
        """
        驱动键盘 弹起 某键
        :param key_name: 键盘名称，对应键帽上的字符
        :return: 无
        """
        self.driver.DD_key(self.vk[key_name], 1)

    def d_key_press(self, key_name):
        """
        驱动键盘 单击 某键
        :param key_name: 键盘名称，对应键帽上的字符
        :return: 无
        """
        # print(self.vk[key_name])
        self.driver.DD_key(self.vk[key_name], 1)
        time.sleep(0.03)
        self.driver.DD_key(self.vk[key_name], 2)
        time.sleep(0.03)

    def d_capture(self, x1, y1, x2, y2, is_re_get_win=0):
        """
        前台模式截图
        :param x1: 起始坐标x
        :param y1: 起始坐标y
        :param x2: 终止坐标x
        :param y2: 终止坐标y
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 适合opencv使用的图片np数组
        """
        if is_re_get_win == 1:
            self.d_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)
        start_x = x1 + self.left
        start_y = y1 + self.top
        width = x2 - x1
        height = y2 - y1

        screenshot = pyautogui.screenshot(region=(start_x, start_y, width, height))
        # 将PIL格式图片转换为np数组
        screen_np = np.array(screenshot)
        # 将np数组中图片rbg格式，转换为opencv中bgr格式
        screen_cv = cv2.cvtColor(screen_np, cv2.COLOR_RGB2BGR)
        return screen_cv

    def d_findpic(self, x1, y1, x2, y2, img_template, similarity, method=0, is_re_get_win=0):
        """
        前台模式，模板匹配找图，返回符合相似度要求,且相似度最高的那个中心点位置坐标, 如果没有合适位置，则返回0，注意范围要比参数图片的尺寸大


        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :param img_template: 要找的图片的全路径
        :param similarity: 相似度
        :param method: 找图方法，其中0 和 1表示越是接近1越相似，2表示越是接近0越相似
                                0 代表：cv2.TM_CCORR_NORMED：标准相似匹配，匹配值范围在0到1之间，匹配值越大表示两个图像越相似，其中1表示完美匹配，0表示没有匹配。
                                1 代表：cv2.TM_CCOEFF_NORMED：标准相关系数匹配，匹配值的范围是-1到1之间。匹配值越接近1，表示两个图像越相似
                                2 代表：cv2.TM_SQDIFF_NORMED：标准平方差匹配。匹配值的范围是0到1。匹配值越小表示两个图像越相似,完全匹配结果为0
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 返回符合相似度要求,且相似度最高的那个中心点位置坐标, 如果没有合适位置，则返回0
        """

        def get_max(_template_res):
            g_minvalue, g_maxvalue, g_minloc, g_maxloc = cv2.minMaxLoc(template_res)
            if g_maxvalue >= similarity:
                g_maxloc_center_point = (g_maxloc[0] + x1 + width // 2, g_maxloc[1] + y1 + height // 2)
                return g_maxloc_center_point
            else:
                return 0

        if is_re_get_win == 1:
            self.d_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)
        find_img = self.d_capture(x1, y1, x2, y2)
        img_template = cv2.imread(img_template)
        height, width, s = img_template.shape
        if method == 0:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCORR_NORMED)
            maxloc_center_point = get_max(template_res)
            return maxloc_center_point

        elif method == 1:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCOEFF_NORMED)
            maxloc_center_point = get_max(template_res)
            return maxloc_center_point
        elif method == 2:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_SQDIFF_NORMED)
            minvalue, maxvalue, minloc, maxloc = cv2.minMaxLoc(template_res)
            if minvalue <= similarity:
                min_loc_center_point = (minloc[0] + x1 + width // 2, minloc[1] + y1 + height // 2)
                return min_loc_center_point
            else:
                return 0

    def d_findpicEx(self, x1, y1, x2, y2, img_template, similarity, method=0, is_re_get_win=0):
        """
        前台模式，模板匹配找图，以列表的形式，返回所有符合相似度要求的中心点位置坐标, 如果没有合适位置，则返回空列表，注意范围要比参数图片的尺寸大
        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :param img_template: 要找的图片的全路径
        :param similarity: 相似度
        :param method: 找图方法，其中0 和 1表示越是接近1越相似，2表示越是接近0越相似
                                0 代表：cv2.TM_CCORR_NORMED：标准相似匹配，匹配值范围在0到1之间，匹配值越大表示两个图像越相似，其中1表示完美匹配，0表示没有匹配。
                                1 代表：cv2.TM_CCOEFF_NORMED：标准相关系数匹配，匹配值的范围是-1到1之间。匹配值越接近1，表示两个图像越相似
                                2 代表：cv2.TM_SQDIFF_NORMED：标准平方差匹配。匹配值的范围是0到1。匹配值越小表示两个图像越相似,完全匹配结果为0
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return:以列表的形式，返回所有符合相似度要求的中心点位置坐标
        """
        if is_re_get_win == 1:
            self.d_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)

        find_img = self.d_capture(x1, y1, x2, y2)
        img_template = cv2.imread(img_template)
        height, width, s = img_template.shape
        hypotenuse_sqr = width ** 2 + height ** 2
        res = None
        if method == 0:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCORR_NORMED)
            res = np.where(template_res > similarity)
        elif method == 1:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCOEFF_NORMED)
            res = np.where(template_res > similarity)
        elif method == 2:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_SQDIFF_NORMED)
            res = np.where(template_res < similarity)
        res_zip = zip(res[0], res[1])
        center_point_list = []
        for a in res_zip:
            b = a[::-1]
            center_point = (x1 + b[0] + width // 2, y1 + b[1] + height // 2)
            if len(center_point_list) == 0:
                center_point_list.append(center_point)
            else:
                not_append = False
                for i in center_point_list:
                    hypotenuse_sqr_point = (center_point[0] - i[0]) ** 2 + (center_point[1] - i[1]) ** 2
                    if hypotenuse_sqr_point <= hypotenuse_sqr:
                        not_append = True
                if not not_append:
                    center_point_list.append(center_point)

        return center_point_list

    def d_start_ocr(self, wechat_ocr_file, wechat_dll_file):
        """
        启动ocr
        :param wechat_ocr_file: 设置WeChatOCR.EXE 全路径，如'ocr/WeChatOCR.EXE'
        :param wechat_dll_file: 设置 mmmojo.dll mmmojo_64.dll路径
        :return: 无
        """

        def ocr_call_back(img_file, results):
            self.ocr_result = results

        # 实例化OcrManager类
        ocr_manager = OcrManager(wechat_dll_file)
        # 设置WeChatOCR.EXE的路径
        ocr_manager.SetExePath(wechat_ocr_file)
        # 设置dll文件路径
        ocr_manager.SetUsrLibDir(wechat_dll_file)
        # 设置回调函数
        ocr_manager.SetOcrResultCallback(ocr_call_back)
        # 启动ocr
        ocr_manager.StartWeChatOCR()
        self.ocr_manager = ocr_manager

    def d_findtext(self, x1, y1, x2, y2, is_re_get_win=0):
        """
        前台模式，本地ocr图片识字，使用前，必须调用start_ocr命令启动ocr服务。以列表形式，返回识别到的字的[内容，x坐标，y坐标]

        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 以列表形式，返回识别到的字的[内容，x坐标，y坐标]
        """

        def make_results():
            results = self.ocr_result
            if len(results['ocrResult']) > 0:
                ocr_text = results['ocrResult'][0]['text']
                text_left_x = results['ocrResult'][0]['location']['left']
                text_left_y = results['ocrResult'][0]['location']['top']
                text_right_x = results['ocrResult'][0]['location']['right']
                text_right_y = results['ocrResult'][0]['location']['bottom']

                if text_left_x is None:
                    text_left_x = 0
                if text_left_y is None:
                    text_left_y = 0
                if text_right_x is None:
                    text_right_x = 0
                if text_right_y is None:
                    text_right_y = 0
                text_center_x = (text_right_x - text_left_x) // 2
                text_center_y = (text_right_y - text_left_y) // 2
                text_center_x = x1 + text_center_x + text_left_x
                text_center_y = y1 + text_center_y + text_left_y
                ocr_res = [ocr_text, text_center_x, text_center_y]
                return ocr_res

        if is_re_get_win == 1:
            self.d_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)
        start_x = x1 + self.left
        start_y = y1 + self.top
        width = x2 - x1
        height = y2 - y1
        screenshot = pyautogui.screenshot(region=(start_x, start_y, width, height))
        img_text_name = str(time.time()) + '.bmp'
        screenshot.save(img_text_name)

        # 开始识别图片中的文字
        self.ocr_manager.DoOCRTask(img_text_name)
        while self.ocr_manager.m_task_id.qsize() != OCR_MAX_TASK_ID:
            pass
        orc_ress = make_results()
        os.remove(img_text_name)
        return orc_ress

    def d_close_ocr(self):
        """
        关闭ocr服务
        :return: 无
        """
        # 结束识别，关闭服务
        self.ocr_manager.KillWeChatOCR()

    """
    驱动2：
    """


class PID:
    """PID"""

    def __init__(self, P=0.35, I=0, D=0):
        """PID"""
        self.kp = P  # 比例
        self.ki = I  # 积分
        self.kd = D  # 微分
        self.uPrevious = 0  # 上一次控制量
        self.uCurent = 0  # 这一次控制量
        self.setValue = 0  # 目标值
        self.lastErr = 0  # 上一次差值
        self.errSum = 0  # 所有差值的累加
        self.errSumLimit = 10  # 近两次的差值累加

    def pidPosition(self, setValue, curValue):
        """位置式 PID 输出控制量"""
        self.setValue = setValue  # 更新目标值
        err = self.setValue - curValue  # 计算差值, 作为比例项
        dErr = err - self.lastErr  # 计算近两次的差值, 作为微分项
        self.errSum += err  # 累加这一次差值,作为积分项
        outPID = (self.kp * err) + (self.ki * self.errSum) + (self.kd * dErr)  # PID
        self.lastErr = err  # 保存这一次差值,作为下一次的上一次差值
        return outPID  # 输出

    def pidIncrease(self, setValue, curValue):
        """增量式 PID 输出控制量的差值"""
        self.uCurent = self.pidPosition(setValue, curValue)  # 计算位置式
        outPID = self.uCurent - self.uPrevious  # 计算差值
        self.uPrevious = self.uCurent  # 保存这一次输出量
        return outPID  # 输出


class jy_driver_l:

    def __init__(self):
        self.top = None
        self.left = None
        self.y_offset = None
        self.x_offset = None
        self.hwnd = None
        self.state = None
        self.dll = None
        self.ocr_manager = None
        self.ocr_result = None

    def get_window_rect(self, _hwnd, _is_first):
        """
        获取窗口的位置和大小
        :return: 返回一个包含窗口左上角和右下角坐标的元组 (left, top, right, bottom)
        """
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(_hwnd, ctypes.byref(rect)):
            raise ctypes.WinError()
        if _is_first == 0:
            ctypes.windll.user32.SetForegroundWindow(_hwnd)
        return rect.left, rect.top, rect.right, rect.bottom

    def l_getwindowposition(self, hwnd, x_offset, y_offset, is_first=0):
        """
        获取窗口的左上角坐标
        :param x_offset: x方向的偏移
        :param y_offset: y方向的偏移
        :param hwnd: 窗口句柄
        :param is_first: 本次程序运行，是否第一次调用本命令，默认为0，即第一次，非0为不是第一次
        :return: 返回一个包含窗口左上角坐标的元组 (left, top)
        """

        self.left, self.top, right, bottom = self.get_window_rect(hwnd, is_first)
        self.hwnd = hwnd
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.left = self.left + x_offset
        self.top = self.top + y_offset
        return self.left, self.top

    def l_set_ini(self, path_name):
        """
        驱动初始化，成功返回1，失败返回0
        :param path_name: 驱动dll全路径，即使与脚本文件同一个安装路径，也需要写完整路径，比如：X:/XX/XX.dll
        :return:成功返回1，失败返回0
        """

        self.dll = windll.LoadLibrary(path_name)  # 打开路径文件
        self.state = (self.dll.device_open() == 1)  # 启动, 并返回是否成功
        if self.state:
            print('驱动启动成功')
            return 1
        else:
            print('驱动启动失败')
            return 0

    def l_mouse_down(self, i=1):
        """
        驱动鼠标按下
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :return: 无
        """
        self.dll.mouse_down(i)

    def l_mouse_up(self, i=1):
        """
        驱动鼠标弹起
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :return: 无
        """
        self.dll.mouse_up(i)

    def l_mouse_real_move(self, x, y, min_time=0.03, min_step=2, is_re_get_win=0):
        """

        :param x: 整数型，x坐标
        :param y: 整数型，y坐标
        :param min_time: 数字类型，最小移动时间
        :param min_step: 整数型，最小步长
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return:
        """

        if is_re_get_win == 1:
            self.l_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)
        x = x + self.left
        y = y + self.top
        pid_x = PID()  # 创建pid对象
        pid_y = PID()

        while True:
            time.sleep(min_time)
            new_x, new_y = pyautogui.position()

            move_x = pid_x.pidPosition(x, new_x)  # 经过pid计算鼠标运动量
            move_y = pid_y.pidPosition(y, new_y)

            # print(f'x={new_x}, y={new_y}, xd={move_x}, yd={move_y}')
            if x == new_x and y == new_y:  # 如果重合就退出循环
                break

            if 0 < move_x < min_step:  # 限制正最小值
                move_x = min_step
            elif 0 > move_x > -min_step:  # 限制负最小值
                move_x = -min_step
            else:
                move_x = int(move_x)  # 需要输入整数,小数会报错

            if 0 < move_y < min_step:
                move_y = min_step
            elif 0 > move_y > -min_step:
                move_y = -min_step
            else:
                move_y = int(move_y)

            self.dll.moveR(move_x, move_y, True)  # 貌似有第三个参数,但是没试出来什么用

    def l_mouse_move_press(self, x, y, min_time=0.03, i=1, min_step=2, is_re_get_win=0):
        """
        鼠标移动点击
        :param x: 整数型，x坐标
        :param y: 整数型，y坐标
        :param min_time:数字类型，最小移动时间，及鼠标按下弹起时间间隔
        :param i: 整数类型，1左键，2中键，3右键，默认为左键
        :param min_step:整数型，最小步长
        :param is_re_get_win:是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return:
        """
        self.l_mouse_real_move(x, y, min_time, min_step, is_re_get_win)
        self.dll.mouse_down(i)
        time.sleep(min_time)
        self.dll.mouse_up(i)

    def l_key_down(self, key_name):
        """
        驱动键盘 按下 某键
        :param key_name:键盘名称，对应键帽上的字符，只支持'a'-'z''0'-'9'
        :return:无
        """
        self.dll.key_down(key_name)

    def l_key_up(self, key_name):
        """
        驱动键盘 弹起 某键
        :param key_name:键盘名称，对应键帽上的字符，只支持'a'-'z''0'-'9'
        :return:无
        """
        self.dll.key_up(key_name)

    def l_key_click(self, key_name):
        """
        驱动键盘，点击某键
        :param key_name:键盘名称，对应键帽上的字符，只支持'a'-'z''0'-'9'
        :return:无
        """
        self.dll.key_down(key_name)
        time.sleep(0.03)
        self.dll.key_up(key_name)

    def l_capture(self, x1, y1, x2, y2, is_re_get_win=0):
        """
        前台模式截图
        :param x1: 起始坐标x
        :param y1: 起始坐标y
        :param x2: 终止坐标x
        :param y2: 终止坐标y
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 适合opencv使用的图片np数组
        """
        if is_re_get_win == 1:
            self.l_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)
        start_x = x1 + self.left
        start_y = y1 + self.top
        width = x2 - x1
        height = y2 - y1

        screenshot = pyautogui.screenshot(region=(start_x, start_y, width, height))
        # 将PIL格式图片转换为np数组
        screen_np = np.array(screenshot)
        # 将np数组中图片rbg格式，转换为opencv中bgr格式
        screen_cv = cv2.cvtColor(screen_np, cv2.COLOR_RGB2BGR)
        return screen_cv

    def l_findpic(self, x1, y1, x2, y2, img_template, similarity, method=0, is_re_get_win=0):
        """
        前台模式，模板匹配找图，返回符合相似度要求,且相似度最高的那个中心点位置坐标, 如果没有合适位置，则返回0，注意范围要比参数图片的尺寸大


        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :param img_template: 要找的图片的全路径
        :param similarity: 相似度
        :param method: 找图方法，其中0 和 1表示越是接近1越相似，2表示越是接近0越相似
                                0 代表：cv2.TM_CCORR_NORMED：标准相似匹配，匹配值范围在0到1之间，匹配值越大表示两个图像越相似，其中1表示完美匹配，0表示没有匹配。
                                1 代表：cv2.TM_CCOEFF_NORMED：标准相关系数匹配，匹配值的范围是-1到1之间。匹配值越接近1，表示两个图像越相似
                                2 代表：cv2.TM_SQDIFF_NORMED：标准平方差匹配。匹配值的范围是0到1。匹配值越小表示两个图像越相似,完全匹配结果为0
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 返回符合相似度要求,且相似度最高的那个中心点位置坐标, 如果没有合适位置，则返回0
        """

        def get_max(_template_res):
            g_minvalue, g_maxvalue, g_minloc, g_maxloc = cv2.minMaxLoc(template_res)
            if g_maxvalue >= similarity:
                g_maxloc_center_point = (g_maxloc[0] + x1 + width // 2, g_maxloc[1] + y1 + height // 2)
                return g_maxloc_center_point
            else:
                return 0

        if is_re_get_win == 1:
            self.l_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)
        find_img = self.l_capture(x1, y1, x2, y2)
        img_template = cv2.imread(img_template)
        height, width, s = img_template.shape
        if method == 0:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCORR_NORMED)
            maxloc_center_point = get_max(template_res)
            return maxloc_center_point

        elif method == 1:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCOEFF_NORMED)
            maxloc_center_point = get_max(template_res)
            return maxloc_center_point
        elif method == 2:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_SQDIFF_NORMED)
            minvalue, maxvalue, minloc, maxloc = cv2.minMaxLoc(template_res)
            if minvalue <= similarity:
                min_loc_center_point = (minloc[0] + x1 + width // 2, minloc[1] + y1 + height // 2)
                return min_loc_center_point
            else:
                return 0

    def l_findpicEx(self, x1, y1, x2, y2, img_template, similarity, method=0, is_re_get_win=0):
        """
        前台模式，模板匹配找图，以列表的形式，返回所有符合相似度要求的中心点位置坐标, 如果没有合适位置，则返回空列表，注意范围要比参数图片的尺寸大
        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :param img_template: 要找的图片的全路径
        :param similarity: 相似度
        :param method: 找图方法，其中0 和 1表示越是接近1越相似，2表示越是接近0越相似
                                0 代表：cv2.TM_CCORR_NORMED：标准相似匹配，匹配值范围在0到1之间，匹配值越大表示两个图像越相似，其中1表示完美匹配，0表示没有匹配。
                                1 代表：cv2.TM_CCOEFF_NORMED：标准相关系数匹配，匹配值的范围是-1到1之间。匹配值越接近1，表示两个图像越相似
                                2 代表：cv2.TM_SQDIFF_NORMED：标准平方差匹配。匹配值的范围是0到1。匹配值越小表示两个图像越相似,完全匹配结果为0
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return:以列表的形式，返回所有符合相似度要求的中心点位置坐标
        """
        if is_re_get_win == 1:
            self.l_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)

        find_img = self.l_capture(x1, y1, x2, y2)
        img_template = cv2.imread(img_template)
        height, width, s = img_template.shape
        hypotenuse_sqr = width ** 2 + height ** 2
        res = None
        if method == 0:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCORR_NORMED)
            res = np.where(template_res > similarity)
        elif method == 1:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_CCOEFF_NORMED)
            res = np.where(template_res > similarity)
        elif method == 2:
            template_res = cv2.matchTemplate(find_img, img_template, cv2.TM_SQDIFF_NORMED)
            res = np.where(template_res < similarity)
        res_zip = zip(res[0], res[1])
        center_point_list = []
        for a in res_zip:
            b = a[::-1]
            center_point = (x1 + b[0] + width // 2, y1 + b[1] + height // 2)
            if len(center_point_list) == 0:
                center_point_list.append(center_point)
            else:
                not_append = False
                for i in center_point_list:
                    hypotenuse_sqr_point = (center_point[0] - i[0]) ** 2 + (center_point[1] - i[1]) ** 2
                    if hypotenuse_sqr_point <= hypotenuse_sqr:
                        not_append = True
                if not not_append:
                    center_point_list.append(center_point)

        return center_point_list

    def l_start_ocr(self, wechat_ocr_file, wechat_dll_file):
        """
        启动ocr
        :param wechat_ocr_file: 设置WeChatOCR.EXE 全路径，如'ocr/WeChatOCR.EXE'
        :param wechat_dll_file: 设置 mmmojo.dll mmmojo_64.dll路径
        :return: 无
        """

        def ocr_call_back(img_file, results):
            self.ocr_result = results

        # 实例化OcrManager类
        ocr_manager = OcrManager(wechat_dll_file)
        # 设置WeChatOCR.EXE的路径
        ocr_manager.SetExePath(wechat_ocr_file)
        # 设置dll文件路径
        ocr_manager.SetUsrLibDir(wechat_dll_file)
        # 设置回调函数
        ocr_manager.SetOcrResultCallback(ocr_call_back)
        # 启动ocr
        ocr_manager.StartWeChatOCR()
        self.ocr_manager = ocr_manager

    def l_findtext(self, x1, y1, x2, y2, is_re_get_win=0):
        """
        前台模式，本地ocr图片识字，使用前，必须调用start_ocr命令启动ocr服务。以列表形式，返回识别到的字的[内容，x坐标，y坐标]

        :param x1: 起始坐标x1
        :param y1: 起始坐标y1
        :param x2: 终止坐标x2
        :param y2: 终止坐标y2
        :param is_re_get_win: 是否重新获取窗口位置，1 重新获取， 0 不重新获取。默认位0，不重新获取
        :return: 以列表形式，返回识别到的字的[内容，x坐标，y坐标]
        """

        def make_results():
            results = self.ocr_result
            if len(results['ocrResult']) > 0:
                ocr_text = results['ocrResult'][0]['text']
                text_left_x = results['ocrResult'][0]['location']['left']
                text_left_y = results['ocrResult'][0]['location']['top']
                text_right_x = results['ocrResult'][0]['location']['right']
                text_right_y = results['ocrResult'][0]['location']['bottom']

                if text_left_x is None:
                    text_left_x = 0
                if text_left_y is None:
                    text_left_y = 0
                if text_right_x is None:
                    text_right_x = 0
                if text_right_y is None:
                    text_right_y = 0
                text_center_x = (text_right_x - text_left_x) // 2
                text_center_y = (text_right_y - text_left_y) // 2
                text_center_x = x1 + text_center_x + text_left_x
                text_center_y = y1 + text_center_y + text_left_y
                ocr_res = [ocr_text, text_center_x, text_center_y]
                return ocr_res

        if is_re_get_win == 1:
            self.l_getwindowposition(self.hwnd, self.x_offset, self.y_offset, 1)
        start_x = x1 + self.left
        start_y = y1 + self.top
        width = x2 - x1
        height = y2 - y1
        screenshot = pyautogui.screenshot(region=(start_x, start_y, width, height))
        img_text_name = str(time.time()) + '.bmp'
        screenshot.save(img_text_name)

        # 开始识别图片中的文字
        self.ocr_manager.DoOCRTask(img_text_name)
        while self.ocr_manager.m_task_id.qsize() != OCR_MAX_TASK_ID:
            pass
        orc_ress = make_results()
        os.remove(img_text_name)
        return orc_ress

    def l_close_ocr(self):
        """
        关闭ocr服务
        :return: 无
        """
        # 结束识别，关闭服务
        self.ocr_manager.KillWeChatOCR()
