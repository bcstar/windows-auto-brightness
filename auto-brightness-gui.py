"""
摄像头模拟环境光传感器 — Windows 自动亮度调节 v2 图形界面
核心逻辑在 brightness_core.py，此文件提供 tkinter GUI。

用法:
  python auto-brightness-gui.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
import threading
import queue
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont
from PIL import Image, ImageTk

from brightness_core import (
    DEFAULT_INTERVAL, FAST_INTERVAL, CHANGE_THRESHOLD, DEADBAND,
    LightAverager, load_calibration, save_calibration,
    get_calibration_samples, interpolate_brightness,
    get_current_brightness, set_brightness,
    get_frame_light, open_camera, capture_one_frame,
    CALIB_FILE,
)

# ============ 图标/表情常量 ============
ICON_RUNNING = "🟢"
ICON_STOPPED = "🔴"
ICON_BULB = "💡"
ICON_SUN = "☀️"
ICON_MOON = "🌙"
ICON_STAR = "★"
ICON_RECORD = "●"


# ============ 工作线程 ============
class BrightnessWorker(threading.Thread):
    """后台摄像头采集 + 亮度计算 + 自动调节"""

    def __init__(self, camera_index=0, interval=DEFAULT_INTERVAL,
                 fast=False, samples=None, enable_camera_preview=True):
        super().__init__(daemon=True)
        self.camera_index = camera_index
        self.interval = FAST_INTERVAL if fast else interval
        self.samples = samples or [(5, 20), (50, 60), (90, 100)]
        self.enable_preview = enable_camera_preview

        self._stop_event = threading.Event()
        self.data_queue = queue.Queue(maxsize=5)
        self.preview_queue = queue.Queue(maxsize=3)

        self.averager = LightAverager()
        self.cap = None

    def run(self):
        self.cap = open_camera(self.camera_index)
        if not self.cap or not self.cap.isOpened():
            self.data_queue.put({"type": "error", "msg": "无法打开摄像头"})
            return

        # 预热
        for _ in range(10):
            self.cap.read()
        time.sleep(0.3)

        last_env = None
        last_set_at = time.time()
        last_frame_time = time.time()

        # 通知 UI 更新状态
        self.data_queue.put({"type": "started"})

        while not self._stop_event.is_set():
            frame = capture_one_frame(self.cap)
            if frame is None:
                time.sleep(0.1)
                continue

            # 计算环境光
            env_light = get_frame_light(frame)
            self.averager.add(env_light)
            avg = self.averager.value

            now = time.time()

            if self.averager.is_warm and avg is not None:
                target = interpolate_brightness(avg, self.samples)
                current = get_current_brightness()

                # 突变检测
                rapid = (last_env is not None
                         and abs(avg - last_env) >= CHANGE_THRESHOLD)

                elapsed = now - last_set_at
                should = False
                if elapsed >= self.interval:
                    should = True
                elif rapid and elapsed >= 1:
                    should = True

                if should:
                    if current is None or abs(target - current) >= DEADBAND:
                        set_brightness(target)
                        reason = "突变" if rapid else "定时"
                        self.data_queue.put({
                            "type": "adjust",
                            "time": now,
                            "env_light": round(avg, 1),
                            "target": target,
                            "current_before": current,
                            "reason": reason,
                        })
                        last_set_at = now

                last_env = avg

                # 实时数据推送
                try:
                    self.data_queue.put_nowait({
                        "type": "data",
                        "time": now,
                        "env_light": round(avg, 1),
                        "target": target,
                        "current": current or 0,
                        "is_warm": True,
                    })
                except queue.Full:
                    pass

            # 预览帧（降帧率）
            now_f = time.time()
            if self.enable_preview and now_f - last_frame_time > 0.2:  # 5fps
                last_frame_time = now_f
                small = cv2_resize_for_gui(frame, 320)
                try:
                    self.preview_queue.put_nowait(small)
                except queue.Full:
                    pass

            # 休眠分段检测
            for _ in range(5):
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)

        # 清理
        if self.cap:
            self.cap.release()
        self.data_queue.put({"type": "stopped"})

    def stop(self):
        self._stop_event.set()

    @property
    def is_running(self):
        return not self._stop_event.is_set() and self.is_alive()


def cv2_resize_for_gui(frame, max_width=320):
    """缩放 OpenCV 帧到 GUI 预览尺寸"""
    h, w = frame.shape[:2]
    scale = max_width / w
    new_w = int(w * scale)
    new_h = int(h * scale)
    return cv2_resize(frame, (new_w, new_h))


def cv2_resize(frame, size):
    """OpenCV resize + 颜色转换"""
    import cv2
    small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2RGB)


# ============ 曲线绘制工具 ============
def draw_calibration_curve(canvas, samples, width, height,
                           current_env=None, current_target=None, current_screen=None):
    """在 Canvas 上绘制校准曲线"""
    canvas.delete("all")

    if not samples:
        canvas.create_text(width // 2, height // 2,
                           text="无校准数据", fill="#888",
                           font=("Segoe UI", 12))
        return

    # 边距
    margin_left = 50
    margin_right = 20
    margin_top = 25
    margin_bottom = 35

    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    # 画背景
    canvas.create_rectangle(margin_left, margin_top,
                            width - margin_right, height - margin_bottom,
                            fill="#f8f9fa", outline="#ddd")

    # 坐标轴
    canvas.create_line(margin_left, height - margin_bottom,
                       width - margin_right, height - margin_bottom,
                       fill="#333", width=1)
    canvas.create_line(margin_left, margin_top,
                       margin_left, height - margin_bottom,
                       fill="#333", width=1)

    # 网格线（每 10 一格）
    for v in range(0, 101, 10):
        x = margin_left + plot_w * v / 100
        y = margin_top + plot_h * (1 - v / 100)

        canvas.create_line(x, margin_top, x, height - margin_bottom,
                           fill="#e0e0e0", width=1)
        canvas.create_line(margin_left, y, width - margin_right, y,
                           fill="#e0e0e0", width=1)

        # 坐标轴标签
        if v > 0:
            canvas.create_text(x, height - margin_bottom + 15,
                               text=str(v), fill="#666", font=("Segoe UI", 8))
        canvas.create_text(margin_left - 12, y,
                           text=str(v), fill="#666", font=("Segoe UI", 8),
                           anchor="e")

    # 轴标题
    canvas.create_text(width // 2, height - 3,
                       text="环境光 →", fill="#333",
                       font=("Segoe UI", 9))
    canvas.create_text(14, height // 2,
                       text="← 屏幕亮度", fill="#333",
                       font=("Segoe UI", 9), angle=90)

    # 整理样本：按环境光排序
    sorted_sm = sorted(samples, key=lambda s: s[0])
    envs = [s[0] for s in sorted_sm]
    scrs = [s[1] for s in sorted_sm]

    def to_canvas(e, s):
        x = margin_left + plot_w * e / 100
        y = margin_top + plot_h * (1 - s / 100)
        return x, y

    # 插值曲线（精细采样）
    curve_points = []
    for e in range(0, 101):
        t = interpolate_brightness(e, samples)
        curve_points.append(to_canvas(e, t))
    for i in range(len(curve_points) - 1):
        canvas.create_line(*curve_points[i], *curve_points[i + 1],
                           fill="#2196F3", width=2, smooth=True)

    # 样本点
    for env, scr in sorted_sm:
        x, y = to_canvas(env, scr)
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                           fill="#FF9800", outline="#E65100", width=2)

    # 标签
    for i, (env, scr) in enumerate(sorted_sm):
        x, y = to_canvas(env, scr)
        canvas.create_text(x + 12, y - 10, text=f"{scr}%",
                           fill="#E65100", font=("Segoe UI", 8))

    # 当前工作点
    if current_env is not None:
        tgt = current_target or interpolate_brightness(current_env, samples)
        x, y = to_canvas(current_env, tgt)
        canvas.create_oval(x - 6, y - 6, x + 6, y + 6,
                           fill="#4CAF50", outline="#1B5E20", width=2)
        label = f"当前 {tgt}%"
        if current_screen is not None:
            label = f"屏幕 {current_screen}% (目标 {tgt}%)"
        canvas.create_text(x + 14, y + 6, text=label,
                           fill="#1B5E20", font=("Segoe UI", 8, "bold"))


# ============ 校准窗口 ============
class CalibrationWindow(tk.Toplevel):
    """GUI 校准窗口——替代 OpenCV 窗口的校准体验"""

    def __init__(self, parent, camera_index=0):
        super().__init__(parent)
        self.parent = parent
        self.camera_index = camera_index
        # 加载已有校准数据作为起点（累计，不覆盖）
        calib = load_calibration()
        self.samples = [(s["env"], s["screen"]) for s in calib["samples"]] if calib else []
        self.cap = None
        self._running = True

        self.title("校准模式 — 自动亮度")
        self.geometry("700x620")
        self.minsize(640, 560)
        self.configure(bg="#f0f0f0")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # 布局框架
        main_frame = ttk.Frame(self, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # === 顶部信息栏 ===
        info_frame = ttk.LabelFrame(main_frame, text="当前状态", padding=8)
        info_frame.pack(fill=tk.X, pady=(0, 8))

        self.info_env_var = tk.StringVar(value="环境光: --")
        self.info_screen_var = tk.StringVar(value="屏幕亮度: --%")
        self.info_records_var = tk.StringVar(value="已记录: 0 个样本")

        ttk.Label(info_frame, textvariable=self.info_env_var,
                  font=("Segoe UI", 11)).pack(side=tk.LEFT, padx=10)
        ttk.Label(info_frame, textvariable=self.info_screen_var,
                  font=("Segoe UI", 11)).pack(side=tk.LEFT, padx=10)
        ttk.Label(info_frame, textvariable=self.info_records_var,
                  font=("Segoe UI", 11)).pack(side=tk.LEFT, padx=10)

        # === 中部：预览 + 曲线 ===
        mid_frame = ttk.Frame(main_frame)
        mid_frame.pack(fill=tk.BOTH, expand=True, pady=4)

        # 摄像头预览
        preview_frame = ttk.LabelFrame(mid_frame, text="摄像头预览", padding=4)
        preview_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        self.preview_label = ttk.Label(preview_frame, background="#222")
        self.preview_label.pack(fill=tk.BOTH, expand=True)

        # 曲线面板
        curve_frame = ttk.LabelFrame(mid_frame, text="校准曲线", padding=4)
        curve_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        self.curve_canvas = tk.Canvas(curve_frame, bg="white", height=260)
        self.curve_canvas.pack(fill=tk.BOTH, expand=True)

        # === 底部：操作按钮 + 样本列表 ===
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.pack(fill=tk.X, pady=(8, 0))

        # 按钮
        btn_frame = ttk.Frame(bottom_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 6))

        self.btn_record = ttk.Button(
            btn_frame, text=f"{ICON_RECORD} 记录当前 (SPACE)",
            command=self.record_sample, width=18)
        self.btn_record.pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_frame, text="删除最后一个",
            command=self.delete_last, width=14).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_frame, text="清空所有",
            command=self.clear_all, width=10).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_frame, text="✓ 完成并保存",
            command=self.save_and_close, width=14).pack(side=tk.RIGHT, padx=2)

        self.btn_save = ttk.Button(
            btn_frame, text="保存但不退出",
            command=self.save_only, width=14)
        self.btn_save.pack(side=tk.RIGHT, padx=2)

        # 样本列表 + 提示
        list_frame = ttk.Frame(bottom_frame)
        list_frame.pack(fill=tk.X)

        self.samples_listbox = tk.Listbox(
            list_frame, height=5, font=("Consolas", 10))
        self.samples_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                  command=self.samples_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.samples_listbox.config(yscrollcommand=scrollbar.set)

        # 提示文本
        tip_frame = ttk.Frame(main_frame)
        tip_frame.pack(fill=tk.X, pady=(4, 0))
        tip_text = ("提示：调好屏幕亮度 → 按「记录」或空格键 → "
                    "换光线环境再调再记 → 至少记录 3-5 个不同场景")
        ttk.Label(tip_frame, text=tip_text, foreground="#666",
                  font=("Segoe UI", 9)).pack()

        # 绑定快捷键
        self.bind("<space>", lambda e: self.record_sample())
        self.bind("<Escape>", lambda e: self.save_and_close())

        # 在列表框中显示已有样本
        self._populate_samples_list()

        # 启动摄像头
        self.start_camera()
        self.after(100, self.poll_camera)
        self.after(500, self.redraw_curve)

    def start_camera(self):
        self.cap = open_camera(self.camera_index)
        if not self.cap or not self.cap.isOpened():
            messagebox.showerror("错误", "无法打开摄像头")
            self._running = False
            return
        for _ in range(10):
            self.cap.read()
        time.sleep(0.3)

    def poll_camera(self):
        if not self._running:
            return
        if self.cap and self.cap.isOpened():
            frame = capture_one_frame(self.cap)
            if frame is not None:
                env_light = get_frame_light(frame)
                screen = get_current_brightness()

                # 更新信息
                self.info_env_var.set(f"环境光: {env_light:.1f}/100")
                self.info_screen_var.set(
                    f"屏幕亮度: {screen}%" if screen else "屏幕亮度: N/A")
                self.info_records_var.set(
                    f"已记录: {len(self.samples)} 个样本")

                # 更新预览
                small = cv2_resize_for_gui(frame, 300)
                img = Image.fromarray(small)
                imgtk = ImageTk.PhotoImage(img)
                self.preview_label.config(image=imgtk)
                self.preview_label.image = imgtk

                # 更新曲线上的当前点
                self._current_env = env_light
                self._current_screen = screen
                self.redraw_curve()

        self.after(150, self.poll_camera)

    def _populate_samples_list(self):
        """在列表框中显示已有样本"""
        for i, (env, scr) in enumerate(self.samples):
            self.samples_listbox.insert(
                tk.END,
                f"  #{i+1:2d}  "
                f"环境光 {env:6.1f}  →  屏幕 {scr:3d}%")
        self.info_records_var.set(f"已记录: {len(self.samples)} 个样本")

    def record_sample(self):
        if not self._running or not self.cap:
            return
        frame = capture_one_frame(self.cap)
        if frame is None:
            return
        env = get_frame_light(frame)
        screen = get_current_brightness()
        if screen is None:
            messagebox.showwarning("无法记录", "无法读取当前屏幕亮度，请重试")
            return

        self.samples.append((round(env, 1), screen))
        self.samples_listbox.insert(
            tk.END,
            f"  #{len(self.samples):2d}  "
            f"环境光 {env:6.1f}  →  屏幕 {screen:3d}%")
        self.samples_listbox.see(tk.END)
        self.redraw_curve()
        self.info_records_var.set(f"已记录: {len(self.samples)} 个样本")

    def delete_last(self):
        if self.samples:
            self.samples.pop()
            self.samples_listbox.delete(tk.END)
            self.redraw_curve()
            self.info_records_var.set(f"已记录: {len(self.samples)} 个样本")

    def clear_all(self):
        if self.samples and messagebox.askyesno("确认", "清空所有校准记录？"):
            self.samples = []
            self.samples_listbox.delete(0, tk.END)
            self.redraw_curve()
            self.info_records_var.set("已记录: 0 个样本")

    def save_only(self):
        if len(self.samples) < 2:
            messagebox.showwarning("样本不足",
                                   "至少需要 2 个校准样本才能保存。")
            return
        save_calibration(self.samples)
        messagebox.showinfo("已保存",
                            f"校准数据已保存（{len(self.samples)} 个样本）")

    def save_and_close(self):
        if len(self.samples) >= 2:
            save_calibration(self.samples)
        self.on_close()

    def redraw_curve(self):
        if not hasattr(self, '_current_env'):
            self._current_env = None
            self._current_screen = None
        w = self.curve_canvas.winfo_width() or 280
        h = self.curve_canvas.winfo_height() or 260
        draw_calibration_curve(
            self.curve_canvas, self.samples, w, h,
            current_env=(
                self._current_env if hasattr(self, '_current_env')
                else None),
            current_target=None,
            current_screen=(
                self._current_screen if hasattr(self, '_current_screen')
                else None),
        )

    def on_close(self):
        self._running = False
        if self.cap:
            self.cap.release()
        self.parent._calibration_window = None
        self.destroy()


# ============ 曲线查看窗口 ============
class CurveViewWindow(tk.Toplevel):
    """查看校准曲线的大窗口"""

    def __init__(self, parent, samples):
        super().__init__(parent)
        self.parent = parent
        self.samples = samples

        self.title("校准曲线 — 自动亮度")
        self.geometry("600x480")
        self.configure(bg="#f0f0f0")

        frame = ttk.Frame(self, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="环境光 → 屏幕亮度 映射曲线",
                  font=("Segoe UI", 12, "bold")).pack(anchor=tk.W, pady=(0, 8))

        self.canvas = tk.Canvas(frame, bg="white")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        ttk.Button(frame, text="关闭", command=self.destroy).pack(pady=(8, 0))

        self.after(100, self._draw)

    def _draw(self):
        w = self.canvas.winfo_width() or 560
        h = self.canvas.winfo_height() or 400
        draw_calibration_curve(self.canvas, self.samples, w, h)

    def refresh(self, samples):
        self.samples = samples
        self._draw()


# ============ 主窗口 ============
class AutoBrightnessApp:
    """自动亮度调节 GUI 主窗口"""

    def __init__(self, root):
        self.root = root
        self.root.title("自动亮度调节 v2")
        self.root.geometry("860x660")
        self.root.minsize(720, 560)

        # 状态变量
        self.worker = None
        self._calibration_window = None
        self._curve_window = None
        self._running = False
        self._debug_mode = tk.BooleanVar(value=False)
        self._fast_mode = tk.BooleanVar(value=False)
        self._preview_enabled = tk.BooleanVar(value=True)
        self._interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL))
        self._samples = get_calibration_samples() or []

        # 数据缓冲区
        self._preview_img = None
        self._last_data = {"env_light": 0, "target": 50, "current": 50}

        # 日志
        self._log_lines = []

        # === 构建界面 ===
        self._setup_styles()
        self._build_menu()
        self._build_ui()
        self._update_status_display()
        self._poll_queue()

        # 绑定退出
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)

    def _setup_styles(self):
        style = ttk.Style()
        style.configure("Running.TLabel", foreground="#2e7d32")
        style.configure("Stopped.TLabel", foreground="#c62828")
        style.configure("Card.TFrame", relief=tk.RAISED, borderwidth=1)
        style.configure("Header.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Gauge.TFrame", relief=tk.SUNKEN, borderwidth=1)

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="启动", command=self.start, accelerator="F5")
        file_menu.add_command(label="停止", command=self.stop, accelerator="F6")
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_exit)
        menubar.add_cascade(label="文件", menu=file_menu)

        calib_menu = tk.Menu(menubar, tearoff=0)
        calib_menu.add_command(label="校准向导...", command=self.open_calibration)
        calib_menu.add_command(label="查看校准曲线...", command=self.open_curve_view)
        menubar.add_cascade(label="校准", menu=calib_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_checkbutton(label="调试信息", variable=self._debug_mode)
        view_menu.add_checkbutton(label="摄像头预览", variable=self._preview_enabled)
        menubar.add_cascade(label="视图", menu=view_menu)

        # 快捷键
        self.root.bind("<F5>", lambda e: self.start())
        self.root.bind("<F6>", lambda e: self.stop())

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # === 第一行：仪表盘 ===
        dashboard = ttk.Frame(main)
        dashboard.pack(fill=tk.X, pady=(0, 8))

        # 环境光仪表
        env_frame = ttk.LabelFrame(dashboard, text="环境光", padding=8, width=200)
        env_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        env_frame.pack_propagate(False)

        self.env_value_label = ttk.Label(
            env_frame, text="-- / 100", font=("Segoe UI", 20, "bold"))
        self.env_value_label.pack()

        self.env_bar = ttk.Progressbar(
            env_frame, length=180, mode="determinate", maximum=100)
        self.env_bar.pack(fill=tk.X, pady=4)

        self.env_label = ttk.Label(env_frame, text="等待启动...",
                                   font=("Segoe UI", 9))
        self.env_label.pack()

        # 屏幕亮度仪表
        screen_frame = ttk.LabelFrame(dashboard, text="屏幕亮度", padding=8, width=200)
        screen_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        screen_frame.pack_propagate(False)

        self.screen_value_label = ttk.Label(
            screen_frame, text="--%", font=("Segoe UI", 20, "bold"))
        self.screen_value_label.pack()

        self.screen_bar = ttk.Progressbar(
            screen_frame, length=180, mode="determinate", maximum=100)
        self.screen_bar.pack(fill=tk.X, pady=4)

        self.screen_label = ttk.Label(screen_frame, text="等待启动...",
                                      font=("Segoe UI", 9))
        self.screen_label.pack()

        # 状态卡片
        status_frame = ttk.LabelFrame(dashboard, text="状态", padding=8, width=180)
        status_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        status_frame.pack_propagate(False)

        self.status_icon = ttk.Label(status_frame, text=ICON_STOPPED,
                                     font=("Segoe UI", 24))
        self.status_icon.pack()

        self.status_label = ttk.Label(status_frame, text="已停止",
                                      font=("Segoe UI", 11))
        self.status_label.pack()

        self.status_detail = ttk.Label(status_frame, text="按 ▶ 启动",
                                       font=("Segoe UI", 9), foreground="#666")
        self.status_detail.pack()

        # === 第二行：摄像头预览 + 曲线 ===
        mid_frame = ttk.Frame(main)
        mid_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        # 预览
        preview_outer = ttk.LabelFrame(mid_frame, text="摄像头预览",
                                       padding=4)
        preview_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

        self.preview_label = ttk.Label(preview_outer, background="#222")
        self.preview_label.pack(fill=tk.BOTH, expand=True)

        # 校准曲线（小）
        curve_outer = ttk.LabelFrame(mid_frame, text="校准曲线", padding=4)
        curve_outer.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(4, 0))

        self.curve_canvas = tk.Canvas(curve_outer, bg="white")
        self.curve_canvas.pack(fill=tk.BOTH, expand=True)

        # === 第三行：控制 ===
        ctrl_frame = ttk.LabelFrame(main, text="控制", padding=8)
        ctrl_frame.pack(fill=tk.X, pady=(0, 8))

        btn_row1 = ttk.Frame(ctrl_frame)
        btn_row1.pack(fill=tk.X, pady=(0, 4))

        self.btn_start = ttk.Button(
            btn_row1, text=f"{ICON_RUNNING} 启动", command=self.start, width=12)
        self.btn_start.pack(side=tk.LEFT, padx=2)

        self.btn_stop = ttk.Button(
            btn_row1, text=f"{ICON_STOPPED} 停止", command=self.stop, width=12,
            state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_row1, text=f"{ICON_BULB} 校准向导...",
            command=self.open_calibration, width=16).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_row1, text="📊 校准曲线",
            command=self.open_curve_view, width=14).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_row1, text="🗑 清除日志",
            command=self.clear_log, width=12).pack(side=tk.RIGHT, padx=2)

        ttk.Button(
            btn_row1, text="⚙ 重新加载校准",
            command=self.reload_calibration, width=16).pack(
                side=tk.RIGHT, padx=2)

        btn_row2 = ttk.Frame(ctrl_frame)
        btn_row2.pack(fill=tk.X)

        ttk.Checkbutton(
            btn_row2, text="快速模式 (2s)", variable=self._fast_mode).pack(
                side=tk.LEFT, padx=2)

        ttk.Checkbutton(
            btn_row2, text="调试日志", variable=self._debug_mode).pack(
                side=tk.LEFT, padx=2)

        ttk.Checkbutton(
            btn_row2, text="摄像头预览", variable=self._preview_enabled,
            command=self._on_preview_toggle).pack(side=tk.LEFT, padx=2)

        ttk.Label(btn_row2, text="间隔:").pack(side=tk.LEFT, padx=(20, 0))
        self.interval_spin = ttk.Spinbox(
            btn_row2, from_=1, to=60, width=5,
            textvariable=self._interval_var)
        self.interval_spin.pack(side=tk.LEFT, padx=2)
        ttk.Label(btn_row2, text="秒").pack(side=tk.LEFT)

        # 校准样本数
        self.sample_count_label = ttk.Label(
            btn_row2, text=f"样本: {len(self._samples)}",
            font=("Segoe UI", 9), foreground="#555")
        self.sample_count_label.pack(side=tk.RIGHT, padx=10)

        # === 第四行：日志 ===
        log_frame = ttk.LabelFrame(main, text="事件日志", padding=4)
        log_frame.pack(fill=tk.X, pady=(0, 0))

        log_inner = ttk.Frame(log_frame)
        log_inner.pack(fill=tk.X)

        self.log_text = tk.Text(
            log_inner, height=6, font=("Consolas", 9),
            wrap=tk.WORD, state=tk.DISABLED, bg="#fafafa")
        self.log_text.pack(side=tk.LEFT, fill=tk.X, expand=True)

        log_scroll = ttk.Scrollbar(log_inner, orient=tk.VERTICAL,
                                   command=self.log_text.yview)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=log_scroll.set)

    def _on_preview_toggle(self):
        """预览开关切换"""
        pass  # 运行时通过 worker 的 enable_preview 控制

    def log(self, msg, tag=None):
        """写日志"""
        now = datetime.now().strftime("%H:%M:%S")
        line = f"[{now}] {msg}\n"
        self._log_lines.append(line)
        if len(self._log_lines) > 500:
            self._log_lines.pop(0)

        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, line)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
        self._log_lines = []

    # ---- 控制 ----

    def start(self):
        if self._running:
            return

        # 加载校准
        samples = get_calibration_samples()
        if not samples:
            ret = messagebox.askyesno(
                "未校准",
                "没有找到校准数据。是否先用默认曲线？\n\n"
                "点「否」打开校准向导。")
            if not ret:
                self.open_calibration()
                return
            samples = [(5, 20), (50, 60), (90, 100)]

        self._samples = samples
        self.sample_count_label.config(text=f"样本: {len(samples)}")

        interval = FAST_INTERVAL if self._fast_mode.get() else int(
            self._interval_var.get() or DEFAULT_INTERVAL)

        self.worker = BrightnessWorker(
            camera_index=0,
            interval=interval,
            fast=self._fast_mode.get(),
            samples=samples,
            enable_camera_preview=self._preview_enabled.get(),
        )
        self.worker.start()
        self._running = True

        # 更新 UI
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.status_icon.config(text=ICON_RUNNING)
        self.status_label.config(text="运行中")
        self.status_detail.config(text=f"间隔 {interval}s")

        self.log("自动亮度已启动")

    def stop(self):
        if self.worker and self._running:
            self.worker.stop()
            self._running = False

            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            self.status_icon.config(text=ICON_STOPPED)
            self.status_label.config(text="已停止")
            self.status_detail.config(text="按 ▶ 启动")

            self.env_value_label.config(text="-- / 100")
            self.env_bar.config(value=0)
            self.screen_value_label.config(text="--%")
            self.screen_bar.config(value=0)

            self.log("自动亮度已停止")
            self.worker = None

    def _poll_queue(self):
        """定期从工作线程拉取数据"""
        if self.worker and self._running:
            try:
                while True:
                    data = self.worker.data_queue.get_nowait()
                    self._handle_worker_data(data)
            except queue.Empty:
                pass

            # 预览帧
            if self._preview_enabled.get() and self.worker.enable_preview:
                try:
                    while True:
                        frame_rgb = self.worker.preview_queue.get_nowait()
                        self._update_preview(frame_rgb)
                except queue.Empty:
                    pass

        self.root.after(100, self._poll_queue)

    def _handle_worker_data(self, data):
        dt = data.get("type", "")

        if dt == "data" or dt == "adjust":
            env = data.get("env_light", 0)
            target = data.get("target", 50)
            current = data.get("current", 50)
            self._last_data = {"env_light": env, "target": target,
                               "current": current}

            self._update_gauges(env, target, current)
            self._update_status_display()

            if dt == "adjust":
                self.log(
                    f"[{data.get('reason','')}] 环境光 {data.get('env_light')} → "
                    f"亮度 {data.get('current_before')}% → {target}%")

        elif dt == "error":
            self.log(f"❌ 错误: {data.get('msg', '未知')}", "error")

        elif dt == "started":
            pass

        elif dt == "stopped":
            self.stop()

    def _update_preview(self, frame_rgb):
        img = Image.fromarray(frame_rgb)
        imgtk = ImageTk.PhotoImage(img)
        self.preview_label.config(image=imgtk)
        self.preview_label.image = imgtk

    def _update_gauges(self, env_light, target, current):
        # 环境光
        self.env_value_label.config(text=f"{env_light:.0f} / 100")
        self.env_bar.config(value=env_light)
        # 亮色时暖色，暗色时冷色
        if env_light > 60:
            env_desc = "明亮"
        elif env_light > 30:
            env_desc = "正常"
        else:
            env_desc = "昏暗"
        self.env_label.config(text=env_desc)

        # 屏幕亮度
        self.screen_value_label.config(text=f"{current}%")
        self.screen_bar.config(value=current)
        # 显示目标和当前区别
        diff = abs(target - current) if current else 0
        if diff > DEADBAND:
            detail = f"目标 {target}% ({'+' if target > current else ''}{-diff if target < current else diff})"
        else:
            detail = f"当前 {current}% ✓"
        self.screen_label.config(text=detail)

    def _update_status_display(self):
        if not self._running:
            return
        data = self._last_data
        self.status_detail.config(
            text=f"环境光 {data['env_light']:.0f}/100 → "
                 f"目标 {data['target']}% | 实际 {data['current']}%")

    # ---- 窗口管理 ----

    def open_calibration(self):
        if self._running:
            messagebox.showwarning("先停止", "校准前请先停止自动亮度调节")
            return
        if self._calibration_window is not None:
            self._calibration_window.lift()
            return
        self._calibration_window = CalibrationWindow(self.root)
        self._calibration_window.protocol(
            "WM_DELETE_WINDOW", self._on_calibration_close)

    def _on_calibration_close(self):
        if self._calibration_window:
            self._calibration_window.on_close()
        self.reload_calibration()

    def open_curve_view(self):
        samples = get_calibration_samples()
        if not samples:
            messagebox.showinfo("无数据", "还没有校准数据，请先运行校准。")
            return
        if self._curve_window is not None:
            self._curve_window.refresh(samples)
            self._curve_window.lift()
            return
        self._curve_window = CurveViewWindow(self.root, samples)
        self._curve_window.protocol(
            "WM_DELETE_WINDOW", lambda: setattr(self, '_curve_window', None))

    def reload_calibration(self):
        self._samples = get_calibration_samples() or []
        self.sample_count_label.config(text=f"样本: {len(self._samples)}")
        if self._samples:
            self.log(f"已重新加载校准（{len(self._samples)} 个样本）")
        self._redraw_small_curve()

    def _redraw_small_curve(self):
        w = self.curve_canvas.winfo_width() or 300
        h = self.curve_canvas.winfo_height() or 200
        if self._running and self.worker:
            data = self._last_data
            draw_calibration_curve(
                self.curve_canvas, self._samples, w, h,
                current_env=data["env_light"],
                current_target=data["target"],
                current_screen=data["current"])
        else:
            draw_calibration_curve(
                self.curve_canvas, self._samples, w, h)
        self.root.after(2000, self._redraw_small_curve)

    def on_exit(self):
        if self._running:
            ret = messagebox.askyesno("确认退出",
                                      "自动亮度正在运行，确定要退出吗？")
            if not ret:
                return
            self.stop()
        if self._calibration_window:
            self._calibration_window.on_close()
        self.root.destroy()


# ============ 启动点 ============
def main():
    root = tk.Tk()
    app = AutoBrightnessApp(root)

    # 自动加载校准后刷新曲线
    app.reload_calibration()
    app.log("{0} 自动亮度调节 v2   {1}".format(ICON_SUN, ICON_MOON))
    app.log("按 F5 启动 / F6 停止")
    if app._samples:
        app.log(f"✓ 已加载校准数据（{len(app._samples)} 个样本）")
    else:
        app.log("⚠ 未校准，请先运行「校准向导」或使用 --calibrate 命令行参数")

    root.mainloop()


if __name__ == "__main__":
    # 确保导入 cv2（需要但不作为 core 的显式依赖）
    import cv2 as _cv2
    main()
