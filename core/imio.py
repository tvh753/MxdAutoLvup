# -*- coding: utf-8 -*-
# @Time    : 26/8/27 4:34
# @Author  : yy
# @File    : imio.py
# @Software: MxdAutoLvup

# -*- coding: utf-8 -*-
"""Unicode 安全的图片读写（修复 Windows 中文路径）
OpenCV 在 Windows 上经 ANSI API 打开文件：路径含中文(如 maps\\蘑菇山\\)
时 cv2.imread 返回 None、cv2.imwrite 静默失败。
对策：np.fromfile + imdecode / imencode + tofile，绕开 ANSI 路径 API。
项目内所有图片 IO 一律使用本模块。
"""
import os
import cv2
import numpy as np


def imread_u(path, flags=cv2.IMREAD_COLOR):
    """中文路径安全读图；失败返回 None"""
    if not path or not os.path.exists(path):
        return None
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, flags)
    except Exception:
        return None


def imwrite_u(path, img, ext=None):
    """中文路径安全写图；成功返回 True"""
    if img is None or not path:
        return False
    ext = ext or (os.path.splitext(path)[1] or ".png")
    try:
        ok, buf = cv2.imencode(ext, img)
        if ok:
            buf.tofile(path)
        return bool(ok)
    except Exception:
        return False