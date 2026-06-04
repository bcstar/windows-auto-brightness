"""
摄像头模拟环境光传感器 — 核心逻辑模块
供 auto-brightness.py (CLI) 和 auto-brightness-gui.py (GUI) 共同使用

v3: 改用 pywin32 COM 直调 WMI，亮度读写从 400ms 降到 5ms
"""

import cv2
import time
import os
import json
import threading
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
CAMERA_INDEX = 0            # 默认摄像头索引

# ============ WMI 亮度控制（pywin32 COM，替代慢速 PowerShell） ============
_WMI_SERVICE = None
_WMI_BRIGHTNESS_PATH = None


def _ensure_wmi():
    """懒初始化 WMI 连接（全局缓存，多线程共享）"""
    global _WMI_SERVICE, _WMI_BRIGHTNESS_PATH
    if _WMI_SERVICE is not None:
        return _WMI_SERVICE, _WMI_BRIGHTNESS_PATH

    try:
        import win32com.client
        locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        svc = locator.ConnectServer(".", "root/wmi")

        # 获取亮度写入方法的对象路径
        path = None
        methods = svc.ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods")
        for m in methods:
            path = m.Path_.Path
            break

        _WMI_SERVICE = svc
        _WMI_BRIGHTNESS_PATH = path
        return svc, path
    except ImportError:
        print("  [警告] 未安装 pywin32，亮度控制不可用 (pip install pywin32)")
        return None, None
    except Exception as e:
        print(f"  [警告] WMI 初始化失败: {e}")
        return None, None


def get_current_brightness():
    """读取当前屏幕亮度（0-100），通过 COM 直调 WMI"""
    svc, _ = _ensure_wmi()
    if svc is None:
        return None
    try:
        items = svc.ExecQuery("SELECT * FROM WmiMonitorBrightness")
        for item in items:
            return item.CurrentBrightness
        return None
    except Exception:
        return None


def set_brightness(brightness):
    """设置屏幕亮度（0-100），通过 COM 直调 WMI"""
    b = max(0, min(100, brightness))
    svc, path = _ensure_wmi()
    if svc is None or path is None:
        return
    try:
        cls = svc.Get("WmiMonitorBrightnessMethods")
        method = cls.Methods_("WmiSetBrightness")
        in_params = method.InParameters.SpawnInstance_()
        in_params.Brightness = b
        in_params.Timeout = 1
        svc.ExecMethod(path, "WmiSetBrightness", in_params)
    except Exception as e:
        print(f"  [错误] 设置亮度失败: {e}")


def get_frame_light(frame):
    """计算画面平均亮度（0-100），先裁剪边缘减少摄像头自动曝光影响"""
    h, w = frame.shape[:2]
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

    def reset(self):
        self.buffer = []


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
    - 环境光低于最小样本 → 用最小样本的屏幕亮度
    - 环境光高于最大样本 → 用最大样本的屏幕亮度
    - 环境光在区间内 → 线性插值
    """
    if not samples:
        return 50

    sorted_samples = sorted(samples, key=lambda s: s[0])
    env_vals = [s[0] for s in sorted_samples]
    scr_vals = [s[1] for s in sorted_samples]

    if env_light <= env_vals[0]:
        return scr_vals[0]
    if env_light >= env_vals[-1]:
        return scr_vals[-1]

    for i in range(len(env_vals) - 1):
        if env_vals[i] <= env_light <= env_vals[i + 1]:
            ratio = (env_light - env_vals[i]) / (env_vals[i + 1] - env_vals[i])
            return round(scr_vals[i] + ratio * (scr_vals[i + 1] - scr_vals[i]))

    return round(scr_vals[0])


def get_calibration_samples():
    """加载校准样本列表，返回 [(env, screen), ...] 或 None"""
    calib = load_calibration()
    if calib:
        return [(s["env"], s["screen"]) for s in calib["samples"]]
    return None


def open_camera(index=0):
    """打开摄像头（DirectShow 后端，比默认 MSMF 快 ~1s）"""
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if cap.isOpened():
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
    return cap


def warmup_camera(cap, frames=10):
    """预热摄像头，让自动曝光稳定"""
    for _ in range(frames):
        cap.read()
    time.sleep(0.3)


def capture_one_frame(cap):
    """读取一帧，返回 frame 或 None"""
    for _ in range(3):
        ret, frame = cap.read()
        if ret:
            return frame
        time.sleep(0.1)
    return None


class CameraManager:
    """全局摄像头管理器——应用启动时开启，持续采集最新帧，所有人读缓存。

    解决了：
    - 每次打开摄像头 1.5s 的延迟（只开一次）
    - 多个模块各自开摄像头打架的问题
    """

    def __init__(self, index=0):
        self.index = index
        self._cap = None
        self._lock = threading.Lock()
        self._latest_frame = None
        self._running = False
        self._thread = None
        self._error = None

    def start(self):
        """在后台线程打开摄像头并持续采集"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self._cap = open_camera(self.index)
        if not self._cap or not self._cap.isOpened():
            self._error = "无法打开摄像头"
            return

        for _ in range(10):
            self._cap.read()
        time.sleep(0.3)

        while self._running:
            ret, frame = self._cap.read()
            if ret:
                with self._lock:
                    self._latest_frame = frame

        if self._cap:
            self._cap.release()

    def get_frame(self):
        """获取最新一帧（线程安全），返回 frame 副本或 None"""
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def get_light_level(self):
        """获取当前环境光强度"""
        frame = self.get_frame()
        if frame is not None:
            return get_frame_light(frame)
        return None

    @property
    def is_ready(self):
        return self._latest_frame is not None and self._error is None

    @property
    def error(self):
        return self._error

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self._latest_frame = None


def compute_brightness_adjustment(env_light, samples):
    """计算当前环境光对应的目标亮度"""
    target = interpolate_brightness(env_light, samples)
    current = get_current_brightness()
    return target, current


def should_adjust(target, current, deadband=DEADBAND):
    """判断是否需要调整亮度（考虑死区）"""
    if current is None:
        return True
    return abs(target - current) >= deadband


# ============ 校准模式（CLI 版本） ============
def calibrate_cli(camera_index=0):
    """CLI 校准模式——记录环境光与屏幕亮度的对应关系"""
    print("=" * 60)
    print("  校准模式")
    print("=" * 60)
    print("  步骤:")
    print("    1. 调到你感觉舒服的屏幕亮度")
    print("    2. 按 SPACE 记录")
    print("    3. 换个光线环境，重复1-2")
    print("    4. 按 ESC 结束")
    print()
    print("  提示: 建议至少记录 3-5 种场景:")
    print("    - 关灯全黑环境")
    print("    - 只开台灯")
    print("    - 正常室内灯光")
    print("    - 靠窗白天自然光")
    print("=" * 60)

    cap = open_camera(camera_index)
    if not cap or not cap.isOpened():
        print("错误：无法打开摄像头")
        return []

    warmup_camera(cap)
    samples = []

    while True:
        frame = capture_one_frame(cap)
        if frame is None:
            break

        env_light = get_frame_light(frame)
        screen_brightness = get_current_brightness()

        lines = [
            f"Environmental Light: {env_light:.1f}/100",
            f"Screen Brightness:   {screen_brightness or 'N/A'}%",
            f"Records: {len(samples)}",
            "SPACE=record  ESC=exit",
        ]
        for i, line in enumerate(lines):
            cv2.putText(frame, line, (10, 30 + i * 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)

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
                print("  [错误] 无法读取当前屏幕亮度")

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
