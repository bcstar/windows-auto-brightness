"""
摄像头模拟环境光传感器 — Windows 自动亮度调节 v2 (CLI)
核心逻辑在 brightness_core.py，此文件只负责 CLI 入口。

用法:
  python auto-brightness.py                   启动自动亮度
  python auto-brightness.py --calibrate       校准模式
  python auto-brightness.py --debug           调试模式
  python auto-brightness.py --fast            更激进（每 2 秒检测）
"""

import sys
import os

# 把当前目录加入 sys.path，确保能 import brightness_core
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import time
import cv2
from datetime import datetime
from brightness_core import (
    DEFAULT_INTERVAL, FAST_INTERVAL, CHANGE_THRESHOLD,
    LightAverager, load_calibration, interpolate_brightness,
    get_current_brightness, set_brightness,
    open_camera, capture_one_frame, get_frame_light,
    calibrate_cli as calibrate,
)


def auto_brightness_loop(camera_index=0, interval=DEFAULT_INTERVAL, debug=False):
    """主循环"""
    calib = load_calibration()
    if not calib:
        print("⚠ 未找到校准数据，请先运行 --calibrate")
        print("   临时使用默认范围 20%-100%")
        samples = [(5, 20), (50, 60), (90, 100)]
    else:
        samples = [(s["env"], s["screen"]) for s in calib["samples"]]
        print(f"✓ 已加载校准数据 ({len(samples)} 个样本)")
        for s in samples:
            print(f"   环境光 {s[0]:.1f} → 屏幕 {s[1]}%")

    print(f"\n{'=' * 50}")
    print("  自动亮度运行中")
    print(f"    检测间隔: {interval}秒")
    print(f"    快变阈值: 环境光变化 > {CHANGE_THRESHOLD} → 立即调整")
    print(f"    校准样本: {len(samples)} 个")
    print(f"    按 Ctrl+C 退出")
    print(f"{'=' * 50}")

    cap = open_camera(camera_index)
    if not cap or not cap.isOpened():
        print(f"错误：无法打开摄像头（索引 {camera_index}）")
        sys.exit(1)

    averager = LightAverager()
    last_env_light = None
    last_set_at = time.time()

    # 预热
    for _ in range(10):
        cap.read()
    time.sleep(0.3)

    try:
        while True:
            for _ in range(3):
                cap.read()
            frame = capture_one_frame(cap)
            if frame is None:
                time.sleep(0.5)
                continue

            env_light = get_frame_light(frame)
            averager.add(env_light)
            avg_light = averager.value

            now = time.time()

            if not averager.is_warm:
                time.sleep(interval)
                continue

            target = interpolate_brightness(avg_light, samples)

            rapid_change = (last_env_light is not None
                            and abs(avg_light - last_env_light) >= CHANGE_THRESHOLD)

            time_elapsed = now - last_set_at
            should_adjust = False

            if time_elapsed >= interval:
                should_adjust = True
            elif rapid_change and time_elapsed >= 1:
                should_adjust = True

            if should_adjust:
                current = get_current_brightness()
                if current is None or abs(target - current) >= 3:  # DEADBAND
                    set_brightness(target)
                    last_set_at = now

                    if debug or rapid_change:
                        trigger = "突变" if rapid_change else "定时"
                        now_str = datetime.now().strftime("%H:%M:%S")
                        print(f"  [{now_str}][{trigger}] 环境={avg_light:.1f}  "
                              f"屏幕={current}%→{target}%")

                last_env_light = avg_light

            if debug:
                current = get_current_brightness() or 0
                gray_display = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                info = f"Light:{avg_light:.1f}  Target:{target}%  Now:{current}%"
                cv2.putText(gray_display, info, (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (150, 255, 150), 2)
                cv2.imshow("Auto Brightness Debug", gray_display)
                if cv2.waitKey(1) & 0xFF == 27:
                    raise KeyboardInterrupt

            for _ in range(interval * 2):
                time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n已退出")
    except Exception as e:
        print(f"\n错误: {e}")
    finally:
        cap.release()
        cv2.destroyAllWindows()
        print("已清理资源")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="摄像头自动亮度 v2 - Windows")
    parser.add_argument("--index", type=int, default=0, help="摄像头索引")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"检测间隔秒数（默认 {DEFAULT_INTERVAL}）")
    parser.add_argument("--calibrate", action="store_true", help="校准模式")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    parser.add_argument("--fast", action="store_true",
                        help=f"快速模式（{FAST_INTERVAL}秒间隔）")
    args = parser.parse_args()

    if args.calibrate:
        calibrate(args.index)
    else:
        interval = FAST_INTERVAL if args.fast else args.interval
        auto_brightness_loop(args.index, interval, args.debug)
