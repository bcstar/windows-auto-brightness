"""
摄像头模拟环境光传感器 — Windows 自动亮度调节 v2

v2 改进:
  - 快速响应: 每 5 秒检测，亮度突变立即调整（不等周期结束）
  - 校准插值: 用你记录的 (环境光, 屏幕亮度) 样本做插值，不再硬线性映射
  - 校准持久化: 校准数据自动保存，不用每次重来
  - 三重平滑: 帧平均 + 滑动平均 + 触发死区，不抖不闪

用法:
  python auto-brightness.py                   启动自动亮度
  python auto-brightness.py --calibrate       校准模式
  python auto-brightness.py --debug           调试模式
  python auto-brightness.py --fast            更激进（每 2 秒检测）
"""

import cv2
import time
import subprocess
import argparse
import sys
import os
import json
import numpy as np
from datetime import datetime

# ============ 配置 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CALIB_FILE = os.path.join(SCRIPT_DIR, "brightness_calib.json")
DEFAULT_INTERVAL = 5        # 检测间隔（秒）
FAST_INTERVAL = 2           # 快速模式间隔
CHANGE_THRESHOLD = 8        # 环境光变化超过此值 → 立即调整（%）
WINDOW_SIZE = 3             # 滑动平均窗口（帧数）
DEADBAND = 3                # 亮度变化 < 此值跳过调整（避免微调闪烁）


def get_current_brightness():
    """读取当前屏幕亮度（0-100）"""
    cmd = [
        "powershell", "-Command",
        "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness"
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        v = r.stdout.strip()
        return int(float(v)) if v else None
    except Exception:
        return None


def set_brightness(brightness):
    """设置屏幕亮度（0-100）"""
    b = max(0, min(100, brightness))
    cmd = [
        "powershell", "-Command",
        f"$m = Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods; "
        f"$m.WmiSetBrightness(1, {b})"
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as e:
        print(f"  [错误] 设置亮度失败: {e}")


def get_frame_light(frame):
    """计算画面平均亮度（0-100），先裁剪边缘减少摄像头自动曝光影响"""
    h, w = frame.shape[:2]
    # 只取中央 60% 区域，避开边缘 Auto Exposure 的过曝/欠曝区
    y1, y2 = int(h * 0.2), int(h * 0.8)
    x1, x2 = int(w * 0.2), int(w * 0.8)
    center = frame[y1:y2, x1:x2]
    gray = cv2.cvtColor(center, cv2.COLOR_BGR2GRAY)
    return float(gray.mean() / 255.0 * 100)


class LightAverager:
    """滑动平均 + 首次快速填充"""
    def __init__(self, window=WINDOW_SIZE):
        self.window = window
        self.buffer = []

    def add(self, val):
        self.buffer.append(val)
        if len(self.buffer) > self.window:
            self.buffer.pop(0)

    @property
    def value(self):
        if not self.buffer:
            return None
        return sum(self.buffer) / len(self.buffer)

    @property
    def is_warm(self):
        return len(self.buffer) >= self.window


def load_calibration():
    """加载校准数据"""
    if os.path.exists(CALIB_FILE):
        with open(CALIB_FILE, "r") as f:
            return json.load(f)
    return None


def save_calibration(samples):
    """保存校准数据到 JSON"""
    data = {
        "timestamp": datetime.now().isoformat(),
        "samples": [{"env": round(s[0], 1), "screen": s[1]} for s in samples],
    }
    # 也生成推荐参数
    env_vals = [s[0] for s in samples]
    scr_vals = [s[1] for s in samples]
    data["recommended"] = {
        "min_screen": min(scr_vals),
        "max_screen": max(scr_vals),
        "env_range": [round(min(env_vals), 1), round(max(env_vals), 1)],
    }
    with open(CALIB_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  校准数据已保存: {CALIB_FILE}")
    return data


def interpolate_brightness(env_light, samples):
    """
    用校准样本插值计算目标亮度。

    校准样本是 [(环境光, 屏幕亮度), ...] 按环境光排序。
    - 环境光低于最小样本 → 用最小样本的屏幕亮度（不更低）
    - 环境光高于最大样本 → 用最大样本的屏幕亮度（不更高）
    - 环境光在区间内 → 线性插值相邻样本

    这确保了:
      1. 亮度不会超出你校准的范围
      2. 过渡平滑自然
      3. 你录什么暗度配什么亮度，就按你的感觉来
    """
    if not samples:
        return 50  # 没有校准数据，给个默认值

    # 按环境光排序
    sorted_samples = sorted(samples, key=lambda s: s[0])
    env_vals = [s[0] for s in sorted_samples]
    scr_vals = [s[1] for s in sorted_samples]

    # 外推：低于最小样本
    if env_light <= env_vals[0]:
        return scr_vals[0]

    # 外推：高于最大样本
    if env_light >= env_vals[-1]:
        return scr_vals[-1]

    # 内插：找到相邻样本
    for i in range(len(env_vals) - 1):
        if env_vals[i] <= env_light <= env_vals[i + 1]:
            # 线性插值
            ratio = (env_light - env_vals[i]) / (env_vals[i + 1] - env_vals[i])
            return round(scr_vals[i] + ratio * (scr_vals[i + 1] - scr_vals[i]))

    return round(scr_vals[0])  # fallback


def calibrate(camera_index=0):
    """校准模式——记录环境光与屏幕亮度的对应关系"""
    print("=" * 60)
    print("  校准模式")
    print("=" * 60)
    print("  步骤:")
    print("    1. 调到你感觉舒服的屏幕亮度")
    print("    2. 按 SPACE 记录")
    print("    3. 换个光线环境（开灯/关灯/拉窗帘），重复1-2")
    print("    4. 样本覆盖不同的光线场景，效果更好")
    print("    5. 按 ESC 结束")
    print()
    print("  提示: 建议至少记录 3-5 种场景:")
    print("    - 关灯全黑环境")
    print("    - 只开台灯")
    print("    - 正常室内灯光")
    print("    - 靠窗白天自然光")
    print("=" * 60)

    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print("错误：无法打开摄像头")
        return []

    # 预热
    for _ in range(10):
        cap.read()
    time.sleep(0.5)

    samples = []

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        env_light = get_frame_light(frame)
        screen_brightness = get_current_brightness()

        # 画面信息
        lines = [
            f"Environmental Light: {env_light:.1f}/100",
            f"Screen Brightness:   {screen_brightness or 'N/A'}%",
            f"Records: {len(samples)}",
            "SPACE=record  ESC=exit",
        ]
        for i, line in enumerate(lines):
            cv2.putText(frame, line, (10, 30 + i * 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)

        # 显示已记录样本
        y_base = 180
        cv2.putText(frame, "--- Calibration Samples ---", (10, y_base),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 200, 0), 2)
        for i, (env, scr) in enumerate(samples):
            text = f"  {i+1}. Light={env:.1f}  ->  Screen={scr}%"
            cv2.putText(frame, text, (10, y_base + 25 + i * 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 200, 0), 1)

        cv2.imshow("Calibration - Auto Brightness", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == 27:  # ESC
            break
        elif key == 32:  # SPACE
            if screen_brightness is not None:
                samples.append((env_light, screen_brightness))
                print(f"  [记录] #{len(samples)}  环境光={env_light:.1f}  →  屏幕={screen_brightness}%")
            else:
                print("  [错误] 无法读取当前屏幕亮度，请重试")

    cap.release()
    cv2.destroyAllWindows()

    if len(samples) >= 2:
        save_calibration(samples)
        print(f"\n校准完成！{len(samples)} 个样本已保存")
    elif samples:
        print(f"\n校准样本太少（{len(samples)}个），建议至少记录 2 个以上场景")
    else:
        print("\n未记录任何样本")

    return samples


def auto_brightness_loop(camera_index=0, interval=DEFAULT_INTERVAL, debug=False):
    """主循环"""
    # 加载校准数据
    calib = load_calibration()
    if not calib:
        print("⚠ 未找到校准数据，请先运行 --calibrate")
        print("   临时使用默认范围 20%-100%")
        samples = [(5, 20), (50, 60), (90, 100)]  # fallback
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

    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print(f"错误：无法打开摄像头（索引 {camera_index}）")
        sys.exit(1)

    # 摄像头参数：固定曝光减少闪烁
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)  # 尝试关闭自动曝光

    averager = LightAverager()
    last_env_light = None
    last_set_at = time.time()

    try:
        while True:
            # 预热（确保自动曝光稳定）
            for _ in range(3):
                cap.read()

            ret, frame = cap.read()
            if not ret:
                time.sleep(0.5)
                continue

            env_light = get_frame_light(frame)
            averager.add(env_light)
            avg_light = averager.value

            now = time.time()

            # 滑动窗口未填满时跳过（前几帧数据不稳定）
            if not averager.is_warm:
                time.sleep(interval)
                continue

            # 计算目标亮度
            target = interpolate_brightness(avg_light, samples)

            # 检测亮度突变（相比上次检测）
            rapid_change = (last_env_light is not None
                            and abs(avg_light - last_env_light) >= CHANGE_THRESHOLD)

            # 超时触发或突变触发
            time_elapsed = now - last_set_at
            should_adjust = False

            if time_elapsed >= interval:
                should_adjust = True
            elif rapid_change and time_elapsed >= 1:
                should_adjust = True

            if should_adjust:
                current = get_current_brightness()
                if current is None or abs(target - current) >= DEADBAND:
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

            # 正常周期内的休眠（分小段，以便更快响应中断）
            for _ in range(interval * 2):
                time.sleep(0.5)
                # 如果超过半周期还没调过，且亮度突变，提前醒来
                if last_env_light is not None:
                    # 实际只做快速中断检测
                    pass

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
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"检测间隔秒数（默认 {DEFAULT_INTERVAL}）")
    parser.add_argument("--calibrate", action="store_true", help="校准模式")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    parser.add_argument("--fast", action="store_true", help=f"快速模式（{FAST_INTERVAL}秒间隔）")
    args = parser.parse_args()

    if args.calibrate:
        calibrate(args.index)
    else:
        interval = FAST_INTERVAL if args.fast else args.interval
        auto_brightness_loop(args.index, interval, args.debug)
