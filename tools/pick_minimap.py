# -*- coding: utf-8 -*-
"""交互式框选小地图内容区"""

import tkinter as tk
from PIL import Image, ImageTk
import cv2
import numpy as np
import os

GAME_SHOT = r"D:\test_demo\MxdAutoLvup\test_game.png"


def imread_u(p):
    return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)


class Selector:
    def __init__(self, img_bgr):
        self.img = img_bgr
        self.H, self.W = img_bgr.shape[:2]
        self.scale = min(1.0, 1200 / self.W, 700 / self.H)
        self.dw = int(self.W * self.scale)
        self.dh = int(self.H * self.scale)

        self.root = tk.Tk()
        self.root.title("拖框选【地图内容区】—— 不含标题/标签/外边框")
        self.canvas = tk.Canvas(self.root, width=self.dw, height=self.dh)
        self.canvas.pack()

        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (self.dw, self.dh))
        self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        self.start = None
        self.rect = None
        self.canvas.bind("<Button-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)

        tk.Label(self.root,
                 text="⚠ 只框【地图地形】那块，不要框到「小地图」标题和「金银岛/蘑菇山」标签",
                 fg="red").pack()
        tk.Button(self.root, text="确认并打印坐标", command=self.confirm,
                  bg="#3ddc84").pack(pady=4)

    def press(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="red", width=2)

    def drag(self, e):
        if self.start and self.rect:
            self.canvas.coords(self.rect,
                               self.start[0], self.start[1], e.x, e.y)

    def release(self, e):
        if not self.start:
            return
        x0 = int(min(self.start[0], e.x) / self.scale)
        y0 = int(min(self.start[1], e.y) / self.scale)
        x1 = int(max(self.start[0], e.x) / self.scale)
        y1 = int(max(self.start[1], e.y) / self.scale)
        print(f"\n✅ 框选结果：MINIMAP = ({x0}, {y0}, {x1-x0}, {y1-y0})")

    def confirm(self):
        self.root.destroy()

    def run(self):
        self.root.mainloop()


img = imread_u(GAME_SHOT)
if img is None:
    print("读不到游戏截图")
else:
    Selector(img).run()