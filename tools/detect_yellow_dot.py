import cv2
import numpy as np
import os


# ================= 基础工具函数 =================
def cv_imread(file_path, flags=cv2.IMREAD_COLOR):
    try:
        img_array = np.fromfile(file_path, dtype=np.uint8)
        return cv2.imdecode(img_array, flags)
    except Exception as e:
        print(f"[错误] 读取图片失败: {file_path}, 异常: {e}")
        return None


def cv_imwrite(file_path, img):
    try:
        ext = os.path.splitext(file_path)[1] or '.png'
        result, n = cv2.imencode(ext, img)
        if result:
            with open(file_path, mode='wb') as f:
                n.tofile(f)
            return True
        return False
    except Exception as e:
        print(f"[错误] 保存图片失败: {file_path}, 异常: {e}")
        return False


# ================= 1. 提取小黄点坐标 =================
def detect_yellow_dot(image):
    if image is None or image.size == 0:
        return None

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    # 黄色 HSV 范围
    lower_yellow = np.array([20, 100, 100])
    upper_yellow = np.array([35, 255, 255])
    mask = cv2.inRange(hsv, lower_yellow, upper_yellow)

    kernel = np.ones((2, 2), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_DILATE, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    valid_contours = [c for c in contours if cv2.contourArea(c) > 1]
    if not valid_contours:
        return None

    max_contour = max(valid_contours, key=cv2.contourArea)
    ((cx, cy), radius) = cv2.minEnclosingCircle(max_contour)
    return (int(cx), int(cy))


# ================= 2. 坐标映射 =================
def map_coordinates(img1_shape, img2_shape, point1, offset_x=0, offset_y=0):
    h1, w1 = img1_shape[:2]
    h2, w2 = img2_shape[:2]

    x1, y1 = point1

    # 比例换算 + 偏移微调
    x2 = int(x1 * (w2 / w1)) + offset_x
    y2 = int(y1 * (h2 / h1)) + offset_y

    return (x2, y2)


# ================= 主流程 =================
def main():
    # 输入图片路径 (请替换为你实际的图片路径)
    MINI_MAP_PATH = r"D:\test_demo\MxdAutoLvup\tools\minimap.png"
    BIG_MAP_PATH = r"D:\test_demo\MxdAutoLvup\tools\map.png"

    OUTPUT_MINI_PATH = r"mini_map_marked.png"
    OUTPUT_BIG_PATH = r"big_map_marked.png"

    # ================= 微调参数 (核心！) =================
    # 如果发现映射出的红点偏左，就把 OFFSET_X 改大（比如 +5, +10）
    # 如果发现红点偏右，就把 OFFSET_X 改小（比如 -5, -10）
    # 如果红点偏上，就把 OFFSET_Y 改大（比如 +5, +10）
    # 如果红点偏下，就把 OFFSET_Y 改小（比如 -5, -10）
    # 根据你图片的偏差情况，红点目前偏左偏下，所以我们需要增加 X，减少 Y
    OFFSET_X = -8  # 向右微调
    OFFSET_Y = 0  # 向上微调
    # ====================================================

    img1 = cv_imread(MINI_MAP_PATH)
    img2 = cv_imread(BIG_MAP_PATH)

    if img1 is None or img2 is None:
        print("图片读取失败，请检查路径。")
        return

    print(f"图1尺寸: {img1.shape}, 图2尺寸: {img2.shape}")

    dot_pos_img1 = detect_yellow_dot(img1)
    if dot_pos_img1 is None:
        print("[警告] 未在图1中检测到黄色小点！")
        return

    print(f"[成功] 图1中小黄点坐标: {dot_pos_img1}")

    # 映射坐标到图2（传入微调参数）
    dot_pos_img2 = map_coordinates(img1.shape, img2.shape, dot_pos_img1, OFFSET_X, OFFSET_Y)
    print(f"[成功] 映射并微调后图2的坐标: {dot_pos_img2}")

    # 可视化标记
    img1_marked = img1.copy()
    cv2.circle(img1_marked, dot_pos_img1, 5, (0, 0, 255), 2)
    cv2.line(img1_marked, (dot_pos_img1[0] - 8, dot_pos_img1[1]), (dot_pos_img1[0] + 8, dot_pos_img1[1]), (0, 0, 255),
             2)
    cv2.line(img1_marked, (dot_pos_img1[0], dot_pos_img1[1] - 8), (dot_pos_img1[0], dot_pos_img1[1] + 8), (0, 0, 255),
             2)

    img2_marked = img2.copy()
    cv2.circle(img2_marked, dot_pos_img2, 15, (0, 0, 255), 3)
    cv2.line(img2_marked, (dot_pos_img2[0] - 25, dot_pos_img2[1]), (dot_pos_img2[0] + 25, dot_pos_img2[1]), (0, 0, 255),
             3)
    cv2.line(img2_marked, (dot_pos_img2[0], dot_pos_img2[1] - 25), (dot_pos_img2[0], dot_pos_img2[1] + 25), (0, 0, 255),
             3)
    cv2.putText(img2_marked, "Player", (dot_pos_img2[0] + 20, dot_pos_img2[1] - 20),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)

    cv_imwrite(OUTPUT_MINI_PATH, img1_marked)
    cv_imwrite(OUTPUT_BIG_PATH, img2_marked)
    print(f"[完成] 标记完成！")


if __name__ == "__main__":
    main()