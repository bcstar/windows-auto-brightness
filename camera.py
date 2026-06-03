"""
笔记本摄像头工具 — Python + OpenCV
用法:
  python camera.py             实时预览，按 SPACE 拍照，按 ESC/q 退出
  python camera.py --capture   直接拍一张照保存，不打开窗口
  python camera.py --list      列出可用摄像头设备
  python camera.py --index 1   指定摄像头设备索引（默认 0）
"""

import cv2
import sys
import os
from datetime import datetime

SAVE_DIR = os.path.join(os.path.expanduser("~"), "Pictures", "CameraCaptures")

def ensure_save_dir():
    os.makedirs(SAVE_DIR, exist_ok=True)

def timestamp_filename(prefix="capture"):
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(SAVE_DIR, f"{prefix}_{now}.png")


def list_cameras(max_test=5):
    """检测可用摄像头，最多尝试 max_test 个索引"""
    print("检测可用摄像头...")
    available = []
    for i in range(max_test):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                h, w = frame.shape[:2]
                available.append((i, w, h))
                print(f"  [{'OK':^4}] 索引 {i} → {w}x{h}")
            else:
                print(f"  [{'--':^4}] 索引 {i} → 可打开但无法读取")
            cap.release()
        else:
            print(f"  [{'--':^4}] 索引 {i} → 不可用")

    if not available:
        print("\n未检测到任何摄像头。")
        print("可能的原因：")
        print("  - 笔记本没有摄像头或驱动未安装")
        print("  - 摄像头被其他程序占用")
        print("  - 虚拟机中未启用摄像头直通")
    else:
        print(f"\n共检测到 {len(available)} 个可用摄像头。")
    return available


def quick_capture(camera_index=0):
    """直接拍照保存，不显示窗口"""
    ensure_save_dir()
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print(f"错误：无法打开摄像头（索引 {camera_index}）")
        sys.exit(1)

    # 给摄像头一点时间初始化
    for _ in range(5):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret:
        print("错误：无法从摄像头读取画面")
        sys.exit(1)

    path = timestamp_filename()
    cv2.imwrite(path, frame)
    print(f"照片已保存: {path}")
    return path


def preview_mode(camera_index=0):
    """实时预览，按 SPACE 拍照，按 ESC/q 退出"""
    ensure_save_dir()
    cap = cv2.VideoCapture(camera_index)

    if not cap.isOpened():
        print(f"错误：无法打开摄像头（索引 {camera_index}）")
        sys.exit(1)

    # 设置分辨率（可选）
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    print(f"摄像头已打开（索引 {camera_index}）")
    print("操作提示：")
    print("  SPACE → 拍照保存")
    print("  ESC / q → 退出")
    print("-" * 40)

    count = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            print("画面丢失，退出...")
            break

        # 在画面左上角显示信息
        info = f"Camera {camera_index} | {frame.shape[1]}x{frame.shape[0]}"
        cv2.putText(frame, info, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

        cv2.imshow("Camera - SPACE拍照 ESC退出", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == 27 or key == ord('q'):      # ESC 或 q
            print("已退出")
            break
        elif key == 32:                        # SPACE
            count += 1
            path = timestamp_filename(f"capture_{count:03d}")
            cv2.imwrite(path, frame)
            print(f"  [拍照] 已保存: {path}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="笔记本摄像头工具")
    parser.add_argument("--capture", action="store_true", help="直接拍照保存")
    parser.add_argument("--list", action="store_true", help="列出可用摄像头")
    parser.add_argument("--index", type=int, default=0, help="摄像头设备索引（默认 0）")

    args = parser.parse_args()

    if args.list:
        list_cameras()
    elif args.capture:
        quick_capture(args.index)
    else:
        preview_mode(args.index)
